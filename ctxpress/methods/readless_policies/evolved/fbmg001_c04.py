"""Small, navigable source reads with a shared budget for related questions."""
from __future__ import annotations

import ast
import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.definition import definitions, find

TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
READ_LINES = 60
READ_CHARS = 5400
MAP_ENTRIES = 20
_NUMBERED = re.compile(r"^\s*\d+\t")

GUIDANCE = (
    "Read only what answers the current implementation question; broad/chained sed and cat reads keep all "
    "their output in history. Prefer `" + TOOL + " search 'REGEX' FILE [FILE ...]`: it returns matching source "
    "with nearby lines and enclosing definition names, often answering the question without another read. "
    "Search several alternatives with 'name1|name2'. For known locations use `" + TOOL + " read FILE A:B` "
    "or `" + TOOL + " read FILE NAME` (qualified names supported). Several selections in one file may follow; "
    "they share a 60-line/5400-character budget. Read implementation directly when changing or debugging it; "
    "if only an API or class structure matters, `" + TOOL + " peek FILE NAME [NAME ...]` shows signatures, "
    "Python docstrings and member spans. No preview is required. `" + TOOL + " map FILE [REGEX] [OFFSET]` "
    "lists definitions; read FILE without a selection maps long source files. Search accepts up to 8 explicit "
    "files; use rg to discover paths. Results mark omissions and give exact continuation commands. Expand "
    "only when the omitted code matters to the edit or verification, and reuse previous reads until files "
    "change. Explicit sed ranges remain available for full text, including unusually long lines."
)


def _quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command(path, selection):
    return TOOL + " read " + _quote(path) + " " + _quote(str(selection))


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
        room = chars - 450 - used - len(prefix)
        if room < 40:
            break
        if len(raw) + 1 > room:
            if number != start:
                break
            output.append(prefix + raw[:max(1, room - 40)] + " ... [line shortened]")
            output.append("[Full line: sed -n '" + str(number) + "p' " + _quote(path) + "]")
        else:
            output.append(prefix + raw)
        used = sum(len(item) + 1 for item in output)
        next_line = number + 1
    if next_line <= end:
        output.append("[" + str(end - next_line + 1) + " lines omitted; continue: " +
                      _command(path, str(next_line) + ":" + str(end)) + "]")
    return "\n".join(output)


def _map(path, lines, pattern="", offset=0):
    entries = definitions(path, lines)
    if pattern:
        try:
            matcher = re.compile(pattern, re.IGNORECASE)
        except re.error as error:
            return "Invalid map regex: " + str(error)
        entries = [entry for entry in entries if matcher.search(entry[0])]
    output = [path + " (" + str(len(lines)) + " lines; " + str(len(entries)) + " matching definitions)"]
    for name, start, end in entries[offset:offset + MAP_ENTRIES]:
        output.append(str(start) + "-" + str(end) + " " + name[:150])
    next_offset = offset + MAP_ENTRIES
    if next_offset < len(entries):
        output.append("[More: " + TOOL + " map " + _quote(path) + " " + _quote(pattern) + " " + str(next_offset) + "]")
    if not entries:
        output.append("[No matching definitions. Read lines: " + _command(path, "1:60") + "]")
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


def _resolve(path, lines, selection, entries):
    span = re.fullmatch(r"(\d+)(?:[:-](\d*))?", selection)
    if span:
        start = int(span.group(1))
        end = int(span.group(2)) if span.group(2) else start + READ_LINES - 1
        if start < 1 or end < start:
            return "Use a positive inclusive range A:B with B >= A."
        return start, end
    matches = find(entries, selection)
    if not matches:
        return path + ": no definition named " + selection + "; try " + TOOL + " search " + _quote(re.escape(selection)) + " " + _quote(path)
    if len(matches) != 1:
        return path + ": ambiguous name; qualify it or read a range:\n" + "\n".join(
            name[:150] + " " + str(start) + ":" + str(end) for name, start, end in matches[:10]) + (
            "\n[More matches omitted; narrow the name.]" if len(matches) > 10 else "")
    unused, start, end = matches[0]
    for unused in range(6):
        if start <= 1 or not re.match(r"\s*(?://|#|/\*|\*|\*/)", lines[start - 2]):
            break
        start -= 1
    return start, end


def read(argv):
    if not 1 <= len(argv) <= 5:
        return "Usage: " + TOOL + " read FILE [A:B|NAME ...] (up to 4 selections, shared budget)"
    path = argv[0]
    try:
        lines = _load(path)
        if not lines:
            return path + ": empty file"
        selections = argv[1:]
        if not selections:
            if len(lines) > READ_LINES and definitions(path, lines):
                return _map(path, lines) + "\n[Read a name or range: " + _command(path, "NAME") + "]"
            return _excerpt(path, lines, 1, len(lines))
        entries = definitions(path, lines) if any(not re.fullmatch(r"\d+(?:[:-]\d*)?", s) for s in selections) else []
        output = []
        for selection in selections:
            span = _resolve(path, lines, selection, entries)
            if isinstance(span, str):
                output.append(span)
            else:
                output.append(_excerpt(path, lines, span[0], span[1],
                                       READ_LINES // len(selections), READ_CHARS // len(selections)))
        return "\n".join(output)
    except (OSError, ValueError, RecursionError) as error:
        return path + ": " + str(error)


def _preview(path, lines, name, entries, tree):
    matches = find(entries, name)
    if len(matches) != 1:
        span = _resolve(path, lines, name, entries)
        return span if isinstance(span, str) else path + ": use a definition name for peek."
    qualified, start, end = matches[0]
    stop = min(end, start + 9)
    node = None
    if tree is not None:
        for item in ast.walk(tree):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = min([item.lineno] + [d.lineno for d in item.decorator_list])
                if first == start:
                    node = item
                    break
    if node is not None and node.body:
        body = node.body[0]
        stop = min(stop, max(start, body.lineno - 1))
        if isinstance(body, ast.Expr) and isinstance(body.value, (ast.Str, ast.Constant)):
            value = body.value.s if isinstance(body.value, ast.Str) else body.value.value
            if isinstance(value, str):
                stop = min(end, start + 15, body.end_lineno)
    else:
        for index in range(start - 1, stop):
            if "{" in lines[index] or re.search(r":\s*(?://.*|#.*)?$", lines[index]):
                stop = index + 1
                break
    output = [path + ":" + str(start) + "-" + str(end) + " " + qualified + " [preview]"]
    output.extend(str(i) + "\t" + lines[i - 1][:200] for i in range(start, stop + 1))
    members = [d for d in entries if d[0].startswith(qualified + ".") and
               d[0].count(".") == qualified.count(".") + 1]
    if members:
        output.append("Members:")
        output.extend(str(a) + "-" + str(b) + " " + q[:120] for q, a, b in members[:10])
        if len(members) > 10:
            output.append("[More members: " + TOOL + " map " + _quote(path) + " " + _quote("^" + re.escape(qualified) + r"\.") + "]")
    output.append("[Full implementation: " + _command(path, qualified) + "]")
    return "\n".join(output)


def peek(argv):
    if not 2 <= len(argv) <= 4:
        return "Usage: " + TOOL + " peek FILE NAME [NAME ...] (up to 3 names)"
    path = argv[0]
    try:
        lines = _load(path)
        entries = definitions(path, lines)
        tree = None
        if path.endswith((".py", ".pyi")):
            try:
                tree = ast.parse("\n".join(lines))
            except (SyntaxError, ValueError):
                pass
        output = []
        used = 0
        for name in argv[1:]:
            item = _preview(path, lines, name, entries, tree)
            if used + len(item) > READ_CHARS:
                output.append("[Preview budget reached; remaining names: " + ", ".join(argv[len(output) + 1:]) + "]")
                break
            output.append(item)
            used += len(item) + 1
        return "\n".join(output)
    except (OSError, ValueError, RecursionError) as error:
        return path + ": " + str(error)


def search(argv):
    if not 2 <= len(argv) <= 9:
        return "Usage: " + TOOL + " search REGEX FILE [FILE ...] (up to 8 files, 12 matching lines)"
    try:
        matcher = re.compile(argv[0])
    except re.error as error:
        return "Invalid search regex: " + str(error)
    output = []
    used, hits = 0, 0
    for file_index, path in enumerate(argv[1:]):
        try:
            lines = _load(path)
            entries = None
            windows = []
            for index, line in enumerate(lines):
                if not matcher.search(line):
                    continue
                if hits >= 12 or used >= READ_CHARS - 1100:
                    output.append("[Search budget reached at " + path + ":" + str(index + 1) +
                                  "; later matches/files not shown. Narrow REGEX or read " + _command(path, str(index + 1)) + "]")
                    return "\n".join(output)
                if entries is None:
                    entries = definitions(path, lines)
                owners = [(q, a, b) for q, a, b in entries if a <= index + 1 <= b]
                owner = min(owners, key=lambda d: d[2] - d[1]) if owners else None
                first, last = max(1, index - 1), min(len(lines), index + 3)
                if windows and first <= windows[-1][1] + 1:
                    first = windows[-1][1] + 1
                label = path + ":" + str(index + 1)
                if owner:
                    label += " in " + owner[0][:100] + " (" + str(owner[1]) + ":" + str(owner[2]) + ")"
                block = [label]
                for number in range(first, last + 1):
                    raw = lines[number - 1]
                    block.append(str(number) + "\t" + raw[:240] + (" ... [line shortened]" if len(raw) > 240 else ""))
                item = "\n".join(block)
                if used + len(item) > READ_CHARS - 400:
                    output.append("[Search budget reached; read " + _command(path, str(index + 1)) + "; later matches/files not shown.]")
                    return "\n".join(output)
                output.append(item)
                used += len(item) + 1
                hits += 1
                windows.append((first, last))
        except (OSError, ValueError, RecursionError) as error:
            item = path + ": " + str(error)
            output.append(item)
            used += len(item) + 1
    if not hits:
        output.append("No matches for " + argv[0] + " in the specified files.")
    return "\n".join(output)


def view(cmd, out, ctx):
    target = read_target(cmd)
    if target is None or not re.match(r"^\s*(?:cat|nl\s+-ba)\s", cmd):
        return None
    path, first, numbered = target
    if first != 1:
        return None
    if path.replace("\\", "/").rsplit("/", 1)[-1].upper() in ("AGENTS.MD", "CLAUDE.MD", "SKILL.MD"):
        return None
    lines = out.splitlines()
    if len(lines) <= READ_LINES and len(out) <= READ_CHARS:
        return None
    if numbered:
        lines = [_NUMBERED.sub("", line) for line in lines]
    if definitions(path, lines):
        header = "\n".join(str(i + 1) + "\t" + line[:160] for i, line in enumerate(lines[:5]))
        result = header + "\n" + _map(path, lines)
        result += "\n[Whole-file print shortened; read a name/range: " + _command(path, "NAME") + "; search nearby source: " + TOOL + " search 'REGEX' " + _quote(path) + "]"
    else:
        result = _excerpt(path, lines, 1, len(lines), 45)
    return result if len(result) < len(out) else None


TOOLS = {"search": search, "read": read, "peek": peek, "map": map_file}

if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
