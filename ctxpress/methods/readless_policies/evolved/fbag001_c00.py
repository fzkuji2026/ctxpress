"""Small, question-directed source reads with explicit routes to complete text."""
from __future__ import annotations

import ast
import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.definition import definitions, find
from ctxpress.tools.outline import outline


TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
READ_LINES = 64
MAX_CHARS = 6000
MAP_ENTRIES = 20
_NUMBERED = re.compile(r"^\s*\d+\t")

GUIDANCE = (
    "Read to answer the next concrete implementation question. Prefer these focused source tools to chained "
    "sed/cat surveys: `" + TOOL + " search 'REGEX' FILE [FILE ...]` searches several known files together, "
    "returning matching lines, two lines of context, and enclosing definitions (12 hits per page; --offset N). "
    "Use rg to discover filenames first. For comparing APIs or learning a collaborator's contract, use `" + TOOL +
    " peek FILE [NAME ...]`: signatures and documentation without long bodies. When you need the implementation, "
    "go directly to `" + TOOL + " read FILE NAME` or `" + TOOL + " read FILE A:B`; qualified names work. "
    "Several selectors in one read share a 64-line budget. Read the specific helper or branch you need, "
    "rather than consecutive chunks of every related file. Omission notices give exact continuation commands; "
    "expand only when the omitted code matters to the change or verification. Explicit sed ranges are available "
    "when a larger complete span is necessary. `" + TOOL + " read FILE` gives a compact overview of long source "
    "files; `" + TOOL + " map FILE [REGEX] [OFFSET]` lists definition spans. Reuse source already read unless it "
    "changed. Batching unrelated reads retains every printed line in history."
)


def _quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command(path, start, end):
    return TOOL + " read " + _quote(path) + " " + str(start) + ":" + str(end)


def _load(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _finish(parts):
    text = "\n".join(parts)
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS - 140] + "\n[Output budget reached; narrow the selection or use an explicit sed range for complete source.]"


def _show(path, lines, spans):
    """One budget across all selections, with every omitted range identified."""
    parts = [path + " (" + str(len(lines)) + " lines)"]
    left = READ_LINES
    room = 4100
    for start, end in spans:
        end = min(end, len(lines))
        if start > end:
            parts.append("[No lines at " + str(start) + ".]")
            continue
        parts.append("--- " + str(start) + ":" + str(end))
        next_line = start
        while next_line <= end and left > 0 and room > 60:
            raw = str(next_line) + "\t" + lines[next_line - 1]
            if len(raw) + 1 > room:
                if next_line == start and room > 300:
                    parts.append(raw[:room - 180] + " ... [line shortened]")
                    parts.append("[Full line: sed -n '" + str(next_line) + "p' " + _quote(path) + "]")
                    next_line += 1
                    left -= 1
                    room = 0
                break
            parts.append(raw)
            room -= len(raw) + 1
            left -= 1
            next_line += 1
        if next_line <= end:
            parts.append("[Omitted " + str(next_line) + ":" + str(end) + "; " + _command(path, next_line, end) + "]")
    return _finish(parts)


def _map(path, lines, pattern="", offset=0):
    entries = definitions(path, lines)
    if pattern:
        matcher = re.compile(pattern, re.IGNORECASE)
        entries = [d for d in entries if matcher.search(d[0]) or matcher.search(lines[d[1] - 1])]
    parts = [path + " (" + str(len(lines)) + " lines; " + str(len(entries)) + " matching definitions)"]
    for name, start, end in entries[offset:offset + MAP_ENTRIES]:
        parts.append(str(start) + ":" + str(end) + " " + name[:100] + " | " + lines[start - 1].strip()[:130])
    if offset + MAP_ENTRIES < len(entries):
        parts.append("[More: " + TOOL + " map " + _quote(path) + " " + _quote(pattern) + " " + str(offset + MAP_ENTRIES) + "]")
    if not entries:
        parts.append("[No matching definitions; read a range: " + _command(path, 1, min(READ_LINES, len(lines))) + "]")
    return _finish(parts)


def _overview(path, lines):
    if len(lines) <= READ_LINES and sum(len(line) + 5 for line in lines) < 4200:
        return _show(path, lines, [(1, len(lines))])
    if not outline(lines):
        return _show(path, lines, [(1, len(lines))])
    parts = [path + ": overview; bodies omitted"]
    parts.extend(str(i + 1) + "\t" + line[:200] for i, line in enumerate(lines[:8]))
    parts.append(_map(path, lines))
    parts.append("[Implementation: " + TOOL + " read " + _quote(path) + " NAME or A:B; contracts: " + TOOL + " peek " + _quote(path) + " NAME]")
    return _finish(parts)


def map_file(argv):
    if not 1 <= len(argv) <= 3:
        return "Usage: " + TOOL + " map FILE [REGEX] [OFFSET]"
    try:
        offset = int(argv[2]) if len(argv) == 3 else 0
        if offset < 0:
            return "OFFSET must be nonnegative."
        return _map(argv[0], _load(argv[0]), argv[1] if len(argv) > 1 else "", offset)
    except (OSError, ValueError, re.error) as error:
        return _finish([str(error)])


def _comments(lines, start):
    for unused in range(8):
        if start <= 1 or not re.match(r"\s*(?://|#|/\*|\*|\*/)", lines[start - 2]):
            break
        start -= 1
    return start


def read(argv):
    if not 1 <= len(argv) <= 7:
        return "Usage: " + TOOL + " read FILE [A:B|NAME ...] (up to six selectors, shared budget)"
    path = argv[0]
    try:
        lines = _load(path)
    except OSError as error:
        return _finish([path + ": " + str(error)])
    if not lines:
        return path + ": empty file"
    if len(argv) == 1:
        return _overview(path, lines)
    spans, notices = [], []
    defs = None
    for selection in argv[1:]:
        span = re.fullmatch(r"(\d+)(?:[:-](\d*))?", selection)
        if span:
            start = int(span.group(1))
            end = int(span.group(2)) if span.group(2) else start + READ_LINES - 1
            if start < 1 or end < start:
                notices.append("Invalid range " + selection + ": use 1-based inclusive A:B.")
            else:
                spans.append((start, end))
            continue
        if defs is None:
            defs = definitions(path, lines)
        matches = find(defs, selection)
        if len(matches) == 1:
            name, start, end = matches[0]
            spans.append((_comments(lines, start), end))
        elif matches:
            notices.append("Ambiguous " + selection + ": " + "; ".join(name[:100] + " " + str(start) + ":" + str(end) for name, start, end in matches[:12]))
        else:
            notices.append("No definition " + selection + "; locate text with " + TOOL + " search " + _quote(re.escape(selection)) + " " + _quote(path))
    # Merge overlaps, so several requested helpers never repeat the same source.
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return _finish([_show(path, lines, merged)] + notices)


def peek(argv):
    if not 1 <= len(argv) <= 7:
        return "Usage: " + TOOL + " peek FILE [NAME ...] (signature/documentation; read FILE NAME for body)"
    path = argv[0]
    try:
        lines = _load(path)
    except OSError as error:
        return _finish([str(error)])
    defs = definitions(path, lines)
    selected, notices = [], []
    for name in argv[1:]:
        matches = find(defs, name)
        if not matches:
            notices.append("No definition " + name + "; use search for text or map for names.")
        selected.extend(d for d in matches if d not in selected)
    if len(argv) == 1:
        selected = defs
    # AST provides the exact signature/docstring boundary for Python; other
    # languages get their declaration and adjacent comments, never a guessed body.
    nodes = {}
    if path.endswith((".py", ".pyi")):
        try:
            for node in ast.walk(ast.parse("\n".join(lines))):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    nodes[start] = node
        except (SyntaxError, ValueError):
            pass
    parts = [path + ": contracts (implementation bodies omitted)"] + notices
    for name, start, end in selected[:10]:
        if sum(len(part) + 1 for part in parts) > 3600:
            parts.append("[Contract budget reached; request remaining names explicitly.]")
            break
        parts.append(str(start) + ":" + str(end) + " " + name)
        node = nodes.get(start)
        stop = start
        if node is not None and node.body:
            stop = max(start, node.body[0].lineno - 1)
        else:
            while stop < min(end, start + 7) and not re.search(r"[{:;]\s*(?://.*)?$", lines[stop - 1]):
                stop += 1
        first = _comments(lines, start)
        for number in range(first, min(stop, start + 9) + 1):
            raw = lines[number - 1]
            parts.append(str(number) + "\t" + raw[:240] + (" ... [line shortened]" if len(raw) > 240 else ""))
        if stop > start + 9:
            parts.append("[Signature continues; read " + str(start) + ":" + str(stop) + ".]")
        if node is not None:
            doc = ast.get_docstring(node, clean=True)
            if doc:
                parts.append("doc: " + doc[:650] + (" [doc continued in source]" if len(doc) > 650 else ""))
        parts.append("[Body: " + TOOL + " read " + _quote(path) + " " + _quote(name) + "]")
    if len(selected) > 10:
        parts.append("[" + str(len(selected) - 10) + " more definitions; use map to select names.]")
    if not selected:
        parts.append("[No definitions; use search or read a line range.]")
    return _finish(parts)


def search(argv):
    if len(argv) < 2:
        return "Usage: " + TOOL + " search REGEX FILE [FILE ...] [--offset N]"
    args = list(argv)
    offset = 0
    try:
        if len(args) >= 4 and args[-2] == "--offset":
            offset = int(args[-1])
            args = args[:-2]
        if offset < 0 or not 2 <= len(args) <= 13:
            return "Use 1-12 explicit files and a nonnegative --offset."
        matcher = re.compile(args[0])
    except (ValueError, re.error) as error:
        return str(error)
    records, errors = [], []
    for path in args[1:]:
        try:
            lines = _load(path)
        except OSError as error:
            errors.append(path + ": " + str(error))
            continue
        hits = [i for i, line in enumerate(lines, 1) if matcher.search(line)]
        records.append((path, lines, hits))
    total = sum(len(hits) for path, lines, hits in records)
    parts = [str(total) + " matching lines; snippets have +/-2 lines of context."] + errors
    skip, shown, room = offset, 0, 4100
    for path, lines, hits in records:
        if skip >= len(hits):
            skip -= len(hits)
            continue
        defs = definitions(path, lines)
        covered = 0
        for number in hits[skip:]:
            if shown >= 12 or room < 350:
                break
            containers = [d for d in defs if d[1] <= number <= d[2]]
            where = min(containers, key=lambda d: d[2] - d[1]) if containers else None
            label = path + ":" + str(number)
            if where:
                label += " in " + where[0] + " (" + str(where[1]) + ":" + str(where[2]) + ")"
            parts.append(label)
            for i in range(max(1, number - 2, covered + 1), min(len(lines), number + 2) + 1):
                raw = lines[i - 1]
                text = raw[:220]
                if len(raw) > 220:
                    text += " ... [line shortened; read this line]"
                entry = str(i) + (">\t" if matcher.search(raw) else " \t") + text
                parts.append(entry)
                room -= len(entry) + 1
            covered = number + 2
            shown += 1
        skip = 0
        if shown >= 12 or room < 350:
            break
    if offset + shown < total:
        more = TOOL + " search " + " ".join(_quote(arg) for arg in args) + " --offset " + str(offset + shown)
        parts.append("[More matching lines: " + more + "]")
    parts.append("[Need surrounding logic? " + TOOL + " read FILE NAME or A:B.]")
    return _finish(parts)


def view(cmd, out, ctx):
    target = read_target(cmd)
    if target is None or not re.match(r"^\s*(?:cat|nl\s+-ba)\s", cmd):
        return None
    path, first, numbered = target
    if path.replace("\\", "/").rsplit("/", 1)[-1].upper() in ("AGENTS.MD", "CLAUDE.MD", "SKILL.MD"):
        return None
    lines = out.splitlines()
    if len(lines) <= READ_LINES and len(out) <= 5000:
        return None
    if numbered:
        lines = [_NUMBERED.sub("", line) for line in lines]
    result = _overview(path, lines)
    return result if len(result) < len(out) else None


TOOLS = {"read": read, "map": map_file, "peek": peek, "search": search}


if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
