"""Bound source reads without making a preview a prerequisite for reading code."""
from __future__ import annotations

import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.definition import definitions, find
from ctxpress.tools.outline import outline


TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
READ_LINES = 100
READ_CHARS = 7000
MAP_ENTRIES = 24
_NUMBERED = re.compile(r"^\s*\d+\t")

GUIDANCE = (
    "Keep source reads focused on the next implementation decision: search for the relevant symbol or filename, "
    "then read its implementation and immediate context. Prefer `" + TOOL + " read FILE A:B` to broad/chained "
    "sed or cat reads; `" + TOOL + " read FILE NAME` also reads a definition directly (qualified names work). "
    "Each returns actual source, at most 100 lines / 7000 characters, with an exact command for omitted lines. "
    "Do not expand automatically: expand when the missing code matters to the change or its verification. "
    "If you already know the location, read it directly; no map or preview is required. "
    "For an unfamiliar file, `" + TOOL + " map FILE [REGEX] [OFFSET]` lists up to 24 definition spans; "
    "filter by the names you need instead of surveying several whole files. "
    "Batching many reads still retains all their output in history. Reuse code already read unless it changed. "
    "Long whole-file prints become compact maps; explicit sed ranges remain available for any needed full text."
)


def _quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command(path, start, end):
    return TOOL + " read " + _quote(path) + " " + str(start) + ":" + str(end)


def _load(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _excerpt(path, lines, start, end, limit=READ_LINES):
    """Always identify omissions; leave the rest accessible without repeating this chunk."""
    end = min(end, len(lines))
    if start > end:
        return path + ": no lines in requested range (" + str(len(lines)) + " total)."
    output = [path + ":" + str(start) + "-" + str(end)]
    used = len(output[0]) + 1
    next_line = start
    shortened = []
    for number in range(start, min(end + 1, start + limit)):
        raw = lines[number - 1]
        prefix = str(number) + "\t"
        # Reserve space for omission notices and their expansion commands.
        room = READ_CHARS - 900 - used - len(prefix)
        if room < 40:
            break
        if len(raw) + 1 > room:
            if number != start:
                break
            output.append(prefix + raw[:room - 40] + " ... [line shortened]")
            shortened.append(number)
            used += len(output[-1]) + 1
        else:
            output.append(prefix + raw)
            used += len(prefix) + len(raw) + 1
        next_line = number + 1
    for number in shortened:
        output.append("[Full line " + str(number) + ": sed -n '" + str(number) + "p' " + _quote(path) + "]")
    if next_line <= end:
        output.append("[" + str(end - next_line + 1) + " more lines: " + _command(path, next_line, end) + "]")
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
        return path + " (" + str(len(lines)) + " lines): no matching definitions. Read a range with " + _command(path, 1, min(100, len(lines)))
    page = entries[offset:offset + MAP_ENTRIES]
    output = [path + " (" + str(len(lines)) + " lines; " + str(len(entries)) + " matching definitions)"]
    for start, end, indent, text in page:
        output.append(str(start) + "-" + str(end) + " " + text[:150])
    remaining = len(entries) - offset - len(page)
    if remaining > 0:
        output.append("[" + str(remaining) + " more definitions; narrow REGEX or continue: " + TOOL + " map " + _quote(path) + " " + _quote(pattern) + " " + str(offset + len(page)) + "]")
    elif not page:
        output.append("[End of map.]")
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
    except (OSError, ValueError) as error:
        return path + ": " + str(error)


def read(argv):
    if not 1 <= len(argv) <= 2:
        return "Usage: " + TOOL + " read FILE [A:B|NAME] (one file and one selection per call)"
    path = argv[0]
    try:
        lines = _load(path)
    except OSError as error:
        return path + ": " + str(error)
    if not lines:
        return path + ": empty file"
    selection = argv[1] if len(argv) == 2 else "1:100"
    span = re.fullmatch(r"(\d+)(?:[:-](\d*))?", selection)
    if span:
        start = int(span.group(1))
        end = int(span.group(2)) if span.group(2) else start + READ_LINES - 1
        if start < 1 or end < start:
            return "Use a positive, inclusive range A:B with B >= A."
        result = _excerpt(path, lines, start, end)
        if len(argv) == 1 and len(lines) > READ_LINES:
            result += "\n[File has " + str(len(lines)) + " lines; choose a symbol/range, or continue: " + _command(path, READ_LINES + 1, len(lines)) + "]"
        return result
    matches = find(definitions(path, lines), selection)
    if not matches:
        return path + ": no definition named " + selection + ". Locate it with rg -n -- " + _quote(selection) + " " + _quote(path) + ", then read its line range."
    if len(matches) > 1:
        output = [path + ": ambiguous name; qualify it or read one range:"]
        output.extend(name[:160] + " " + str(start) + ":" + str(end) for name, start, end in matches[:12])
        if len(matches) > 12:
            output.append("[" + str(len(matches) - 12) + " other matches; narrow the name.]")
        return "\n".join(output)
    name, start, end = matches[0]
    # Include adjacent documentation when a language's outline starts at its declaration.
    for unused in range(8):
        if start <= 1 or not re.match(r"\s*(?://|#|/\*|\*|\*/)", lines[start - 2]):
            break
        start -= 1
    return _excerpt(path, lines, start, end)


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
        head = [str(i + 1) + "\t" + line[:180] for i, line in enumerate(lines[:6])]
        result = "\n".join(head) + "\n" + _map(path, lines)
        result += "\n[Whole-file print shortened. Read a needed span: " + TOOL + " read " + _quote(path) + " A:B; or use an explicit sed range.]"
    else:
        result = _excerpt(path, lines, 1, len(lines), 60)
    return result if len(result) < len(out) else None


TOOLS = {"read": read, "map": map_file}


if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
