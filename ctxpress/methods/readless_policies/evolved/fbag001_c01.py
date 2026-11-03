"""Focused source reads, API previews, and search with implementation pointers."""
from __future__ import annotations

import ast
import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.definition import definitions, find
from ctxpress.tools.outline import outline


TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
READ_LINES = 64
READ_CHARS = 4800
MAP_ENTRIES = 20
_NUMBERED = re.compile(r"^\s*\d+\t")

GUIDANCE = (
    "Keep source output tied to the next decision; unused reads cost context on every later request. "
    "Prefer these focused tools to chained sed/cat surveys. "
    "Locate files with rg -l, then use `" + TOOL + " search 'NAME|OTHER_NAME' FILE [FILE ...]`: "
    "one bounded answer gives matching lines, three lines of context, and enclosing definition spans. "
    "Use this to answer several related questions without printing several large ranges. "
    "To compare unfamiliar APIs use `" + TOOL + " peek FILE NAME [NAME ...]` for declaration/doc previews. "
    "When implementation matters, go straight to `" + TOOL + " read FILE NAME` (qualified names work) "
    "or `" + TOOL + " read FILE A:B`; no preview is required. Reads show up to 64 lines/4800 characters "
    "and give the exact continuation for omissions. Read omitted code when needed for the change or verification, "
    "rather than paging automatically. `" + TOOL + " read FILE` shows a small file or a compact map; "
    "`" + TOOL + " map FILE [REGEX] [OFFSET]` filters definition names/signatures. "
    "Search supports --skip N to continue matches. Batch focused questions, not full implementations; "
    "reuse code already read unless it changed. Explicit sed ranges remain available for full source."
)


def _quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command(path, start, end):
    return TOOL + " read " + _quote(path) + " " + str(start) + ":" + str(end)


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
    for number in range(start, min(end + 1, start + limit)):
        raw = lines[number - 1]
        prefix = str(number) + "\t"
        room = budget - 600 - used - len(prefix)
        if room < 40:
            break
        if len(raw) + 1 > room:
            if number != start:
                break
            output.append(prefix + raw[:room - 35] + " ... [line shortened]")
            output.append("[Full line: sed -n '" + str(number) + "p' " + _quote(path) + "]")
        else:
            output.append(prefix + raw)
        used = sum(len(part) + 1 for part in output)
        next_line = number + 1
    if next_line <= end:
        output.append("[" + str(end - next_line + 1) + " lines omitted; " + _command(path, next_line, end) + "]")
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
        output.append(str(start) + "-" + str(end) + " " + text[:150])
    remaining = len(entries) - offset - len(page)
    if remaining > 0:
        output.append("[" + str(remaining) + " more; filter REGEX or " + TOOL + " map " + _quote(path) + " " + _quote(pattern) + " " + str(offset + len(page)) + "]")
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
    if len(lines) <= READ_LINES and sum(len(line) + 1 for line in lines) <= READ_CHARS - 500:
        return _excerpt(path, lines, 1, len(lines))
    if not outline(lines):
        return _excerpt(path, lines, 1, len(lines), 32)
    head = [str(i + 1) + "\t" + line[:180] + (" ... [line shortened]" if len(line) > 180 else "")
            for i, line in enumerate(lines[:5])]
    return ("\n".join(head) + "\n" + _map(path, lines) +
            "\n[Overview only. Implementation: " + TOOL + " read " + _quote(path) + " NAME or A:B.]")


def _comments(lines, start):
    for unused in range(6):
        if start <= 1 or not re.match(r"\s*(?://|#|/\*|\*|\*/)", lines[start - 2]):
            break
        start -= 1
    return start


def _missing(path, selection, defs):
    near = [name for name, start, end in defs if selection.lower() in name.lower()][:8]
    return (path + ": no definition named " + selection +
            ("; similar: " + ", ".join(near) if near else "") +
            ". Search text: " + TOOL + " search " + _quote(re.escape(selection)) + " " + _quote(path))


def read(argv):
    if not 1 <= len(argv) <= 2:
        return "Usage: " + TOOL + " read FILE [A:B|NAME]"
    path = argv[0]
    try:
        lines = _load(path)
    except (OSError, ValueError) as error:
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
            return "Use a positive inclusive range A:B with B >= A."
        return _excerpt(path, lines, start, end)
    defs = definitions(path, lines)
    matches = find(defs, selection)
    if not matches:
        return _missing(path, selection, defs)
    if len(matches) > 1:
        output = [path + ": ambiguous name; qualify it or read a range:"]
        output.extend(name[:150] + " " + str(start) + ":" + str(end) for name, start, end in matches[:12])
        if len(matches) > 12:
            output.append("[More matches; narrow the name.]")
        return "\n".join(output)
    name, start, end = matches[0]
    return _excerpt(path, lines, _comments(lines, start), end)


def _declaration_end(lines, start, end):
    """Bounded signature preview; never claim that a preview is a full body."""
    stop = start
    for number in range(start, min(end, start + 11) + 1):
        text = lines[number - 1].strip()
        stop = number
        if text.startswith("@"):
            continue
        if "{" in text or text.endswith(";") or re.search(r":\s*(?:#.*)?$", text):
            break
    return stop


def _python_previews(path, lines):
    if not path.endswith((".py", ".pyi")):
        return {}
    try:
        tree = ast.parse("\n".join(lines))
    except (SyntaxError, ValueError):
        return {}
    spans = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        body = node.body[0]
        stop = max(node.lineno, body.lineno - 1)
        if isinstance(body, ast.Expr) and isinstance(body.value, ast.Str):
            stop = min(body.end_lineno, body.lineno + 7)
        spans[start] = stop
    return spans


def peek(argv):
    if not 2 <= len(argv) <= 5:
        return "Usage: " + TOOL + " peek FILE NAME [NAME ...] (up to four names; read FILE NAME for body)"
    path = argv[0]
    try:
        lines = _load(path)
        defs = definitions(path, lines)
        previews = _python_previews(path, lines)
    except (OSError, ValueError) as error:
        return path + ": " + str(error)
    output = []
    budget = min(2100, (READ_CHARS - 300) // (len(argv) - 1))
    for selection in argv[1:]:
        matches = find(defs, selection)
        if not matches:
            output.append(_missing(path, selection, defs))
            continue
        if len(matches) != 1:
            output.append(selection + ": qualify one of " + ", ".join(name[:100] for name, s, e in matches[:8]))
            continue
        name, start, end = matches[0]
        stop = previews.get(start)
        if stop is None:
            stop = _declaration_end(lines, start, end)
        # Python/Cython documentation immediately following a declaration.
        if start not in previews and stop < end:
            text = lines[stop].lstrip()
            match = re.match(r"[rRuU]?(\"\"\"|''')", text)
            if match:
                quote = match.group(1)
                doc_start = stop + 1
                stop = doc_start
                if quote not in text[match.end():]:
                    while stop < min(end, doc_start + 7):
                        stop += 1
                        if quote in lines[stop - 1]:
                            break
        output.append(name + " (definition " + str(start) + ":" + str(end) + "; preview)\n" +
                      _excerpt(path, lines, _comments(lines, start), stop, 20, budget) +
                      "\n[Implementation: " + _command(path, start, end) + "]")
    return "\n\n".join(output)


def search(argv):
    usage = "Usage: " + TOOL + " search REGEX FILE [FILE ...] [--skip N] (up to 12 files, 8 matching regions/page)"
    args = list(argv)
    skip = 0
    if "--skip" in args:
        index = args.index("--skip")
        if index != len(args) - 2:
            return usage
        try:
            skip = int(args[-1])
        except ValueError:
            return usage
        args = args[:index]
    if not 2 <= len(args) <= 13 or skip < 0:
        return usage
    try:
        matcher = re.compile(args[0])
    except re.error as error:
        return "Invalid search regex: " + str(error)
    output = []
    used = 0
    total = 0
    consumed = skip
    regions = 0
    full = False
    for path in args[1:]:
        try:
            lines = _load(path)
        except (OSError, ValueError) as error:
            message = path + ": " + str(error)
            output.append(message[:220])
            used += len(output[-1]) + 1
            continue
        hits = [i + 1 for i, line in enumerate(lines) if matcher.search(line)]
        defs = None
        last_end = 0
        for number in hits:
            total += 1
            if total <= skip or full:
                continue
            if number <= last_end:
                consumed = total
                continue
            if regions >= 8 or used > READ_CHARS - 1100:
                full = True
                continue
            if defs is None:
                defs = definitions(path, lines)
            enclosing = [(q, s, e) for q, s, e in defs if s <= number <= e]
            label = path + ":" + str(number)
            if enclosing:
                name, start, end = min(enclosing, key=lambda entry: entry[2] - entry[1])
                label += " [" + name[:100] + " " + str(start) + ":" + str(end) + "]"
            first = max(last_end + 1, number - 3, 1)
            last_end = min(len(lines), number + 3)
            block = [label]
            for n in range(first, last_end + 1):
                raw = lines[n - 1]
                block.append(str(n) + (">\t" if matcher.search(raw) else "\t") +
                             raw[:240] + (" ... [line shortened]" if len(raw) > 240 else ""))
            text = "\n".join(block)
            if used + len(text) > READ_CHARS - 550:
                # Always allow progress, even for minified source.
                block = [label, str(number) + ">\t" + lines[number - 1][:240] +
                         " [context omitted; read this range]"]
                last_end = number
                text = "\n".join(block)
            output.append(text)
            used += len(text) + 2
            consumed = total
            regions += 1
    if total == 0:
        output.append("No matches (case-sensitive regex).")
    elif skip >= total:
        output.append("End of search (" + str(total) + " matching lines).")
    elif consumed < total:
        output.append("[" + str(total - consumed) + " more matching lines; repeat search with --skip " + str(consumed) + "]")
    output.append("[Full context: " + TOOL + " read FILE NAME or A:B. > marks a match; previews may shorten long lines.]")
    return "\n\n".join(output)


def view(cmd, out, ctx):
    target = read_target(cmd)
    if target is None or not re.match(r"^\s*(?:cat|nl\s+-ba)\s", cmd):
        return None
    path, first, numbered = target
    if path.replace("\\", "/").rsplit("/", 1)[-1].upper() in ("AGENTS.MD", "CLAUDE.MD", "SKILL.MD"):
        return None
    if first != 1:
        return None
    lines = out.splitlines()
    if len(lines) <= READ_LINES and len(out) <= READ_CHARS:
        return None
    if numbered:
        lines = [_NUMBERED.sub("", line) for line in lines]
    result = _overview(path, lines)
    return result if len(result) < len(out) else None


TOOLS = {"read": read, "map": map_file, "peek": peek, "search": search}


if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
