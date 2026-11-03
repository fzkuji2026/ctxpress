"""Focused source reads, batched selections, and searches with useful context."""
from __future__ import annotations

import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.definition import definitions, find


TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
READ_LINES = 80
READ_CHARS = 6000
MAP_ENTRIES = 20
SEARCH_MATCHES = 12
_NUMBERED = re.compile(r"^\s*\d+\t")

GUIDANCE = (
    "Keep reads tied to the next coding decision. Use rg to locate files, then prefer these focused source tools "
    "over chained sed/cat prints: `" + TOOL + " search FILE 'REGEX' [OFFSET]` shows matching lines with nearby "
    "source and enclosing definition names/spans; use alternation to answer several related questions at once. "
    "If you know a symbol or line range, go straight to `" + TOOL + " read FILE NAME` or `" + TOOL + " read FILE A:B`. "
    "You can give up to six names/ranges after FILE in one read; they share an 80-line/6000-character budget. "
    "A name returns implementation code, including qualified methods; no preview is required before editing. "
    "`" + TOOL + " read FILE` returns a small file whole, or a compact overview for a large one. "
    "`" + TOOL + " map FILE [REGEX] [OFFSET]` lists definition signatures and exact spans; filter to the names "
    "relevant to your change. Read a body when its behavior matters, and callers/tests when they resolve a concrete "
    "question. Follow an omission pointer only when you need the missing code; do not page through files by default. "
    "Reuse source already read unless it changed. Explicit sed ranges remain available for longer uninterrupted reads."
)


def _quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command(path, start, end):
    return TOOL + " read " + _quote(path) + " " + str(start) + ":" + str(end)


def _clip(text, length):
    return text if len(text) <= length else text[:length - 4] + " ..."


def _load(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _excerpt(path, lines, start, end, limit=READ_LINES, budget=READ_CHARS):
    end = min(end, len(lines))
    if start > end:
        return path + ": no lines in requested range (" + str(len(lines)) + " total)."
    output = [path + ":" + str(start) + "-" + str(end)]
    used = len(output[0]) + 1
    next_line = start
    clipped = None
    # Leave space for both a continuation and a full-line escape hatch.
    reserve = 180 + 2 * len(TOOL) + 2 * len(_quote(path))
    for number in range(start, min(end + 1, start + limit)):
        prefix = str(number) + "\t"
        raw = lines[number - 1]
        room = budget - reserve - used - len(prefix) - 1
        if room < 40:
            break
        if len(raw) > room:
            if number != start:
                break
            output.append(prefix + _clip(raw, room - 24) + " [line clipped]")
            clipped = number
        else:
            output.append(prefix + raw)
        used += len(output[-1]) + 1
        next_line = number + 1
    if clipped is not None:
        output.append("[Full line: sed -n '" + str(clipped) + "p' " + _quote(path) + "]")
    if next_line <= end:
        output.append("[" + str(end - next_line + 1) + " lines omitted; next: " + _command(path, next_line, end) + "]")
    return "\n".join(output)


def _signature(lines, start, end):
    # Decorators are included in Python definition spans; show the declaration.
    at = start - 1
    for index in range(at, min(end, at + 12)):
        if re.match(r"\s*(?:async\s+)?(?:def|class)\s", lines[index]):
            at = index
            break
        if not lines[at].lstrip().startswith("@"):
            break
    text = lines[at].strip()
    if re.match(r"(?:async\s+)?(?:def|class)\s", text) and not text.endswith(("{", "}", ";")):
        for index in range(at + 1, min(end, at + 6)):
            if (":" in text and text.count("(") <= text.count(")")) or text.rstrip().endswith("{") or len(text) >= 160:
                break
            text += " " + lines[index].strip()
        header = re.match(r".*?\)\s*(?:->[^:]+)?\s*:", text)
        if header:
            text = header.group(0)
    return _clip(text, 170)


def _map(path, lines, pattern="", offset=0, entries=None, budget=READ_CHARS):
    entries = definitions(path, lines) if entries is None else entries
    if pattern:
        try:
            matcher = re.compile(pattern, re.IGNORECASE)
        except re.error as error:
            return "Invalid map regex: " + str(error)
        entries = [entry for entry in entries if matcher.search(entry[0]) or matcher.search(_signature(lines, entry[1], entry[2]))]
    if not entries:
        return path + " (" + str(len(lines)) + " lines): no matching definitions. Read source: " + _command(path, 1, min(READ_LINES, len(lines)))
    output = [path + " (" + str(len(lines)) + " lines; " + str(len(entries)) + " matching definitions)"]
    used = len(output[0]) + 1
    reserve = 100 + len(TOOL) + len(_quote(path)) + len(_quote(pattern))
    shown = 0
    for name, start, end in entries[offset:offset + MAP_ENTRIES]:
        row = str(start) + ":" + str(end) + " " + _clip(name, 100) + " | " + _signature(lines, start, end)
        if used + len(row) + 1 > budget - reserve:
            break
        output.append(row)
        used += len(row) + 1
        shown += 1
    remaining = len(entries) - offset - shown
    if remaining > 0:
        output.append("[" + str(remaining) + " more; filter REGEX or continue: " + TOOL + " map " + _quote(path) + " " + _quote(pattern) + " " + str(offset + shown) + "]")
    elif not shown:
        output.append("[End of map.]")
    return "\n".join(output)


def _overview(path, lines):
    if len(lines) <= READ_LINES and sum(len(line) + 1 for line in lines) < READ_CHARS - 250:
        return _excerpt(path, lines, 1, len(lines))
    entries = definitions(path, lines)
    if not entries:
        return _excerpt(path, lines, 1, len(lines), 40)
    # Keep opening imports/module documentation visible alongside the map.
    first = min(8, max(0, min(entry[1] for entry in entries) - 1))
    head = [str(i + 1) + "\t" + _clip(line, 160) for i, line in enumerate(lines[:first])]
    footer = "[Overview; bodies and other source omitted. Read a needed NAME or A:B with " + TOOL + " read " + _quote(path) + " NAME|A:B.]"
    budget = READ_CHARS - sum(len(line) + 1 for line in head) - len(footer) - 2
    return "\n".join(head + [_map(path, lines, entries=entries, budget=budget), footer])


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
    if not 1 <= len(argv) <= 7:
        return "Usage: " + TOOL + " read FILE [NAME|A:B ...] (up to six selections; shared output budget)"
    path = argv[0]
    try:
        lines = _load(path)
    except OSError as error:
        return path + ": " + str(error)
    if not lines:
        return path + ": empty file"
    if len(argv) == 1:
        return _overview(path, lines)
    entries = None
    output = []
    count = len(argv) - 1
    for selection in argv[1:]:
        span = re.fullmatch(r"(\d+)(?:[:-](\d*))?", selection)
        if span:
            start = int(span.group(1))
            end = int(span.group(2)) if span.group(2) else start + READ_LINES - 1
            if start < 1 or end < start:
                output.append("Use a positive, inclusive range A:B with B >= A.")
                continue
        else:
            if entries is None:
                entries = definitions(path, lines)
            matches = find(entries, selection)
            if not matches:
                output.append(path + ": no definition named " + _clip(selection, 160) + ". Locate uses with " + TOOL + " search " + _quote(path) + " " + _quote(re.escape(selection)) + ".")
                continue
            if len(matches) > 1:
                choices = ["  " + _clip(name, 120) + " " + str(start) + ":" + str(end) for name, start, end in matches[:6]]
                output.append(path + ": ambiguous name " + _clip(selection, 100) + "; qualify it or read a range:\n" + "\n".join(choices) + ("\n[Other matches omitted; qualify the name.]" if len(matches) > 6 else ""))
                continue
            name, start, end = matches[0]
            for unused in range(6):
                if start <= 1 or not re.match(r"\s*(?://|#|/\*|\*|\*/)", lines[start - 2]):
                    break
                start -= 1
        output.append(_excerpt(path, lines, start, end, max(1, READ_LINES // count), (READ_CHARS - 2 * count) // count))
    return "\n\n".join(output)


def search(argv):
    if not 2 <= len(argv) <= 3:
        return "Usage: " + TOOL + " search FILE REGEX [OFFSET] (matching lines, +/-2 context, enclosing definitions)"
    path, pattern = argv[:2]
    try:
        matcher = re.compile(pattern)
        offset = int(argv[2]) if len(argv) > 2 else 0
        if offset < 0:
            return "OFFSET must be nonnegative."
        lines = _load(path)
    except (OSError, ValueError, re.error) as error:
        return path + ": " + str(error)
    hits = [number for number, line in enumerate(lines, 1) if matcher.search(line)]
    if not hits:
        return path + ": no matching lines."
    entries = definitions(path, lines)
    output = [path + " (" + str(len(hits)) + " matching lines; > marks a match)"]
    used = len(output[0]) + 1
    shown = set()
    hitset = set(hits)
    previous = None
    cursor = min(offset, len(hits))
    reserve = 220 + 2 * len(TOOL) + 2 * len(_quote(path)) + len(_quote(pattern))
    for number in hits[offset:offset + SEARCH_MATCHES]:
        enclosing = [entry for entry in entries if entry[1] <= number <= entry[2]]
        owner = min(enclosing, key=lambda entry: entry[2] - entry[1]) if enclosing else None
        piece = []
        if owner and owner != previous:
            piece.append("in " + _clip(owner[0], 120) + " (" + str(owner[1]) + ":" + str(owner[2]) + ")")
        fresh = [n for n in range(max(1, number - 2), min(len(lines), number + 2) + 1) if n not in shown]
        if fresh and shown and min(fresh) > max(shown) + 1:
            piece.append("...")
        for n in fresh:
            raw = lines[n - 1]
            text = raw if len(raw) <= 260 else raw[:240] + " ... [line clipped]"
            piece.append((">" if n in hitset else " ") + str(n) + "\t" + text)
        block = "\n".join(piece)
        if used + len(block) + 1 > READ_CHARS - reserve:
            break
        if block:
            output.append(block)
        used += len(block) + 1
        shown.update(fresh)
        previous = owner
        cursor += 1
    if cursor < len(hits):
        output.append("[" + str(len(hits) - cursor) + " matching lines remain; narrow REGEX or continue: " + TOOL + " search " + _quote(path) + " " + _quote(pattern) + " " + str(cursor) + "]")
    elif offset >= len(hits):
        output.append("[End of matches.]")
    output.append("[Search context only; full source: " + TOOL + " read " + _quote(path) + " NAME|A:B.]")
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


TOOLS = {"read": read, "map": map_file, "search": search}


if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
