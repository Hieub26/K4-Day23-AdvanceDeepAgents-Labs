"""check_citations.py - STUDENT IMPLEMENTS `check`.   Runs INSIDE the sandbox (standard library only).

research.py uploads this file to the sandbox and the lead agent runs it with the `execute` tool:
    python3 /tmp/work/research/check_citations.py [report.md] [sources.json]
It must exit 0 and print "OK: ..." when the report is consistent, else print each problem and exit 1.
"""
import json
import re
import sys

REPORT = "/tmp/work/report/report.md"
SOURCES = "/tmp/work/research/sources.json"

_GROUP = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\](?!\()")   # [3]  [1, 2]  [1-3]; not a Markdown link [3](url)
_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_REF_HEADING = re.compile(r"(?m)^##[ \t]+References[ \t]*$")
_REF_LINE = re.compile(r"^\s*\[(\d+)\]")
_URL = re.compile(r"https?://\S+")


def _group_numbers(group):
    """Expand the inside of a citation: "3" -> [3], "1, 2" -> [1, 2], "1-3" -> [1, 2, 3]."""
    numbers = []
    for part in re.split(r"\s*,\s*", group):
        span = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", part)
        if span:
            a, b = int(span.group(1)), int(span.group(2))
            numbers.extend(range(a, b + 1) if 0 <= b - a <= 200 else [a, b])
        else:
            numbers.append(int(part))
    return numbers


def _cited_numbers(body):
    """Numbers cited as [n] in the body, ignoring code spans/blocks and Markdown links."""
    cited = set()
    for i, segment in enumerate(_CODE.split(body)):
        if i % 2:                                             # odd indexes are code
            continue
        for match in _GROUP.finditer(segment):
            cited.update(_group_numbers(match.group(1)))
    return cited


def check(report_text, sources):
    """Return a list of problem strings (empty list = OK).

    PSEUDO-CODE:
      problems = []
      if sources is empty: return ["no sources in sources.json"]
      for each source entry:
          n must be an int                       -> problem if not
          url must start with http:// or https://-> problem if not
          the same url must not appear twice     -> problem if duplicated
      split report_text at the heading "## References":
          body = text before it; if the heading is missing -> problem
      cited = set of numbers found as [n] in the BODY only (not in the reference list; use a regex)
      every number in `cited` must exist in sources -> problem "[n] cited but missing from sources.json"
      every source number must be in `cited`        -> problem "source [n] never cited"
      the lines of the References section that start with "[n]" (regex) are the reference lines:
          every source needs exactly ONE reference line (none missing, no number twice, no number that is not a source)
          each reference line holds exactly ONE http(s) URL and it must equal that source's url
          (a line bundling several sources under one number is a problem)
      return problems
    """
    if not isinstance(sources, list) or not all(isinstance(entry, dict) for entry in sources):
        return ["sources.json must be a JSON list of objects"]
    if not sources:
        return ["no sources in sources.json"]
    problems = []
    url_of, seen_urls = {}, set()
    for entry in sources:
        n, url = entry.get("n"), entry.get("url")
        if not isinstance(n, int) or isinstance(n, bool):
            problems.append(f"source n={n!r} is not an integer")
        elif n in url_of:
            problems.append(f"source number [{n}] appears twice in sources.json")
        else:
            url_of[n] = url
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            problems.append(f"source [{n}] url {url!r} does not start with http:// or https://")
        elif url in seen_urls:
            problems.append(f"source [{n}] duplicates url {url}")
        else:
            seen_urls.add(url)

    headings = list(_REF_HEADING.finditer(report_text))
    if headings:
        body, references = report_text[:headings[-1].start()], report_text[headings[-1].end():]
    else:
        body, references = report_text, ""
        problems.append('report has no "## References" heading')

    cited = _cited_numbers(body)
    for n in sorted(cited - set(url_of)):
        problems.append(f"[{n}] cited but missing from sources.json")
    for n in sorted(set(url_of) - cited):
        problems.append(f"source [{n}] never cited")

    if headings:
        listed = set()
        for line in references.splitlines():
            match = _REF_LINE.match(line)
            if not match:
                continue
            n = int(match.group(1))
            if n in listed:
                problems.append(f"reference [{n}] is listed more than once")
                continue
            listed.add(n)
            if n not in url_of:
                problems.append(f"reference [{n}] is not a source in sources.json")
                continue
            urls = _URL.findall(line)
            if len(urls) != 1:
                problems.append(f"reference [{n}] must hold exactly one URL, found {len(urls)}")
            elif url_of[n] not in (urls[0], urls[0].rstrip(".,;")):
                problems.append(f"reference [{n}] url {urls[0]} does not match sources.json ({url_of[n]})")
        for n in sorted(set(url_of) - listed):
            problems.append(f"source [{n}] has no line in the References section")
    return problems


def main(argv):
    report_path = argv[1] if len(argv) > 1 else REPORT
    sources_path = argv[2] if len(argv) > 2 else SOURCES
    try:
        with open(report_path, encoding="utf-8") as f:
            report = f.read()
        with open(sources_path, encoding="utf-8") as f:
            sources = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"cannot read inputs: {exc}")
        return 1
    problems = check(report, sources)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"OK: {len(sources)} sources, all citations resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
