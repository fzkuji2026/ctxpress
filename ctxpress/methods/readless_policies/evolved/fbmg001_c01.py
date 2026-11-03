"""Focused source neighborhoods, with bounded implementation reads on demand."""
from __future__ import annotations

import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.definition import definitions, find
from ctxpress.tools.outline import outline


TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
READ_LINES = 80
READ_CHARS = 5500
MAP_ENTRIES = 24
FOCUS_HITS = 8
_NUMBERED = re.compile(r"^\s*\d+\t")

GUIDANCE = (
    "Use focused source reads to answer the next implementation question. "
    "After locating a file, prefer `" + TOOL + " focus FILE 'name|other_name'` over chained sed/cat: "
    "it searches all lines and returns up to 8 matches with merged nearby source and enclosing definition names. "
    "Combine related search terms in one regex; an optional OFFSET pages through matches. "
    "When you need implementation, use `" + TOOL + " read FILE NAME` (qualified names work) or "
    "`" + TOOL + " read FILE A:B`; these print actual source, up to 80 lines / 5500 characters, "
    "with commands for any omitted text. Read known locations directly; a preview is unnecessary. "
    "For file structure use `" + TOOL + " map FILE [REGEX] [OFFSET]`; read FILE without a selection "
    "gives an overview of a large file. Expand omitted bodies when their behavior matters to the change, "
    "callers, edge cases, or tests; a search neighborhood alone may not establish behavior. "
    "Avoid surveying unrelated bodies or automatically paging to EOF. Reuse source already read unless changed. "
    "Long whole-file prints become overviews; explicit line ranges remain available."
)


def _quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command(path, start, end):
    return TOOL + " read " + _quote(path) + " " + str(start) + ":" + str(end)


def _load(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _merge(spans):
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _excerpts(path, lines, spans, limit=READ_LINES):
    """One shared output budget, including when search neighborhoods are disjoint."""
    output = [path + " (" + str(len(lines)) + " lines)"]
    used = len(output[0]) + 1
    count = 0
    for start, end in _merge(spans):
        end = min(end, len(lines))
        if start > end:
            output.append("[No lines in " + str(start) + ":" + str(end) + "]")
            continue
        output.append("--- " + str(start) + ":" + str(end) + " ---")
        used += len(output[-1]) + 1
        next_line = start
        for number in range(start, end + 1):
            prefix = str(number) + "\t"
            room = READ_CHARS - 1200 - used - len(prefix)
            if count >= limit or room < 80:
                break
            raw = lines[number - 1]
            if len(raw) + 1 > room:
                # Do not get stuck when a single generated line exceeds the budget.
                if count:
                    break
                output.append(prefix + raw[:room - 35] + " ... [line shortened]")
                output.append("[Full line: sed -n '" + str(number) + "p' " + _quote(path) + "]")
            else:
                output.append(prefix + raw)
            used += len(output[-1]) + len(prefix) + len(raw[:room]) + 1 if len(raw) + 1 > room else len(output[-1]) + 1
            count += 1
            next_line = number + 1
        if next_line <= end:
            output.append("[Omitted " + str(next_line) + ":" + str(end) + "; " + _command(path, next_line, end) + "]")
    return "\n".join(output)


def _map(path, lines, pattern="", offset=0):
    entries = outline(lines)
    if pattern:
        try:
            matcher = re.compile(pattern, re.IGNORECASE)
        except re.error as error:
            return "Invalid map regex: " + str(error)
        entries = [entry for entry in entries if matcher.search(entry[3])]
    output = [path + " (" + str(len(lines)) + " lines; " + str(len(entries)) + " matching definitions)"]
    page = entries[offset:offset + MAP_ENTRIES]
    for start, end, indent, text in page:
        output.append(str(start) + "-" + str(end) + " " + text[:150])
    if offset + len(page) < len(entries):
        output.append("[More: " + TOOL + " map " + _quote(path) + " " + _quote(pattern) + " " + str(offset + len(page)) + "]")
    elif not page:
        output.append("[No matching definitions on this page. Search content with " + TOOL + " focus " + _quote(path) + " REGEX]")
    return "\n".join(output)


def _overview(path, lines):
    if len(lines) <= READ_LINES and sum(len(line) + 1 for line in lines) <= READ_CHARS - 1200:
        return _excerpts(path, lines, [(1, len(lines))])
    if not outline(lines):
        return _excerpts(path, lines, [(1, len(lines))], 40)
    head = [str(i + 1) + "\t" + line[:160] for i, line in enumerate(lines[:6])]
    result = "\n".join(head) + "\n" + _map(path, lines)
    return result + "\n[Overview only. Source: " + TOOL + " read " + _quote(path) + " NAME or A:B; content search: " + TOOL + " focus " + _quote(path) + " REGEX]"


def map_file(argv):
    if not 1 <= len(argv) <= 3:
        return "Usage: " + TOOL + " map FILE [REGEX] [OFFSET]"
    path = argv[0]
    try:
        offset = int(argv[2]) if len(argv) > 2 else 0
        if offset < 0:
            return "OFFSET must be nonnegative."
        return _map(path, _load(path), argv[1] if len(argv) > 1 else "", offset)
    except (OSError, ValueError) as error:
        return path + ": " + str(error)


def focus(argv):
    if not 2 <= len(argv) <= 3:
        return "Usage: " + TOOL + " focus FILE REGEX [OFFSET] (3 context lines per side; 8 matches per page)"
    path, pattern = argv[:2]
    try:
        offset = int(argv[2]) if len(argv) > 2 else 0
        if offset < 0:
            return "OFFSET must be nonnegative."
        matcher = re.compile(pattern)
        lines = _load(path)
    except (OSError, ValueError, re.error) as error:
        return path + ": " + str(error)
    hits = [i + 1 for i, line in enumerate(lines) if matcher.search(line)]
    page = hits[offset:offset + FOCUS_HITS]
    if not page:
        return path + ": " + str(len(hits)) + " matching lines; none at offset " + str(offset) + "."
    defs = definitions(path, lines)
    owners = []
    for number in page:
        enclosing = [d for d in defs if d[1] <= number <= d[2]]
        if enclosing:
            owner = min(enclosing, key=lambda d: (d[2] - d[1], -d[1]))
            if owner not in owners:
                owners.append(owner)
    header = ["Matches " + str(offset + 1) + "-" + str(offset + len(page)) + "/" + str(len(hits)) + " at lines " + ",".join(str(n) for n in page)]
    for name, start, end in owners:
        header.append("Enclosing " + name[:140] + " " + str(start) + ":" + str(end))
    spans = [(max(1, n - 3), min(len(lines), n + 3)) for n in page]
    body = _excerpts(path, lines, spans, 64)
    result = "\n".join(header) + "\n" + body
    if offset + len(page) < len(hits):
        result += "\n[More matches: " + TOOL + " focus " + _quote(path) + " " + _quote(pattern) + " " + str(offset + len(page)) + "]"
    result += "\n[For behavior, read enclosing NAME or A:B: " + TOOL + " read " + _quote(path) + " NAME]"
    return result


def read(argv):
    if not 1 <= len(argv) <= 2:
        return "Usage: " + TOOL + " read FILE [A:B|NAME]"
    path = argv[0]
    try:
        lines = _load(path)
    except OSError as error:
        return path + ": " + str(error)
    if not lines:
        return path + ": empty file"
    if len(argv) == 1:
        return _overview(path, lines)
    selection = argv[1]
    span = re.fullmatch(r"(\d+)(?:[:-](\d*))?", selection)
    if span:
        start = int(span.group(1))
        end = int(span.group(2)) if span.group(2) else start + READ_LINES - 1
        if start < 1 or end < start:
            return "Use a positive, inclusive range A:B with B >= A."
        return _excerpts(path, lines, [(start, end)])
    matches = find(definitions(path, lines), selection)
    if not matches:
        return path + ": no definition named " + selection + ". Search: " + TOOL + " focus " + _quote(path) + " " + _quote(re.escape(selection))
    if len(matches) > 1:
        output = [path + ": ambiguous name; qualify it or read one range:"]
        output.extend(name[:160] + " " + str(start) + ":" + str(end) for name, start, end in matches[:12])
        if len(matches) > 12:
            output.append("[" + str(len(matches) - 12) + " other matches; narrow the name.]")
        return "\n".join(output)
    name, start, end = matches[0]
    for unused in range(8):
        if start <= 1 or not re.match(r"\s*(?://|#|/\*|\*|\*/)", lines[start - 2]):
            break
        start -= 1
    return _excerpts(path, lines, [(start, end)])


def view(cmd, out, ctx):
    target = read_target(cmd)
    if target is None or not re.match(r"^\s*(?:cat|nl\s+-ba)\s", cmd):
        return None
    path, first, numbered = target
    if path.replace("\\", "/").rsplit("/", 1)[-1].upper() in ("AGENTS.MD", "CLAUDE.MD", "SKILL.MD"):
        return None
    lines = out.splitlines()
    if len(lines) <= READ_LINES and len(out) <= READ_CHARS:
        return None
    if numbered:
        lines = [_NUMBERED.sub("", line) for line in lines]
    result = _overview(path, lines)
    return result if len(result) < len(out) else None


TOOLS = {"focus": focus, "read": read, "map": map_file}


if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
