"""tools.py - STUDENT IMPLEMENTS.  Source tools for the research agents.   Guide: GUIDE.md, part 1.

Rules for every tool:
  * runs on the HOST (not in the sandbox): API keys must never enter the sandbox;
  * returns a STRING (JSON text of compact records) and NEVER raises:
        "NO RESULTS"  when the source answers with nothing,
        "ERROR: ..."  when the source keeps failing after the retries (the agent then tries another source);
  * the docstring is the tool description the LLM reads: keep it precise (what it does, what it returns, when to use it).
Try your tools without any agent:   python tools.py
"""
import json
import os
import random
import re
import threading
import time
import xml.etree.ElementTree  # arXiv answers with Atom XML

import httpx
from dotenv import load_dotenv
from langchain_core.tools import tool

load_dotenv()  # EXA_API_KEY for `python tools.py`; the key stays on the host

# ---- constants (given) ----
ARXIV_URL = "https://export.arxiv.org/api/query"  # https only: http answers 301
HF_DAILY_URL = "https://huggingface.co/api/daily_papers"
HF_SEARCH_URL = "https://huggingface.co/api/papers/search"
EXA_URL = "https://mcp.exa.ai/mcp"

RETRY_STATUS = {429, 500, 502, 503, 504}
ARXIV_MIN_INTERVAL = 3.0  # seconds between two arXiv calls (their API etiquette)
ARXIV_MAX_TERMS = 8
ATOM = "{http://www.w3.org/2005/Atom}"
SUMMARY_CHARS = 600
PAGE_CHARS = 12000
ERROR_CHARS = 300
EXA_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
EXA_RATE_LIMIT_TEXT = "exa's free mcp rate limit"


class RetryableError(Exception):
    """Given. Raise it inside a call to ask with_retry to wait and try again (retry_after in seconds, optional)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


# ---- TODO 1: retry helper ----
def with_retry(fn, *, attempts=5, base=1.0, cap=30.0):
    """Call fn(); when it raises RetryableError, wait and call it again.

    PSEUDO-CODE:
      for attempt in 0 .. attempts-1:
          try: return fn()
          except RetryableError as e:
              if this was the last attempt: raise
              delay = e.retry_after if the server told us, else exponential backoff base * 2**attempt
              cap the delay at `cap` seconds; add random jitter to the exponential case
              sleep(delay)
    Use it to wrap EVERY network call below. Also treat these as retryable: HTTP 429/500/502/503/504,
    httpx.TransportError (timeouts, connection resets). Read the Retry-After header when present.
    """
    attempts = max(1, attempts)
    for attempt in range(attempts):
        try:
            return fn()
        except RetryableError as exc:
            if attempt == attempts - 1:
                raise
            if exc.retry_after is not None:
                delay = min(exc.retry_after, cap)
            else:
                delay = min(base * 2 ** attempt, cap)
                delay = random.uniform(delay / 2, delay)  # jitter: clients must not all wake up together
            time.sleep(delay)


def _retry_after(response):
    """Seconds asked by the Retry-After header, or None (absent, or an HTTP date)."""
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _send(method, url, *, timeout=30.0, **kwargs):
    """One HTTP call. Transient failures become RetryableError; call it inside with_retry."""
    try:
        response = httpx.request(method, url, timeout=httpx.Timeout(timeout, connect=10.0), **kwargs)
    except httpx.TransportError as exc:
        raise RetryableError(f"{type(exc).__name__}: {exc}") from exc
    if response.status_code in RETRY_STATUS:
        raise RetryableError(f"HTTP {response.status_code} from {response.url.host}", retry_after=_retry_after(response))
    response.raise_for_status()
    return response


def _clean(text):
    return " ".join(str(text or "").split())


def _clamp(value, low, high):
    return max(low, min(high, int(value)))


def _redact(text):
    """Remove the Exa key: it travels in the endpoint URL, and httpx puts URLs in its error messages."""
    key = (os.getenv("EXA_API_KEY") or "").strip()
    if key:
        text = text.replace(key, "***")
    return re.sub(r"(?i)(exaApiKey=)[^&\s'\"]+", r"\1***", text)


def _error(exc):
    return _redact(f"ERROR: {type(exc).__name__}: {exc}")[:ERROR_CHARS]


_arxiv_lock = threading.Lock()  # researchers run in parallel threads
_arxiv_last_call = 0.0


def _arxiv_get(params):
    global _arxiv_last_call
    with _arxiv_lock:
        wait = _arxiv_last_call + ARXIV_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _arxiv_last_call = time.monotonic()
    return _send("GET", ARXIV_URL, params=params)


def _arxiv_terms(query):
    """Plain keywords of an LLM-written query: field prefixes, quotes, colons and boolean operators are dropped."""
    words = re.findall(r"[^\W_]+(?:-[^\W_]+)*", re.sub(r"\b(?:all|ti|abs|au|cat|co|jr|rn|id):", " ", query))
    return [w for w in words if len(w) > 1 and w not in {"AND", "OR", "NOT", "ANDNOT"}][:ARXIV_MAX_TERMS]


# ---- TODO 2: arXiv ----
@tool
def arxiv_search(query: str, max_results: int = 10) -> str:
    """Search arXiv papers by keywords, newest first. Returns a JSON list of {id, url, published, title, summary}."""
    # PSEUDO-CODE:
    #   keep only word characters of `query` -> terms; no terms -> "NO RESULTS" (do not call the network)
    #   respect arXiv etiquette: at least 3 seconds between two arXiv calls (remember the time of the last call)
    #   GET ARXIV_URL params: search_query="all:t1 AND all:t2 ...", sortBy=submittedDate, sortOrder=descending,
    #       max_results=clamp(max_results, 1, 30)           (wrap in with_retry)
    #   parse the Atom XML: each <entry> -> {id (last part of <id> after /abs/), url, published[:10], title, summary}
    #       collapse whitespace/newlines in title and summary; cut summary to ~600 chars
    #   no entries -> "NO RESULTS"; else json.dumps(records, ensure_ascii=False)
    #   any exception -> "ERROR: <type>: <message>"
    try:
        terms = _arxiv_terms(query)
        if not terms:
            return "NO RESULTS"
        params = {"search_query": " AND ".join(f"all:{t}" for t in terms), "sortBy": "submittedDate",
                  "sortOrder": "descending", "start": 0, "max_results": _clamp(max_results, 1, 30)}
        response = with_retry(lambda: _arxiv_get(params), attempts=6, cap=60.0)  # shared IPs get 429 for a while
        records = []
        for entry in xml.etree.ElementTree.fromstring(response.content).iter(f"{ATOM}entry"):
            link = entry.findtext(f"{ATOM}id") or ""
            if "/abs/" not in link:  # arXiv reports its own errors as an <entry>
                continue
            paper_id = re.sub(r"v\d+$", "", link.split("/abs/")[-1].strip())
            records.append({"id": paper_id, "url": f"https://arxiv.org/abs/{paper_id}",
                            "published": (entry.findtext(f"{ATOM}published") or "")[:10],
                            "title": _clean(entry.findtext(f"{ATOM}title")),
                            "summary": _clean(entry.findtext(f"{ATOM}summary"))[:SUMMARY_CHARS]})
        return json.dumps(records, ensure_ascii=False) if records else "NO RESULTS"
    except Exception as exc:  # noqa: BLE001  (a tool never raises)
        return _error(exc)


# ---- TODO 3: Hugging Face ----
def _hf_records(items, prefer_ai_summary=False):
    records = []
    for item in items if isinstance(items, list) else []:
        paper = item.get("paper") or {}
        if not paper.get("id"):
            continue
        summary = (prefer_ai_summary and paper.get("ai_summary")) or paper.get("summary") or item.get("summary")
        records.append({"id": paper["id"], "url": f"https://huggingface.co/papers/{paper['id']}",
                        "published": str(paper.get("publishedAt") or item.get("publishedAt") or "")[:10],
                        "title": _clean(paper.get("title") or item.get("title")),
                        "summary": _clean(summary)[:SUMMARY_CHARS], "upvotes": paper.get("upvotes") or 0,
                        "github": paper.get("githubRepo"), "stars": paper.get("githubStars")})
    return records


@tool
def hf_daily_papers(limit: int = 30, date: str = "", keyword: str = "") -> str:
    """Hugging Face Daily Papers = what is trending in AI research. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars} sorted by upvotes. `date` is YYYY-MM-DD (empty = latest).
    `keyword` filters title/summary; there is no topic search on this endpoint (use hf_search_papers for a topic)."""
    # PSEUDO-CODE:
    #   GET HF_DAILY_URL params: limit (clamp 1..100) and date (only when given)      (with_retry)
    #   response = list of items {"paper": {id, title, summary, upvotes, githubRepo, githubStars, publishedAt}, ...}
    #   map every item to the record shape above (skip items without paper.id); url = https://huggingface.co/papers/<id>
    #   keyword -> keep records whose title+summary contains it (case-insensitive); sort by upvotes descending
    try:
        params = {"limit": _clamp(limit, 1, 100)}
        if date.strip():
            params["date"] = date.strip()
        records = _hf_records(with_retry(lambda: _send("GET", HF_DAILY_URL, params=params)).json())
        needle = keyword.strip().lower()
        if needle:
            records = [r for r in records if needle in f"{r['title']} {r['summary']}".lower()]
        records.sort(key=lambda r: r["upvotes"], reverse=True)
        return json.dumps(records, ensure_ascii=False) if records else "NO RESULTS"
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@tool
def hf_search_papers(query: str, limit: int = 10) -> str:
    """Search Hugging Face papers by topic. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars}."""
    # PSEUDO-CODE:
    #   GET HF_SEARCH_URL params: q=query, limit (clamp 1..50)                         (with_retry)
    #   same item shape as the daily endpoint; prefer paper["ai_summary"] over paper["summary"] when present
    try:
        if not query.strip():
            return "NO RESULTS"
        params = {"q": query.strip(), "limit": _clamp(limit, 1, 50)}
        records = _hf_records(with_retry(lambda: _send("GET", HF_SEARCH_URL, params=params)).json(),
                              prefer_ai_summary=True)
        return json.dumps(records, ensure_ascii=False) if records else "NO RESULTS"
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


# ---- TODO 4: web search / fetch through the Exa MCP endpoint ----
def _mcp_message(text):
    """The JSON-RPC answer of an MCP call: sent as server-sent events ("data: {...}" lines), sometimes as plain JSON."""
    events = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    for raw in reversed(events):
        message = json.loads(raw)
        if isinstance(message, dict) and ("result" in message or "error" in message):
            return message
    return json.loads(text)


def _exa_rate_limited(result, text):
    """The free tier answers HTTP 200 with a notice as the 'page text' and a flag in result._meta."""
    meta = result.get("_meta") or {}
    flagged = any("ratelimit" in re.sub(r"[^a-z]", "", str(k).lower()) and v for k, v in meta.items())
    return flagged or EXA_RATE_LIMIT_TEXT in text.lower()


def _exa_call(name, arguments):
    """Call one tool of the Exa MCP server over plain HTTP (JSON-RPC "tools/call") and return its text."""
    key = (os.getenv("EXA_API_KEY") or "").strip()
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}

    def call():
        response = _send("POST", EXA_URL, params={"exaApiKey": key} if key else None, headers=EXA_HEADERS,
                         json=payload, timeout=60.0)
        message = _mcp_message(response.text)
        if "error" in message:
            detail = str((message["error"] or {}).get("message") or message["error"])
            if EXA_RATE_LIMIT_TEXT in detail.lower():
                raise RetryableError("Exa rate limit")
            raise RuntimeError(f"Exa MCP error: {detail}")
        result = message.get("result") or {}
        text = "\n".join(part.get("text", "") for part in result.get("content") or [] if part.get("type") == "text")
        if _exa_rate_limited(result, text):
            raise RetryableError("Exa rate limit")
        if result.get("isError"):
            raise RuntimeError(f"Exa tool error: {text}")
        return text.strip()

    return with_retry(call, attempts=6, cap=60.0)  # the free tier stays limited for ~20 s, longer on a shared IP


def _truncate(text):
    return text if len(text) <= PAGE_CHARS else text[:PAGE_CHARS] + "\n[... truncated]"


@tool
def web_search(query: str, objective: str = "", num_results: int = 5) -> str:
    """Search the web (Exa). Describe the ideal page in natural language. Returns clean text of the top results with URLs."""
    # PSEUDO-CODE:
    #   call the MCP tool "web_search_exa" with arguments {query, objective, numResults}
    #       (objective is REQUIRED by Exa: when empty, build one from the query)
    #   see GUIDE.md part 1.4 for how to call an MCP server over plain HTTP (JSON-RPC "tools/call") and read the answer
    #   read optional env EXA_API_KEY; when present it is sent to the Exa endpoint.
    #       (see GUIDE.md 1.4 for where it goes) => the key then appears in exception text: redact it before returning "ERROR: ..."
    #   WATCH OUT: read GUIDE.md 1.4 about how Exa signals "rate limited" on the free tier, and retry on it
    try:
        if not query.strip():
            return "NO RESULTS"
        arguments = {"query": query.strip(), "numResults": _clamp(num_results, 1, 10),
                     "objective": objective.strip() or f"Find authoritative, recent pages about: {query.strip()}"}
        return _truncate(_exa_call("web_search_exa", arguments)) or "NO RESULTS"
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@tool
def web_fetch(url: str) -> str:
    """Read the full content of one web page (e.g. an arXiv abstract page) as markdown. Long pages are truncated."""
    # PSEUDO-CODE: MCP tool "web_fetch_exa" with arguments {"urls": [url]}; truncate the text to ~12000 chars
    try:
        if not url.strip().startswith(("http://", "https://")):
            return "ERROR: url must start with http:// or https://"
        arguments = {"urls": [url.strip()], "maxCharacters": PAGE_CHARS}  # Exa's own default is 3000
        return _truncate(_exa_call("web_fetch_exa", arguments)) or "NO RESULTS"
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


# ---- TODO 5: registry (the researcher subagent gets exactly these) ----
SOURCE_TOOLS = [arxiv_search, hf_daily_papers, hf_search_papers, web_search, web_fetch]


if __name__ == "__main__":
    for name, fn, args in [
        ("arxiv_search", arxiv_search, {"query": "world model", "max_results": 3}),
        ("hf_daily_papers", hf_daily_papers, {"limit": 20}),
        ("hf_search_papers", hf_search_papers, {"query": "world model", "limit": 3}),
        ("web_search", web_search, {"query": "survey paper on world models", "num_results": 2}),
        ("web_fetch", web_fetch, {"url": "https://arxiv.org/abs/1803.10122"}),
    ]:
        try:
            print(f"== {name}\n{fn.invoke(args)[:400]}\n")
        except NotImplementedError as exc:
            print(f"== {name}: not implemented yet ({exc})\n")
