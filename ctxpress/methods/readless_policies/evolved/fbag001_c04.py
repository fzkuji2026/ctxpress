"""Small source windows, with direct access to every omitted implementation."""
from __future__ import annotations

import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.definition import definitions, find
from ctxpress.tools.outline import outline


TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
READ_LINES = 64
READ_CHARS = 4800
MAP_ENTRIES = 20
FOCUS_HITS = 8
_NUMBERED = re.compile(r"^\s*\d+\t")

GUIDANCE = (
    "Use small source reads to answer the next implementation question. For names or behavior across files, "
    "prefer `" + TOOL + " focus 'REGEX' FILE...` over chained sed/cat: it searches case-insensitively and "
    "returns up to 8 matching lines with two lines of context, merged overlaps, and enclosing definition spans. "
    "Use regex alternatives to answer related questions in one call; choose files with rg first if necessary. "
    "Then read only the implementation needed: `" + TOOL + " read FILE NAME` (qualified names work) or "
    "`" + TOOL + " read FILE A:B`. Reads return up to 64 lines/4800 characters and an exact continuation command. "
    "Known locations can be read directly, without a search or preview. `" + TOOL + " read FILE` gives a "
    "small file's source or a large source file's map; `" + TOOL + " map FILE [REGEX] [OFFSET]` filters definitions. "
    "Avoid surveying adjacent chunks or reading every search hit: expand only when missing code affects your "
    "change or verification. Reuse source already read unless it changed. Search windows are partial evidence; "
    "read the relevant body before editing or drawing conclusions about its behavior."
)


def _quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command(path, start, end):
    return TOOL + " read " + _quote(path) + " " + str(start) + ":" + str(end)


def _load(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _excerpt(path, lines, start, end):
    end = min(end, len(lines))
    if start > end:
        return path + ": no lines in requested range (" + str(len(lines)) + " total)."
    output = [path + ":" + str(start) + "-" + str(end)]
    used = len(output[0]) + 1
    next_line = start
    for number in range(start, min(end + 1, start + READ_LINES)):
        raw = lines[number - 1]
        prefix = str(number) + "\t"
        room = READ_CHARS - 650 - used - len(prefix)
        if room < 40:
            break
        before = len(output)
        if len(raw) + 1 > room:
            if number != start:
                break
            output.append(prefix + raw[:room - 40] + " ... [line shortened]")
            output.append("[Full line: sed -n '" + str(number) + "p' " + _quote(path) + "]")
        else:
            output.append(prefix + raw)
        used += sum(len(part) + 1 for part in output[before:])
        next_line = number + 1
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
        output.append("[" + str(remaining) + " more; narrow REGEX or continue: " + TOOL + " map " + _quote(path) + " " + _quote(pattern) + " " + str(offset + len(page)) + "]")
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
    head = [str(i + 1) + "\t" + line[:180] for i, line in enumerate(lines[:6])]
    return ("\n".join(head) + "\n" + _map(path, lines)
            + "\n[Map only; read a needed NAME or A:B with " + TOOL + " read " + _quote(path) + " NAME]")


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
        if len(lines) > READ_LINES and outline(lines):
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
        return path + ": no definition named " + selection + ". Search source: " + TOOL + " focus " + _quote(re.escape(selection)) + " " + _quote(path)
    if len(matches) > 1:
        output = [path + ": ambiguous name; qualify it or read one range:"]
        output.extend(name[:140] + " " + str(start) + ":" + str(end) for name, start, end in matches[:12])
        if len(matches) > 12:
            output.append("[" + str(len(matches) - 12) + " other matches; narrow the name.]")
        return "\n".join(output)
    name, start, end = matches[0]
    for unused in range(8):
        if start <= 1 or not re.match(r"\s*(?://|#|/\*|\*|\*/)", lines[start - 2]):
            break
        start -= 1
    return _excerpt(path, lines, start, end)


def focus(argv):
    usage = "Usage: " + TOOL + " focus REGEX FILE... [--offset N] (up to 12 files)"
    if len(argv) < 2:
        return usage
    args = list(argv)
    offset = 0
    if len(args) >= 4 and args[-2] == "--offset":
        try:
            offset = int(args[-1])
        except ValueError:
            return "OFFSET must be nonnegative."
        args = args[:-2]
    if offset < 0:
        return "OFFSET must be nonnegative."
    if not 2 <= len(args) <= 13:
        return usage
    try:
        matcher = re.compile(args[0], re.IGNORECASE)
    except re.error as error:
        return "Invalid focus regex: " + str(error)
    # Scan all requested files to report a truthful count, but retain only one page.
    total = 0
    groups = []
    errors = []
    for path in args[1:]:
        try:
            lines = _load(path)
        except OSError as error:
            errors.append(path + ": " + str(error))
            continue
        page = []
        for number, line in enumerate(lines, 1):
            if matcher.search(line):
                if offset <= total < offset + FOCUS_HITS:
                    page.append(number)
                total += 1
        if page:
            # Definition parsing is only needed for files represented on this page.
            defs = definitions(path, lines)
            windows = []
            for number in page:
                start, end = max(1, number - 2), min(len(lines), number + 2)
                adjacent = windows and start <= windows[-1][1] + 1
                if adjacent and sum(min(len(raw), 285) + 10 for raw in lines[windows[-1][0] - 1:end]) <= 2200:
                    windows[-1][1] = end
                    windows[-1][2].append(number)
                else:
                    windows.append([start, end, [number]])
            groups.append((path, lines, defs, windows))
    output = [str(total) + " matching lines; showing source windows (> marks matches)."]
    output.extend(errors)
    used = sum(len(line) + 1 for line in output)
    shown = 0
    clipped = False
    stopped = False
    for path, lines, defs, windows in groups:
        for start, end, hits in windows:
            block = [path + ":" + str(start) + "-" + str(end)]
            owners = []
            for hit in hits:
                enclosing = [d for d in defs if d[1] <= hit <= d[2]]
                if enclosing:
                    name, first, last = min(enclosing, key=lambda d: d[2] - d[1])
                    label = name[:100] + " " + str(first) + ":" + str(last)
                    if label not in owners:
                        owners.append(label)
            if owners:
                block.append("  in " + "; ".join(owners))
            shortened = False
            for number in range(start, end + 1):
                raw = lines[number - 1]
                if len(raw) > 260:
                    raw = raw[:260] + " ... [line shortened]"
                    shortened = True
                block.append(str(number) + ("> " if number in hits else "  ") + raw)
            size = sum(len(line) + 1 for line in block)
            if used + size > READ_CHARS - 700:
                stopped = True
                break
            output.extend(block)
            used += size
            shown += len(hits)
            clipped = clipped or shortened
        if stopped:
            break
    if total > offset + shown:
        output.append("[More matches; narrow REGEX or continue: " + TOOL + " focus "
                      + " ".join(_quote(arg) for arg in args) + " --offset " + str(offset + shown) + "]")
    elif not shown:
        output.append("[No matches on this page.]")
    if clipped:
        output.append("[Shortened lines: use " + TOOL + " read FILE N:N for the full line.]")
    output.append("[Read a needed definition with " + TOOL + " read FILE NAME, or its A:B span.]")
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
    if outline(lines):
        result = _overview(path, lines)
    else:
        result = _excerpt(path, lines, 1, len(lines))
    return result if len(result) < len(out) else None


TOOLS = {"read": read, "map": map_file, "focus": focus}


if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
