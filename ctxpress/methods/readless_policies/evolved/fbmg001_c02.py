"""Focused, bounded source reads with search context and explicit continuations."""
from __future__ import annotations

import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.definition import definitions, find
from ctxpress.tools.outline import outline


TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
READ_LINES = 72
READ_CHARS = 5200
MAP_ENTRIES = 20
_NUMBERED = re.compile(r"^\s*\d+\t")

GUIDANCE = (
    "For source inspection, prefer these focused commands over cat or chained sed ranges. "
    "Once you have located a relevant file with rg, use `" + TOOL + " search FILE 'REGEX'` to see "
    "matching source with two surrounding lines and enclosing definition names/spans. This often answers "
    "usage, configuration and call-site questions directly, without reading whole functions. It shows up to "
    "6 matching lines; narrow REGEX or use the reported OFFSET for more. "
    "When implementation details matter, use `" + TOOL + " read FILE NAME` (qualified names work) or "
    "`" + TOOL + " read FILE A:B`. You can select several names/ranges in one call, e.g. "
    "`" + TOOL + " read FILE Class.method helper 20:35`; output shares a bounded budget. "
    "Known locations can be read directly; no preliminary preview is required. "
    "For orientation use `" + TOOL + " map FILE [REGEX] [OFFSET]`: declaration spans/signatures and brief "
    "documentation, 20 entries per page. `read FILE` shows a small file in full or a map of a larger file. "
    "Read only enough to answer the current question. Follow an omission's exact continuation only if "
    "that code is needed for the change or verification; summaries are not full implementations. "
    "Reuse unchanged source already in history. Broad batches retain every printed line on later requests. "
    "Explicit sed ranges remain available for unrestricted source, including unusually long lines."
)


def _quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command(path, start, end):
    return TOOL + " read " + _quote(path) + " " + str(start) + ":" + str(end)


def _load(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _excerpt(path, lines, start, end, limit=READ_LINES, chars=READ_CHARS):
    end = min(end, len(lines))
    if start > end:
        return path + ": no lines in requested range (" + str(len(lines)) + " total)."
    output = [path + ":" + str(start) + "-" + str(end)]
    used = len(output[0]) + 1
    next_line = start
    for number in range(start, min(end + 1, start + limit)):
        raw = lines[number - 1]
        prefix = str(number) + "\t"
        room = chars - 400 - used - len(prefix)
        if room < 40:
            break
        if len(raw) + 1 > room:
            if number != start:
                break
            output.append(prefix + raw[:room - 40] + " ... [line shortened]")
            output.append("[Full line: sed -n '" + str(number) + "p' " + _quote(path) + "]")
        else:
            output.append(prefix + raw)
        used = sum(len(row) + 1 for row in output)
        next_line = number + 1
    if next_line <= end:
        output.append("[" + str(end - next_line + 1) + " lines omitted; continue: " + _command(path, next_line, end) + "]")
    return "\n".join(output)


def _doc(lines, start, end):
    # A short hint only; never present the declaration or hint as the full body.
    for line in lines[start:min(end, start + 8)]:
        text = line.strip()
        if not text:
            continue
        if text.startswith(('"""', "'''")):
            hint = text[3:].strip().rstrip("\"'").strip()
            return hint[:90]
        if text.startswith(("#", "//")):
            return text[:90]
        if not (text.endswith((",", ":", "(", ")", "{")) or text.startswith("@")):
            break
    return ""


def _map(path, lines, pattern="", offset=0):
    entries = outline(lines)
    if pattern:
        try:
            matcher = re.compile(pattern, re.IGNORECASE)
        except re.error as error:
            return "Invalid map regex: " + str(error)
        entries = [entry for entry in entries if matcher.search(entry[3])]
    if not entries:
        if pattern:
            return path + ": no matching declarations; try " + TOOL + " search " + _quote(path) + " " + _quote(pattern)
        return _excerpt(path, lines, 1, len(lines), 40)
    output = [path + " (" + str(len(lines)) + " lines; " + str(len(entries)) + " declarations; bodies omitted)"]
    page = entries[offset:offset + MAP_ENTRIES]
    for start, end, indent, text in page:
        output.append(str(start) + "-" + str(end) + " " + text[:150])
        hint = _doc(lines, start, end)
        if hint:
            output.append("  " + hint)
    if offset + len(page) < len(entries):
        output.append("[More: " + TOOL + " map " + _quote(path) + " " + _quote(pattern) + " " + str(offset + len(page)) + "]")
    elif not page:
        output.append("[End of map.]")
    output.append("[Read a needed body/range: " + TOOL + " read " + _quote(path) + " NAME|A:B]")
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
    if not 1 <= len(argv) <= 7:
        return "Usage: " + TOOL + " read FILE [NAME|A:B ...] (up to six selections)"
    path = argv[0]
    try:
        lines = _load(path)
    except OSError as error:
        return path + ": " + str(error)
    if not lines:
        return path + ": empty file"
    selections = argv[1:]
    if not selections:
        if len(lines) <= READ_LINES and sum(len(line) + 1 for line in lines) <= READ_CHARS - 500:
            return _excerpt(path, lines, 1, len(lines))
        return _map(path, lines)
    limit = max(12, READ_LINES // len(selections))
    chars = max(800, READ_CHARS // len(selections))
    defs = None
    output = []
    for selection in selections:
        span = re.fullmatch(r"(\d+)(?:[:-](\d*))?", selection)
        if span:
            start = int(span.group(1))
            end = int(span.group(2)) if span.group(2) else start + limit - 1
            if start < 1 or end < start:
                output.append("Use a positive inclusive range A:B with B >= A.")
                continue
            output.append(_excerpt(path, lines, start, end, limit, chars))
            continue
        if defs is None:
            defs = definitions(path, lines)
        matches = find(defs, selection)
        if not matches:
            output.append(path + ": no definition named " + selection + "; try " + TOOL + " search " + _quote(path) + " " + _quote(selection))
            continue
        if len(matches) > 1:
            output.append(path + ": ambiguous " + selection + "; qualify or read a range:")
            output.extend(name[:120] + " " + str(start) + ":" + str(end) for name, start, end in matches[:8])
            if len(matches) > 8:
                output.append("[" + str(len(matches) - 8) + " further matches; narrow the name.]")
            continue
        name, start, end = matches[0]
        for unused in range(5):
            if start <= 1 or not re.match(r"\s*(?://|#|/\*|\*|\*/)", lines[start - 2]):
                break
            start -= 1
        output.append(_excerpt(path, lines, start, end, limit, chars))
    return "\n\n".join(output)


def search(argv):
    if not 2 <= len(argv) <= 3:
        return "Usage: " + TOOL + " search FILE REGEX [OFFSET] (6 matching lines, +/-2 context)"
    path, pattern = argv[:2]
    try:
        matcher = re.compile(pattern)
        offset = int(argv[2]) if len(argv) > 2 else 0
        if offset < 0:
            return "OFFSET must be nonnegative."
        lines = _load(path)
    except (OSError, ValueError, re.error) as error:
        return path + ": " + str(error)
    hits = [i + 1 for i, line in enumerate(lines) if matcher.search(line)]
    if not hits:
        return path + ": no matches for " + pattern
    groups = []
    for hit in hits[offset:offset + 6]:
        start, end = max(1, hit - 2), min(len(lines), hit + 2)
        if groups and start <= groups[-1][1] + 1:
            groups[-1][1] = end
            groups[-1][2].append(hit)
        else:
            groups.append([start, end, [hit]])
    defs = definitions(path, lines)
    output = [path + ": " + str(len(hits)) + " matching lines; matches " + ",".join(str(n) for n in hits[offset:offset + 6])]
    consumed = 0
    for start, end, selected in groups:
        room = READ_CHARS - sum(len(row) + 1 for row in output)
        if room < 900:
            break
        owners = []
        for hit in selected:
            enclosing = [d for d in defs if d[1] <= hit <= d[2]]
            if enclosing:
                name, a, b = min(enclosing, key=lambda d: d[2] - d[1])
                label = name[:120] + " " + str(a) + ":" + str(b)
                if label not in owners:
                    owners.append(label)
        if owners:
            output.append("[In " + "; ".join(owners) + "]")
        output.append(_excerpt(path, lines, start, end, READ_LINES, max(600, room - 250)))
        consumed += len(selected)
    if offset + consumed < len(hits):
        output.append("[More matches: " + TOOL + " search " + _quote(path) + " " + _quote(pattern) + " " + str(offset + consumed) + "]")
    elif not groups:
        output.append("[End of matches.]")
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
        head = [str(i + 1) + "\t" + line[:160] for i, line in enumerate(lines[:5])]
        result = "\n".join(head) + "\n" + _map(path, lines)
    else:
        result = _excerpt(path, lines, 1, len(lines), 40)
    result += "\n[Whole-file print shortened. For occurrences with context: " + TOOL + " search " + _quote(path) + " REGEX; for full source use read FILE A:B or sed -n 'A,Bp' FILE.]"
    return result if len(result) < len(out) else None


TOOLS = {"search": search, "read": read, "map": map_file}


if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
