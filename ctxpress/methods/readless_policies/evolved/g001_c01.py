"""Small source maps, API previews, and bounded, explicit implementation reads."""
from __future__ import annotations

import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.outline import render
from ctxpress.tools.definition import definitions, find

TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
LIMIT = 12000
READ_LINES = 120

GUIDANCE = (
    "Keep source exploration small: broad chained cat/sed reads accumulate in your history. "
    "Use these commands inside your shell chains (quote selectors):\n"
    "`" + TOOL + " peek FILE [FILE ...]` gives compact file maps with definition spans. "
    "`" + TOOL + " peek 'FILE::Name' [...]` shows signatures, leading docs/comments, and member names, "
    "without implementation bodies. Qualify methods as Type.Method.\n"
    "`" + TOOL + " search 'REGEX' FILE [FILE ...]` shows matching lines with two context lines and "
    "the enclosing definition's name/span; use this first when following names or behavior across known files. "
    "Use rg to discover paths.\n"
    "`" + TOOL + " read 'FILE::Name' ['OTHER::Name' ...]` reads the selected implementations with line numbers; "
    "`" + TOOL + " read 'FILE::A:B' [...]` reads exact inclusive ranges (also imports, constants, or nearby tests). "
    "Batch related selectors in one call. Bare FILE reads its first 120 lines. Long reads give a continuation range. "
    "Start with search or maps for unfamiliar files; once you know the relevant function, read its body directly. "
    "Previews omit bodies: read all relevant logic before editing, and expand omitted regions when needed. "
    "Long whole-file source cats become maps; repeating that command retrieves the original output. "
    "Instructions, configs, tests/log output, and explicit sed reads remain available normally."
)


def _quote(value):
    # POSIX single quoting, including embedded apostrophes, without extra imports.
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command(name, selector):
    return TOOL + " " + name + " " + _quote(selector)


def _finish(parts):
    text = "\n\n".join(parts)
    if len(text) <= LIMIT:
        return text
    cut = text.rfind("\n", 0, LIMIT - 250)
    if cut < 0:
        cut = LIMIT - 250
    return text[:cut] + (
        "\n[Output limit reached; some results omitted. Request fewer selectors/files or a smaller line range. "
        "For complete unusually long lines, use sed -n 'A,Bp' FILE.]"
    )


def _load(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return stream.read().splitlines(), None
    except (OSError, ValueError) as exc:
        return [], path + ": " + str(exc)


def _definitions(path, lines):
    try:
        return definitions(path, lines)
    except (SyntaxError, ValueError, RecursionError, IndexError):
        return []


def _rows(lines, start, end, compact=False):
    result = []
    for i in range(max(1, start), min(end, len(lines)) + 1):
        line = lines[i - 1]
        if compact and len(line) > 350:
            line = line[:350] + " ... [long line; use read]"
        result.append("%6d\t%s" % (i, line))
    return result


def _map(path, lines):
    # Keep large member lists from making the navigation tool itself a large read.
    result = [render(path, lines, max_entries=45)]
    result.append("First lines:")
    result.extend(_rows(lines, 1, min(5, len(lines)), compact=True))
    result.append("Bodies/imports/constants: " + _command("read", path + "::NAME") +
                  " or " + _command("read", path + "::A:B"))
    return "\n".join(result)


def _split(selector):
    if "::" in selector:
        return selector.split("::", 1)
    return selector, ""


def _matches(defs, name):
    return find(defs, name)


def _missing(path, name, defs):
    near = [q for q, s, e in defs if name.lower() in q.lower()][:12]
    return (path + ": no definition named " + name +
            ("; similar: " + ", ".join(near) if near else "") +
            ". Search text or map with " + _command("peek", path))


def _preview(path, lines, item, defs):
    name, start, end = item
    end = min(end, len(lines))
    result = ["%s:%d-%d  %s (API preview; body omitted)" % (path, start, end, name)]
    # Preserve a few preceding declaration comments, but never claim they are the body.
    before = start
    while before > max(1, start - 4):
        text = lines[before - 2].strip()
        if text.startswith(("//", "#", "/*", "*")):
            before -= 1
        else:
            break
    stop = start
    for i in range(start, min(end, start + 11) + 1):
        stop = i
        text = lines[i - 1].rstrip()
        if text.endswith((":", ";")) or "{" in text:
            break
        if i == start and not text.lstrip().startswith(("@", "def ", "async def ", "class ", "func ")):
            # Single-line declarations from languages not using braces.
            if "(" not in text or ")" in text:
                break
    # Python's leading docstring belongs to the API, not to the hidden body.
    if stop < end:
        doc = lines[stop].lstrip()
        match = re.match(r"(?i)^[ru]*(\"\"\"|''')", doc)
        if match:
            quote = match.group(1)
            doc_end = stop + 1
            if quote not in doc[match.end():]:
                for i in range(stop + 1, min(end, stop + 12)):
                    doc_end = i + 1
                    if quote in lines[i]:
                        break
            stop = doc_end
    result.extend(_rows(lines, before, min(stop, end), compact=True))
    members = [(q, s, e) for q, s, e in defs
               if q.startswith(name + ".") and q.count(".") == name.count(".") + 1]
    if members:
        result.append("Members: " + ", ".join("%s [%d:%d]" % d for d in members[:20]))
        if len(members) > 20:
            result.append("%d more members; search their names." % (len(members) - 20))
    result.append("Implementation: " + _command("read", path + "::" + name))
    return "\n".join(result)


def peek(argv):
    if not argv:
        return "Usage: " + TOOL + " peek FILE [FILE::NAME ...]"
    parts = []
    for selector in argv:
        path, name = _split(selector)
        lines, error = _load(path)
        if error:
            parts.append(error)
        elif not name:
            parts.append(_map(path, lines))
        else:
            defs = _definitions(path, lines)
            hits = _matches(defs, name)
            if not hits:
                parts.append(_missing(path, name, defs))
            for item in hits[:5]:
                parts.append(_preview(path, lines, item, defs))
            if len(hits) > 5:
                parts.append("More matches omitted; qualify the name: " +
                             ", ".join(q for q, s, e in hits[5:15]))
        if sum(len(p) for p in parts) > LIMIT:
            break
    return _finish(parts)


def _read_span(path, lines, name, start, end):
    start, end = max(1, start), min(end, len(lines))
    if start > end:
        return "%s: empty range %d:%d (%d lines in file)" % (path, start, end, len(lines))
    stop = min(end, start + READ_LINES - 1)
    parts = ["%s:%d-%d%s" % (path, start, end, "  " + name if name else "")]
    parts.extend(_rows(lines, start, stop))
    if stop < end:
        parts.append("[%d lines omitted; continue: %s]" %
                     (end - stop, _command("read", "%s::%d:%d" % (path, stop + 1, end))))
    return "\n".join(parts)


def read(argv):
    if not argv:
        return "Usage: " + TOOL + " read FILE::NAME [FILE::A:B ...]; bare FILE reads first 120 lines"
    parts = []
    for selector in argv:
        path, name = _split(selector)
        lines, error = _load(path)
        if error:
            parts.append(error)
            continue
        span = re.fullmatch(r"(\d*):(\d*)", name)
        if not name or span:
            start = int(span.group(1) or "1") if span else 1
            end = int(span.group(2)) if span and span.group(2) else len(lines)
            parts.append(_read_span(path, lines, "", start, end))
        else:
            defs = _definitions(path, lines)
            hits = _matches(defs, name)
            if not hits:
                parts.append(_missing(path, name, defs))
            for q, start, end in hits[:5]:
                parts.append(_read_span(path, lines, q, start, end))
            if len(hits) > 5:
                parts.append("More matches omitted; qualify the name: " +
                             ", ".join(q for q, s, e in hits[5:15]))
        if sum(len(p) for p in parts) > LIMIT:
            break
    return _finish(parts)


def search(argv):
    if len(argv) < 2:
        return "Usage: " + TOOL + " search [-i] REGEX FILE [FILE ...]"
    flags = 0
    if argv[0] == "-i":
        flags, argv = re.I, argv[1:]
    if len(argv) < 2:
        return "Supply a regex and at least one file."
    try:
        pattern = re.compile(argv[0], flags)
    except re.error as exc:
        return "Invalid regex: " + str(exc)
    parts, shown = [], 0
    for path in argv[1:]:
        lines, error = _load(path)
        if error:
            parts.append(error)
            continue
        hits = [i + 1 for i, line in enumerate(lines) if pattern.search(line)]
        if not hits:
            parts.append(path + ": no matches")
            continue
        if shown >= 24:
            parts.append(path + ": %d matching lines omitted; narrow the search." % len(hits))
            continue
        defs = _definitions(path, lines)
        selected = hits[:min(12, 24 - shown)]
        spans = []
        for n in selected:
            start, end = max(1, n - 2), min(len(lines), n + 2)
            if spans and start <= spans[-1][1] + 1:
                spans[-1][1] = end
            else:
                spans.append([start, end])
        result = ["%s: %d matching lines" % (path, len(hits))]
        for start, end in spans:
            names = []
            for n in selected:
                if start <= n <= end:
                    enclosing = [d for d in defs if d[1] <= n <= d[2]]
                    if enclosing:
                        q, s, e = min(enclosing, key=lambda d: d[2] - d[1])
                        label = "%s [%d:%d]" % (q, s, e)
                        if label not in names:
                            names.append(label)
            result.append("%s:%d-%d%s" % (path, start, end, "  " + ", ".join(names) if names else ""))
            result.extend(_rows(lines, start, end, compact=True))
        shown += len(selected)
        if len(selected) < len(hits):
            result.append("[%d matching lines omitted; narrow the regex or search fewer files.]" %
                          (len(hits) - len(selected)))
        parts.append("\n".join(result))
        if sum(len(p) for p in parts) > LIMIT:
            break
    return _finish(parts)


def view(cmd, out, ctx):
    target = read_target(cmd)
    if target is None or not re.match(r"^\s*(cat|nl\s+-ba)\s", cmd):
        return None
    path, first, numbered = target
    if not path.endswith((".py", ".pyi", ".pyx", ".go", ".rs", ".js", ".jsx", ".ts", ".tsx",
                          ".c", ".h", ".cc", ".cpp", ".hpp", ".java", ".kt", ".swift", ".rb")):
        return None
    lines = out.splitlines()
    if len(lines) < 120:
        return None
    if numbered:
        lines = [re.sub(r"^\s*\d+\t", "", line) for line in lines]
    if not _definitions(path, lines):
        return None
    compact = _finish([_map(path, lines),
                       "[Source map substituted for whole-file output. Read a named body or exact range above; "
                       "repeat the original command to retrieve all original output.]"])
    if len(compact) >= len(out):
        return None
    tokens = ctx.get("tokens")
    if tokens is not None and tokens(compact) >= tokens(out):
        return None
    return compact


TOOLS = {"peek": peek, "read": read, "search": search}

if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
