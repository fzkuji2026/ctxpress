"""Focused source reads and search results with enough context to choose a body."""
from __future__ import annotations

import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.definition import definitions, find
from ctxpress.tools.outline import outline


TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
READ_LINES = 64
READ_CHARS = 5200
MAP_ENTRIES = 20
SEARCH_HITS = 8
_NUMBERED = re.compile(r"^\s*\d+\t")

GUIDANCE = (
    "Keep source reads tied to the next change or verification. Use `" + TOOL +
    " search 'REGEX' FILE [FILE ...]` to locate behavior: one call returns matching lines, nearby source, "
    "and enclosing definition names/spans across the named files. Combine related questions with regex |; "
    "this often answers them without a second read. Search shows up to 8 matches, with an explicit next page. "
    "Use rg --files or rg -l to discover paths, then this search for context instead of chained sed/cat surveys. "
    "Once you know what needs understanding, `" + TOOL + " read FILE NAME` reads its actual implementation "
    "(qualified names work); `" + TOOL + " read FILE A:B` reads exact inclusive lines. "
    "Reads show up to 64 lines/5200 characters with a continuation command. Read further when the missing "
    "logic affects your change, callers, edge cases, or tests; do not paginate merely to finish a file. "
    "For structure, `" + TOOL + " map FILE [REGEX] [OFFSET]` lists definition spans; a bare read gives a "
    "compact overview of a long file. Known locations need no preliminary map/search. "
    "Prefer these focused tools to broad or chained sed/cat reads: every printed chunk stays in history. "
    "Reuse source already read unless it changed. Explicit sed ranges remain available for full text."
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
    shortened = []
    for number in range(start, min(end + 1, start + limit)):
        raw = lines[number - 1]
        prefix = str(number) + "\t"
        room = READ_CHARS - 900 - used - len(prefix)
        if room < 40:
            break
        if len(raw) + 1 > room:
            if number != start:
                break
            output.append(prefix + raw[:room - 40] + " ... [line shortened]")
            shortened.append(number)
        else:
            output.append(prefix + raw)
        used += len(output[-1]) + 1
        next_line = number + 1
    for number in shortened:
        output.append("[Full line: sed -n '" + str(number) + "p' " + _quote(path) + "]")
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
        return path + " (" + str(len(lines)) + " lines): no matching definitions. Read source: " + _command(path, 1, min(READ_LINES, len(lines)))
    page = entries[offset:offset + MAP_ENTRIES]
    output = [path + " (" + str(len(lines)) + " lines; " + str(len(entries)) + " matching definitions)"]
    for start, end, indent, text in page:
        output.append(str(start) + "-" + str(end) + " " + text[:140])
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


def _overview(path, lines):
    if not outline(lines):
        return _excerpt(path, lines, 1, len(lines), 32)
    head = [str(i + 1) + "\t" + line[:180] + (" ... [line shortened]" if len(line) > 180 else "")
            for i, line in enumerate(lines[:5])]
    return ("\n".join(head) + "\n" + _map(path, lines) +
            "\n[Overview only. Read a definition or exact range: " + TOOL + " read " + _quote(path) + " NAME|A:B]")


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
        if len(lines) > READ_LINES or sum(len(line) + 1 for line in lines) > READ_CHARS:
            return _overview(path, lines)
        return _excerpt(path, lines, 1, len(lines))
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
        return path + ": no definition named " + selection + ". Find references: " + TOOL + " search " + _quote(re.escape(selection)) + " " + _quote(path)
    if len(matches) > 1:
        output = [path + ": ambiguous name; qualify it or read one range:"]
        output.extend(name[:160] + " " + str(start) + ":" + str(end) for name, start, end in matches[:10])
        if len(matches) > 10:
            output.append("[" + str(len(matches) - 10) + " other matches; narrow the name.]")
        return "\n".join(output)
    name, start, end = matches[0]
    for unused in range(8):
        if start <= 1 or not re.match(r"\s*(?://|#|/\*|\*|\*/)", lines[start - 2]):
            break
        start -= 1
    return _excerpt(path, lines, start, end)


def search(argv):
    """Match lines plus small context, not entire enclosing implementations."""
    usage = "Usage: " + TOOL + " search [--skip N] REGEX FILE [FILE ...]"
    skip = 0
    if argv and argv[0] == "--skip":
        try:
            skip = int(argv[1])
        except (IndexError, ValueError):
            return usage
        argv = argv[2:]
    if len(argv) < 2 or skip < 0:
        return usage
    pattern, paths = argv[0], list(dict.fromkeys(argv[1:]))
    try:
        matcher = re.compile(pattern)
    except re.error as error:
        return "Invalid search regex: " + str(error)
    total = 0
    selected = []
    errors = []
    for path in paths:
        try:
            lines = _load(path)
        except OSError as error:
            errors.append(path + ": " + str(error))
            continue
        hits = [i + 1 for i, line in enumerate(lines) if matcher.search(line)]
        local = max(0, skip - total)
        wanted = hits[local:local + max(0, SEARCH_HITS - len(selected))]
        if wanted:
            defs = definitions(path, lines)
            for number in wanted:
                selected.append((path, lines, defs, number))
        total += len(hits)
    output = [str(total) + " matching lines; context is +/-2 lines, not complete bodies."]
    used = len(output[0]) + 1
    shown = 0
    previous = {}
    for path, lines, defs, number in selected:
        enclosing = [entry for entry in defs if entry[1] <= number <= entry[2]]
        enclosing.sort(key=lambda entry: (entry[2] - entry[1], -entry[1]))
        header = path + ":" + str(number)
        if enclosing:
            name, first, last = enclosing[0]
            header += " in " + name[:120] + " (" + str(first) + ":" + str(last) + ")"
        start, end = max(1, number - 2), min(len(lines), number + 2)
        # Adjacent matches share source lines instead of printing the same context twice.
        start = max(start, previous.get(path, 0) + 1)
        block = [header]
        for line_number in range(start, end + 1):
            raw = lines[line_number - 1]
            block.append(str(line_number) + "\t" + raw[:220] + (" ... [line shortened]" if len(raw) > 220 else ""))
        piece = "\n".join(block)
        if shown and used + len(piece) > READ_CHARS - 650:
            break
        output.append(piece)
        used += len(piece) + 1
        shown += 1
        previous[path] = end
    if errors:
        output.extend(errors[:3])
        if len(errors) > 3:
            output.append("[" + str(len(errors) - 3) + " more unreadable paths.]")
    if not total:
        output.append("No matches in the readable files. Search is case-sensitive; (?i) makes it insensitive.")
    elif not shown:
        output.append("[No matches after offset " + str(skip) + ".]")
    else:
        output.append("[Expand needed code: " + TOOL + " read FILE NAME|A:B]")
    if skip + shown < total:
        output.append("[More matches; narrow REGEX or continue: " + TOOL + " search --skip " + str(skip + shown) + " " + _quote(pattern) + " " + " ".join(_quote(path) for path in paths) + "]")
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
    count = ctx.get("tokens", len)
    return result if len(result) < len(out) and count(result) < count(out) else None


TOOLS = {"read": read, "search": search, "map": map_file}


if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
