"""Bound source reads, folding long documentation and avoiding tiny continuation reads."""
from __future__ import annotations

import io
import re
import tokenize
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.definition import definitions, find
from ctxpress.tools.outline import outline


TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
READ_LINES = 100
READ_CHARS = 7000
FULL_LINES = 240
FULL_CHARS = 16000
FINISH_LINES = 24
MAP_ENTRIES = 24
_NUMBERED = re.compile(r"^\s*\d+\t")

GUIDANCE = (
    "Keep source reads focused on the next implementation decision. Search for relevant symbols, then prefer `"
    + TOOL + " read FILE A:B` or `" + TOOL + " read FILE NAME` (qualified names work) to broad/chained sed or cat reads. "
    "Read directly when you know the location; no map or preview is required. One file and one selection per read. "
    "Reads return actual numbered code, normally up to 100 lines / 7000 characters; a small remainder may fit. "
    "Long Python/Cython docstrings retain their opening and closing lines with an explicit pointer to the omitted text. "
    "Line numbers always refer to the original file. Omission notices are optional access paths, not a reading checklist: "
    "continue only if the missing implementation or contract matters to your change or verification. "
    "When it does, the supplied `read FILE A:B full` command reads only the remainder, with a larger 240-line / "
    "16000-character budget and literal documentation, avoiding a series of small reads. "
    "For an unfamiliar file, `" + TOOL + " map FILE [REGEX] [OFFSET]` lists up to 24 definition spans; filter by relevant "
    "names instead of surveying several files. Batching many reads still retains all their output in history. "
    "Reuse code already read unless it changed. Long whole-file prints become compact maps; explicit ranges remain available."
)


def _quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command(path, start, end, full=False):
    return (TOOL + " read " + _quote(path) + " " + str(start) + ":" + str(end)
            + (" full" if full else ""))


def _load(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _docstrings(path, lines):
    """Find standalone first-suite strings, including Cython, without parsing its grammar."""
    if not path.endswith((".py", ".pyi", ".pyx", ".pxd", ".pxi")):
        return []
    try:
        tokens = [t for t in tokenize.generate_tokens(io.StringIO("\n".join(lines)).readline)
                  if t.type not in (tokenize.NL, tokenize.COMMENT, tokenize.ENCODING)]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []
    spans = []
    for i, token in enumerate(tokens):
        if token.type != tokenize.STRING or token.end[0] - token.start[0] < 15:
            continue
        # Only ordinary, standalone string statements; never assignments or f-strings.
        text = token.string.lower()
        if not text.startswith(('"""', "\'\'\'", 'r"""', "r\'\'\'", 'u"""', "u\'\'\'")):
            continue
        if i + 1 >= len(tokens) or tokens[i + 1].type not in (tokenize.NEWLINE, tokenize.ENDMARKER):
            continue
        if i:
            if tokens[i - 1].type != tokenize.INDENT:
                continue
            j = i - 2
            if j >= 0 and tokens[j].type == tokenize.NEWLINE:
                j -= 1
            header = []
            while j >= 0 and tokens[j].type not in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
                header.append(tokens[j].string)
                j -= 1
            header.reverse()
            if not header or header[0] not in ("def", "class", "async", "cdef", "cpdef"):
                continue
        spans.append((token.start[0], token.end[0]))
    return spans


def _excerpt(path, lines, start, end, limit=READ_LINES, full=False):
    """Keep implementation visible; every skipped byte has a literal expansion path."""
    end = min(end, len(lines))
    if start > end:
        return path + ": no lines in requested range (" + str(len(lines)) + " total)."
    char_limit = FULL_CHARS if full else READ_CHARS
    if full:
        limit = FULL_LINES
    gaps = {}
    if not full:
        for first, last in _docstrings(path, lines):
            # A read wholly inside documentation is literal. Fold only when code follows.
            if start <= first and last < end:
                gaps[first + 4] = last - 1
    items = []
    number = start
    while number <= end:
        if number in gaps:
            last = gaps[number]
            text = ("[Docstring lines " + str(number) + "-" + str(last) + ": "
                    + _command(path, number, last, True) + "]")
            items.append((number, last, text, False))
            number = last + 1
        else:
            items.append((number, number, str(number) + "\t" + lines[number - 1], True))
            number += 1
    output = [path + ":" + str(start) + "-" + str(end)]
    # Finish a nearly complete selection within the same character budget.
    count = sum(1 for item in items if item[3])
    size = len(output[0]) + sum(len(item[2]) + 1 for item in items)
    if not full and count <= limit + FINISH_LINES and size < char_limit - 900:
        limit = max(limit, count)
    used, shown, next_line = len(output[0]) + 1, 0, start
    for first, last, text, source in items:
        if source and shown >= limit:
            break
        room = char_limit - 900 - used
        if room < 40:
            break
        previous = len(output)
        if len(text) + 1 > room:
            if first != start or not source:
                break
            output.append(text[:room - 40] + " ... [line shortened]")
            output.append("[Full line " + str(first) + ": sed -n '" + str(first) + "p' " + _quote(path) + "]")
        else:
            output.append(text)
        used += sum(len(line) + 1 for line in output[previous:])
        shown += int(source)
        next_line = last + 1
    if next_line <= end:
        output.append("[" + str(end - next_line + 1) + " more lines, if needed: "
                      + _command(path, next_line, end, True) + "]")
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
    if not 1 <= len(argv) <= 3 or (len(argv) == 3 and argv[2] != "full"):
        return "Usage: " + TOOL + " read FILE [A:B|NAME] [full] (one file and one selection per call)"
    full = len(argv) == 3
    path = argv[0]
    try:
        lines = _load(path)
    except OSError as error:
        return path + ": " + str(error)
    if not lines:
        return path + ": empty file"
    selection = argv[1] if len(argv) >= 2 else "1:100"
    span = re.fullmatch(r"(\d+)(?:[:-](\d*))?", selection)
    if span:
        start = int(span.group(1))
        end = int(span.group(2)) if span.group(2) else start + READ_LINES - 1
        if start < 1 or end < start:
            return "Use a positive, inclusive range A:B with B >= A."
        result = _excerpt(path, lines, start, end, full=full)
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
    return _excerpt(path, lines, start, end, full=full)


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
