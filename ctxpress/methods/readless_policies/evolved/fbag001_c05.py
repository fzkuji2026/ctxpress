"""Small source windows with explicit expansion and search-centered navigation."""
from __future__ import annotations

import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.definition import definitions, find
from ctxpress.tools.outline import outline


TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
READ_LINES = 60
READ_CHARS = 4600
MAP_ENTRIES = 16
_NUMBERED = re.compile(r"^\s*\d+\t")

GUIDANCE = (
    "Keep reads tied to the next change or verification. For locating behavior, prefer `" + TOOL +
    " near FILE 'REGEX'`: it shows matching lines with nearby source and enclosing definition spans. "
    "Use regex alternatives to answer related questions in one search; choose a file using rg first. "
    "For implementation, use `" + TOOL + " read FILE NAME` (qualified names work) or `" + TOOL +
    " read FILE A:B`. Reads return actual source, at most 60 lines / 4600 characters; "
    "omissions have exact continuation commands. Read additional code whenever dependencies, invariants, "
    "or tests require it; do not expand merely to complete a file. "
    "If the location is known, read it directly. `" + TOOL +
    " read FILE` gives a compact map for source files, and `" + TOOL +
    " map FILE [REGEX] [OFFSET]` filters/pages the map. "
    "Prefer these focused tools over cat or chained sed windows that collect unrelated implementations. "
    "Do not walk a file in consecutive chunks or survey parallel implementations unless comparison is needed. "
    "Reuse source already read unless it changed. Explicit sed ranges remain available for full required text."
)


def _quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command(path, start, end):
    return TOOL + " read " + _quote(path) + " " + str(start) + ":" + str(end)


def _load(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _windows(path, lines, spans, limit=READ_LINES):
    """One shared budget, including when search windows overlap."""
    output = [path + " (" + str(len(lines)) + " lines)"]
    used = len(output[0]) + 1
    count = 0
    last = 0
    for start, end, label in spans:
        start = max(1, start, last + 1)
        end = min(end, len(lines))
        if start > end:
            continue
        if label:
            header = "[" + label[:200] + "]"
            output.append(header)
            used += len(header) + 1
        next_line = start
        for number in range(start, end + 1):
            prefix = str(number) + "\t"
            raw = lines[number - 1]
            room = READ_CHARS - used - len(prefix) - 1
            if count >= limit or room < 80:
                break
            if len(raw) > room:
                if count:
                    break
                source = prefix + raw[:max(0, room - 30)] + " ... [line shortened]"
                output.append(source)
                used += len(source) + 1
                output.append("[Full line: sed -n '" + str(number) + "p' " + _quote(path) + "]")
                used += len(output[-1]) + 1
            else:
                output.append(prefix + raw)
                used += len(output[-1]) + 1
            count += 1
            next_line = number + 1
        if next_line <= end:
            output.append("[Omitted " + str(next_line) + "-" + str(end) + ": " + _command(path, next_line, end) + "]")
        last = end
    return "\n".join(output)


def _map(path, lines, pattern="", offset=0):
    entries = outline(lines)
    if pattern:
        matcher = re.compile(pattern, re.IGNORECASE)
        entries = [entry for entry in entries if matcher.search(entry[3])]
    if not entries:
        if pattern:
            return path + ": no matching definitions. Search text with " + TOOL + " near " + _quote(path) + " " + _quote(pattern)
        return _windows(path, lines, [(1, len(lines), "")], 40)
    page = entries[offset:offset + MAP_ENTRIES]
    output = [path + " (" + str(len(lines)) + " lines; " + str(len(entries)) + " definitions" + (" matching regex" if pattern else "") + ")"]
    output.extend(str(start) + "-" + str(end) + " " + text[:140] for start, end, indent, text in page)
    if offset + len(page) < len(entries):
        output.append("[More: " + TOOL + " map " + _quote(path) + " " + _quote(pattern) + " " + str(offset + len(page)) + "]")
    elif not page:
        output.append("[End of map.]")
    output.append("[Source: " + TOOL + " read " + _quote(path) + " NAME or A:B]")
    return "\n".join(output)


def map_file(argv):
    if not 1 <= len(argv) <= 3:
        return "Usage: " + TOOL + " map FILE [REGEX] [OFFSET]"
    path = argv[0]
    try:
        offset = int(argv[2]) if len(argv) > 2 else 0
        if offset < 0:
            return "OFFSET must be nonnegative."
        return _map(path, _load(path), argv[1] if len(argv) > 1 else "", offset)
    except (OSError, ValueError, re.error) as error:
        return path + ": " + str(error)


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
        return _map(path, lines)
    selection = argv[1]
    span = re.fullmatch(r"(\d+)(?:[:-](\d*))?", selection)
    if span:
        start = int(span.group(1))
        end = int(span.group(2)) if span.group(2) else start + READ_LINES - 1
        if start < 1 or end < start:
            return "Use a positive inclusive range A:B with B >= A."
        if start > len(lines):
            return path + ": range starts beyond end (" + str(len(lines)) + " lines)."
        return _windows(path, lines, [(start, end, "")])
    matches = find(definitions(path, lines), selection)
    if not matches:
        return path + ": no definition named " + selection + ". Search text: " + TOOL + " near " + _quote(path) + " " + _quote(re.escape(selection))
    if len(matches) > 1:
        output = [path + ": ambiguous name; qualify it or read a range:"]
        output.extend(name[:150] + " " + str(start) + ":" + str(end) for name, start, end in matches[:10])
        if len(matches) > 10:
            output.append("[More matches omitted; qualify the name.]")
        return "\n".join(output)
    name, start, end = matches[0]
    # Keep adjacent comments and annotations with the implementation.
    for unused in range(6):
        if start <= 1 or not re.match(r"\s*(?://|#|/\*|\*|\*/|@)", lines[start - 2]):
            break
        start -= 1
    return _windows(path, lines, [(start, end, name + " " + str(start) + "-" + str(end))])


def near(argv):
    if not 2 <= len(argv) <= 3:
        return "Usage: " + TOOL + " near FILE REGEX [OFFSET] (2 lines before, 3 after; 6 hits/page)"
    path, pattern = argv[:2]
    try:
        offset = int(argv[2]) if len(argv) > 2 else 0
        if offset < 0:
            return "OFFSET must be nonnegative."
        matcher = re.compile(pattern)
        lines = _load(path)
        hits = [i + 1 for i, line in enumerate(lines) if matcher.search(line)]
        if not hits:
            return path + ": no matching lines."
        page = hits[offset:offset + 6]
        if not page:
            return path + ": end of matches (" + str(len(hits)) + " matching lines)."
        entries = outline(lines)
        spans = []
        for number in page:
            start, end = max(1, number - 2), min(len(lines), number + 3)
            if spans and start <= spans[-1][1] + 1:
                old_start, old_end, label = spans[-1]
                spans[-1] = (old_start, max(old_end, end), label)
                continue
            scopes = [entry for entry in entries if entry[0] <= number <= entry[1]]
            label = "match " + str(number)
            if scopes:
                a, b, indent, signature = max(scopes, key=lambda entry: (entry[2], entry[0]))
                label += "; scope " + str(a) + "-" + str(b) + " " + signature[:140]
            spans.append((start, end, label))
        result = _windows(path, lines, spans)
        result += "\n[" + str(len(hits)) + " matching lines; hits " + str(offset + 1 if page else 0) + "-" + str(offset + len(page)) + "]"
        if offset + len(page) < len(hits):
            result += "\n[More hits: " + TOOL + " near " + _quote(path) + " " + _quote(pattern) + " " + str(offset + len(page)) + "]"
        return result
    except (OSError, ValueError, re.error) as error:
        return path + ": " + str(error)


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
    if outline(lines):
        head = "\n".join(str(i + 1) + "\t" + line[:160] for i, line in enumerate(lines[:4]))
        result = head + "\n" + _map(path, lines)
    else:
        result = _windows(path, lines, [(1, len(lines), "")], 40)
    result += "\n[Whole-file print shortened; use read NAME/A:B or near REGEX for source.]"
    return result if len(result) < len(out) else None


TOOLS = {"near": near, "read": read, "map": map_file}


if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
