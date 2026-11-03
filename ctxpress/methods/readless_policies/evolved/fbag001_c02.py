"""Focused source reads and contextual search with a shared output budget."""
from __future__ import annotations

import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.definition import definitions, find
from ctxpress.tools.outline import outline

TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
READ_LINES = 60
READ_CHARS = 4800
MAP_ENTRIES = 20
SEARCH_HITS = 12
_NUMBERED = re.compile(r"^\s*\d+\t")

GUIDANCE = (
    "Keep reads tied to the next implementation decision. For exploration, start with `" + TOOL +
    " search 'NAME|OTHER_NAME' FILE [FILE ...]`: one shared budget returns matching source, nearby lines, "
    "and enclosing definition spans across up to 8 files. It often answers the question directly; "
    "read more only when the omitted code matters. Prefer this to chained sed/cat surveys. "
    "When you know the code you need, use `" + TOOL + " read FILE NAME` (qualified names work) or "
    "`" + TOOL + " read FILE A:B` directly: actual source, up to 60 lines/4800 characters, with continuation. "
    "Do not automatically follow continuations or batch successive pages. Choose the relevant symbol/range. "
    "`" + TOOL + " map FILE [REGEX] [OFFSET]` lists definition spans, 20 at a time; "
    "`read FILE` gives a brief overview of large source files. Search supports regex alternation for several "
    "questions at once. Reuse source already read unless changed. Read the full relevant implementation "
    "before editing when snippets leave behavior uncertain; explicit sed ranges remain available."
)


def _quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command(path, start, end):
    return TOOL + " read " + _quote(path) + " " + str(start) + ":" + str(end)


def _load(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _excerpt(path, lines, start, end, limit=READ_LINES):
    end = min(end, len(lines))
    if start > end:
        return path + ": no lines in requested range (" + str(len(lines)) + " total)."
    output = [path + ":" + str(start) + "-" + str(end)]
    used = len(output[0]) + 1
    next_line = start
    for number in range(start, min(end + 1, start + limit)):
        raw = lines[number - 1]
        prefix = str(number) + "\t"
        room = READ_CHARS - 750 - used - len(prefix)
        if room < 40:
            break
        block_start = len(output)
        if len(raw) + 1 > room:
            if number != start:
                break
            output.append(prefix + raw[:room - 30] + " ... [line clipped]")
            output.append("[Full line: sed -n '" + str(number) + "p' " + _quote(path) + "]")
        else:
            output.append(prefix + raw)
        used += sum(len(item) + 1 for item in output[block_start:])
        next_line = number + 1
    if next_line <= end:
        output.append("[" + str(end - next_line + 1) + " lines omitted; if needed: " + _command(path, next_line, end) + "]")
    return "\n".join(output)


def _map(path, lines, pattern="", offset=0):
    entries = outline(lines)
    if pattern:
        try:
            matcher = re.compile(pattern, re.IGNORECASE)
        except re.error as error:
            return "Invalid map regex: " + str(error)
        entries = [entry for entry in entries if matcher.search(entry[3])]
    if not entries:
        return path + " (" + str(len(lines)) + " lines): no matching definitions; use search or " + _command(path, 1, min(READ_LINES, len(lines)))
    page = entries[offset:offset + MAP_ENTRIES]
    output = [path + " (" + str(len(lines)) + " lines; " + str(len(entries)) + " matching definitions)"]
    for start, end, indent, text in page:
        output.append(str(start) + "-" + str(end) + " " + text[:150])
    remaining = len(entries) - offset - len(page)
    if remaining > 0:
        output.append("[" + str(remaining) + " more; filter REGEX or continue: " + TOOL + " map " + _quote(path) + " " + _quote(pattern) + " " + str(offset + len(page)) + "]")
    elif not page:
        output.append("[End of map.]")
    return "\n".join(output)


def _overview(path, lines):
    if not outline(lines):
        return _excerpt(path, lines, 1, len(lines), 40)
    head = [str(i + 1) + "\t" + line[:120] for i, line in enumerate(lines[:5])]
    result = "\n".join(head) + "\n" + _map(path, lines)
    return result + "\n[Overview; implementation: " + TOOL + " read " + _quote(path) + " NAME or A:B.]"


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
        if len(lines) <= READ_LINES and sum(len(line) + 1 for line in lines) < READ_CHARS - 750:
            return _excerpt(path, lines, 1, len(lines))
        return _overview(path, lines)
    selection = argv[1]
    span = re.fullmatch(r"(\d+)(?:[:-](\d*))?", selection)
    if span:
        start = int(span.group(1))
        end = int(span.group(2)) if span.group(2) else start + READ_LINES - 1
        if start < 1 or end < start:
            return "Use a positive, inclusive range A:B with B >= A."
        return _excerpt(path, lines, start, end)
    matches = find(definitions(path, lines), selection)
    if not matches:
        return path + ": no definition named " + selection + "; locate it with " + TOOL + " search " + _quote(re.escape(selection.split(".")[-1])) + " " + _quote(path)
    if len(matches) > 1:
        output = [path + ": ambiguous name; qualify it or read one range:"]
        output.extend(name[:150] + " " + str(start) + ":" + str(end) for name, start, end in matches[:12])
        if len(matches) > 12:
            output.append("[" + str(len(matches) - 12) + " other matches; narrow the name.]")
        return "\n".join(output)
    name, start, end = matches[0]
    for unused in range(8):
        if start <= 1 or not re.match(r"\s*(?://|#|/\*|\*|\*/)", lines[start - 2]):
            break
        start -= 1
    return _excerpt(path, lines, start, end)


def search(argv):
    if not 2 <= len(argv) <= 9:
        return "Usage: " + TOOL + " search REGEX FILE [FILE ...] (up to 8 files; quote regexes)"
    try:
        matcher = re.compile(argv[0])
    except re.error as error:
        return "Invalid search regex: " + str(error)
    output = []
    used, shown, total = 0, 0, 0
    for path in argv[1:]:
        try:
            lines = _load(path)
        except OSError as error:
            message = path + ": " + str(error)
            if used + len(message) < READ_CHARS - 650:
                output.append(message)
                used += len(message) + 1
            continue
        hits = [i + 1 for i, line in enumerate(lines) if matcher.search(line)]
        total += len(hits)
        if not hits or shown >= SEARCH_HITS or used >= READ_CHARS - 1100:
            continue
        defs = definitions(path, lines)
        last_end = 0
        for number in hits:
            if shown >= SEARCH_HITS:
                break
            if number <= last_end:
                shown += 1
                continue
            enclosing = [item for item in defs if item[1] <= number <= item[2]]
            owner = min(enclosing, key=lambda item: item[2] - item[1]) if enclosing else None
            start, end = max(1, number - 2), min(len(lines), number + 2)
            if owner:
                start = max(start, owner[1])
                end = min(end, owner[2])
            start = max(start, last_end + 1)
            header = path + ":" + str(start) + "-" + str(end)
            if owner:
                header += " in " + owner[0][:100] + " (" + str(owner[1]) + ":" + str(owner[2]) + ")"
            block = [header]
            clipped = False
            for line_number in range(start, end + 1):
                raw = lines[line_number - 1]
                clipped = clipped or len(raw) > 240
                block.append(str(line_number) + (">\t" if matcher.search(raw) else "\t") + raw[:240] + (" ... [line clipped]" if len(raw) > 240 else ""))
            if clipped:
                block.append("[Full lines: sed -n '" + str(start) + "," + str(end) + "p' " + _quote(path) + "]")
            text = "\n".join(block)
            if used + len(text) > READ_CHARS - 650:
                break
            output.append(text)
            used += len(text) + 1
            last_end = end
            shown += 1
    output.insert(0, str(total) + " matching lines; " + str(shown) + " covered by snippets.")
    if total > shown:
        output.append("[More matches omitted; narrow REGEX/files or use rg -n for all locations.]")
    if total:
        output.append("[Need implementation? " + TOOL + " read FILE NAME or A:B. Enclosing spans above are navigation hints.]")
    return "\n".join(output)


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


TOOLS = {"search": search, "read": read, "map": map_file}


if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
