"""Small, expandable source reads and searches under a shared output budget."""
from __future__ import annotations

import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.definition import definitions, find
from ctxpress.tools.outline import outline


TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
READ_LINES = 64
READ_CHARS = 4800
MAP_ENTRIES = 18
SEARCH_MATCHES = 8
_NUMBERED = re.compile(r"^\s*\d+\t")

GUIDANCE = (
    "Keep reads tied to the next decision; unused source costs context on every request. "
    "Use `" + TOOL + " search FILE 'REGEX'` to locate relevant names with nearby source and enclosing definition spans "
    "(8 matching lines/page; optional final OFFSET). This often answers a question without a separate file read. "
    "For implementation use `" + TOOL + " read FILE NAME` (qualified names work), or `read FILE A:B`; "
    "several names/ranges may follow FILE, sharing one 64-line/4800-character budget. "
    "Prefer these to chained sed/cat surveys. If you know the location, read it directly. "
    "`" + TOOL + " map FILE [REGEX] [OFFSET]` lists 18 declaration spans/page; "
    "`read FILE` shows a short file or a map of a long source file. "
    "Use rg to find candidate files, then search/read only the relevant ones. "
    "Expand omitted code when needed to implement or verify the change, not automatically; exact continuation ranges are given. "
    "Reuse earlier reads unless the file changed. Explicit sed ranges remain available for full text or very long lines."
)


def _quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command(path, selection):
    return TOOL + " read " + _quote(path) + " " + selection


def _load(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _excerpt(path, lines, start, end, limit=READ_LINES, chars=READ_CHARS):
    """Return source plus an explicit continuation, and the number of lines shown."""
    end = min(end, len(lines))
    if start > end:
        return path + ": no lines in requested range (" + str(len(lines)) + " total).", 0
    output = [path + ":" + str(start) + "-" + str(end)]
    used = len(output[0]) + 1
    next_line = start
    shortened = []
    for number in range(start, min(end + 1, start + limit)):
        raw = lines[number - 1]
        prefix = str(number) + "\t"
        room = chars - 550 - used - len(prefix)
        if room < 60:
            break
        if len(raw) + 1 > room:
            if number != start:
                break
            output.append(prefix + raw[:max(1, room - 30)] + " ... [line shortened]")
            shortened.append(number)
        else:
            output.append(prefix + raw)
        used += len(output[-1]) + 1
        next_line = number + 1
    for number in shortened:
        output.append("[Full line: sed -n '" + str(number) + "p' " + _quote(path) + "]")
    if next_line <= end:
        output.append("[" + str(end - next_line + 1) + " lines omitted: " +
                      _command(path, str(next_line) + ":" + str(end)) + "]")
    return "\n".join(output), next_line - start


def _map(path, lines, pattern="", offset=0):
    entries = outline(lines)
    if pattern:
        try:
            matcher = re.compile(pattern, re.IGNORECASE)
        except re.error as error:
            return "Invalid map regex: " + str(error)
        entries = [entry for entry in entries if matcher.search(entry[3])]
    if not entries:
        return (path + " (" + str(len(lines)) + " lines): no matching declarations. " +
                "Read a range: " + _command(path, "1:" + str(min(READ_LINES, len(lines)))))
    page = entries[offset:offset + MAP_ENTRIES]
    output = [path + " (" + str(len(lines)) + " lines; " + str(len(entries)) + " matching declarations)"]
    for start, end, indent, declaration in page:
        text = declaration if len(declaration) <= 150 else declaration[:147] + "..."
        output.append(str(start) + ":" + str(end) + " " + text)
    remaining = len(entries) - offset - len(page)
    if remaining > 0:
        output.append("[" + str(remaining) + " more; narrow REGEX or continue: " + TOOL + " map " +
                      _quote(path) + " " + _quote(pattern) + " " + str(offset + len(page)) + "]")
    elif not page:
        output.append("[End of map.]")
    output.append("[Read a needed span: " + _command(path, "A:B") + "]")
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
    if not 1 <= len(argv) <= 9:
        return "Usage: " + TOOL + " read FILE [NAME|A:B ...] (up to 8 selections; one shared budget)"
    path = argv[0]
    try:
        lines = _load(path)
    except OSError as error:
        return path + ": " + str(error)
    if not lines:
        return path + ": empty file"
    if len(argv) == 1:
        if len(lines) > READ_LINES and outline(lines):
            return _map(path, lines)
        return _excerpt(path, lines, 1, len(lines))[0]
    spans, errors = [], []
    defs = None
    for selection in argv[1:]:
        span = re.fullmatch(r"(\d+)(?:[:-](\d*))?", selection)
        if span:
            start = int(span.group(1))
            end = int(span.group(2)) if span.group(2) else start + READ_LINES - 1
            if start < 1 or end < start:
                errors.append("Invalid range " + selection[:60] + ": use positive inclusive A:B.")
            else:
                spans.append((start, end))
            continue
        if defs is None:
            defs = definitions(path, lines)
        matches = find(defs, selection)
        if not matches:
            errors.append("No definition " + selection[:100] + "; locate text with " + TOOL + " search " +
                          _quote(path) + " " + _quote(re.escape(selection[:100])))
        elif len(matches) > 1:
            errors.append("Ambiguous " + selection[:100] + "; qualify or read a range: " +
                          "; ".join(name[:80] + " " + str(start) + ":" + str(end)
                                    for name, start, end in matches[:8]) +
                          ("; more matches omitted" if len(matches) > 8 else ""))
        else:
            name, start, end = matches[0]
            for unused in range(6):
                if start <= 1 or not re.match(r"\s*(?://|#|/\*|\*|\*/)", lines[start - 2]):
                    break
                start -= 1
            spans.append((start, end))
    # Coalesce overlapping selections so batching does not print the same source twice.
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    output = []
    remaining_lines, remaining_chars = READ_LINES, READ_CHARS
    for index, (start, end) in enumerate(merged):
        if remaining_lines <= 0 or remaining_chars < 750:
            rest = " ".join(str(a) + ":" + str(b) for a, b in merged[index:])
            output.append("[Unshown selections: " + _command(path, rest) + "]")
            break
        text, count = _excerpt(path, lines, start, end, remaining_lines, remaining_chars)
        output.append(text)
        remaining_lines -= count
        remaining_chars -= len(text) + 1
    if errors:
        output.extend(errors)
    return "\n".join(output)


def search(argv):
    if not 2 <= len(argv) <= 3:
        return "Usage: " + TOOL + " search FILE REGEX [OFFSET] (matching lines, 1 line of context, enclosing spans)"
    path, pattern = argv[:2]
    try:
        matcher = re.compile(pattern)
        offset = int(argv[2]) if len(argv) > 2 else 0
        if offset < 0:
            return "OFFSET must be nonnegative."
        lines = _load(path)
    except (OSError, ValueError, re.error) as error:
        return path + ": " + str(error)
    hits = [i for i, line in enumerate(lines) if matcher.search(line)]
    if not hits:
        return path + ": no matching lines (case-sensitive regex)."
    page = hits[offset:offset + SEARCH_MATCHES]
    output = [path + " (" + str(len(lines)) + " lines; " + str(len(hits)) + " matching lines)"]
    if not page:
        return output[0] + "\n[End of matches.]"
    # Small, deduplicated windows reveal uses without dumping their definitions.
    defs = definitions(path, lines)
    shown_owners, shown_lines = set(), set()
    clipped = []
    used, processed = len(output[0]) + 1, 0
    for hit in page:
        block, block_clipped = [], []
        owners = [(name, a, b) for name, a, b in defs if a <= hit + 1 <= b]
        owner = min(owners, key=lambda d: d[2] - d[1]) if owners else None
        if owner is not None and owner not in shown_owners:
            name, a, b = owner
            block.append("[in " + name[:100] + " " + str(a) + ":" + str(b) + "]")
        numbers = [i for i in range(max(0, hit - 1), min(len(lines), hit + 2))
                   if i not in shown_lines]
        if numbers and shown_lines and numbers[0] > max(shown_lines) + 1:
            block.append("--")
        for i in numbers:
            raw = lines[i]
            match = matcher.search(raw)
            if len(raw) > 240:
                # Center long matching lines on the hit instead of losing it in the prefix.
                left = max(0, match.start() - 70) if match else 0
                left = min(left, max(0, len(raw) - 220))
                raw = ("..." if left else "") + raw[left:left + 220] + " ... [shortened]"
                block_clipped.append(i + 1)
            block.append(str(i + 1) + ("> " if match else "  ") + raw)
        cost = sum(len(line) + 1 for line in block)
        if processed and used + cost > READ_CHARS - 850:
            break
        output.extend(block)
        used += cost
        processed += 1
        shown_lines.update(numbers)
        if owner is not None:
            shown_owners.add(owner)
        clipped.extend(block_clipped)
    next_offset = offset + processed
    if next_offset < len(hits):
        output.append("[" + str(len(hits) - next_offset) + " more matches: " + TOOL + " search " +
                      _quote(path) + " " + _quote(pattern) + " " + str(next_offset) + "]")
    if clipped:
        output.append("[Shortened lines; full text: sed -n '" +
                      ";".join(str(n) + "p" for n in clipped) + "' " + _quote(path) + "]")
    output.append("[Read needed source/enclosing spans: " + _command(path, "A:B") + "]")
    return "\n".join(output)


def view(cmd, out, ctx):
    target = read_target(cmd)
    if target is None or not re.match(r"^\s*(?:cat|nl\s+-ba)\s", cmd):
        return None
    path, first, numbered = target
    if path.replace("\\", "/").rsplit("/", 1)[-1].upper() in ("AGENTS.MD", "CLAUDE.MD", "SKILL.MD"):
        return None
    if len(out) <= READ_CHARS and len(out.splitlines()) <= READ_LINES:
        return None
    lines = out.splitlines()
    if numbered:
        lines = [_NUMBERED.sub("", line) for line in lines]
    if outline(lines):
        head = [str(i + 1) + "\t" + line[:160] for i, line in enumerate(lines[:4])]
        result = "\n".join(head) + "\n" + _map(path, lines)
        result += "\n[Whole-file print shortened; read a needed definition or range.]"
    else:
        result = _excerpt(path, lines, 1, len(lines), 40)[0]
    return result if len(result) < len(out) else None


TOOLS = {"read": read, "map": map_file, "search": search}


if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
