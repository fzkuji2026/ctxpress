"""Bounded, progressive source reads with batched selectors and search context."""
from __future__ import annotations

import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.definition import definitions, find


TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
MAX_CHARS = 16000
PAGE = 160
SHORT = 55
ENTRIES = 50
_NUMBERED = re.compile(r"^\s*\d+\t")
_SOURCE = re.compile(r"\.(?:py|pyi|pyx|pxd|go|[cm]?[jt]sx?|c|cc|cpp|cxx|h|hh|hpp|hxx|rs|java|kt|kts|cs|swift|rb|php)$", re.I)

GUIDANCE = (
    "Use compact source reads in place of chained sed/cat or large definition dumps. Command prefix: `" + TOOL + "`. "
    "`" + TOOL + " read FILE [NAME ...]` shows a file map, or named definitions (qualify ambiguous names, e.g. "
    "Class.method). Short functions are printed in full; large definitions show a labeled preview and member map. "
    "`" + TOOL + " read FILE NAME --full` reads the implementation; use this before changing or reasoning about "
    "omitted code. `" + TOOL + " lines FILE START END` reads an exact numbered span, up to 160 lines per page. "
    "For a known identifier, prefer `" + TOOL + " search FILE 'TEXT' ['TEXT' ...]`: literal matches, nearby source, "
    "and enclosing definition names in one read (use --regex for a regex). This also finds fields, tests, and "
    "declarations the definition parser misses. Batch independent questions in one command using ` -- ` between "
    "file/selector groups, e.g. `" + TOOL + " read a.py Foo -- b.go Bar Baz`. All tools accept --after LINE to "
    "continue a map, search, or full read after that source line. Read task instructions normally; use ordinary "
    "search to locate files. Long whole-file source cats may become maps; repeating the original command reveals "
    "the file. Prefer requesting the relevant implementation rather than replaying a whole large file."
)


def _quote(value):
    # POSIX single quotes preserve spaces and shell metacharacters literally.
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _load(path):
    with open(path, encoding="utf-8", errors="replace") as stream:
        return stream.read().splitlines()


def _number(lines, first, last):
    return ["%6d\t%s" % (i, lines[i - 1])
            for i in range(max(1, first), min(last, len(lines)) + 1)]


def _finish(parts):
    text = "\n".join(parts)
    if len(text) <= MAX_CHARS:
        return text
    end = text.rfind("\n", 0, MAX_CHARS - 250)
    if end < 0:
        end = MAX_CHARS - 250
    return (text[:end] + "\n[Output budget reached; the rest is omitted. Request fewer names/files, "
            "or use lines FILE START END from the last displayed source line.]")


def _options(argv):
    args, full, regex, after = [], False, False, 0
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--full":
            full = True
        elif arg == "--regex":
            regex = True
        elif arg == "--after":
            i += 1
            if i >= len(argv):
                raise ValueError("--after requires a source line number")
            after = max(0, int(argv[i]))
        else:
            args.append(arg)
        i += 1
    groups, group = [], []
    for arg in args + ["--"]:
        if arg == "--":
            if group:
                groups.append(group)
            group = []
        else:
            group.append(arg)
    return groups, full, regex, after


def _map(path, lines, defs, after=0, head=True):
    out = ["%s (%d lines); definition map" % (path, len(lines))]
    if head and after == 0:
        out += _number(lines, 1, min(8, len(lines)))
    selected = [d for d in defs if d[1] > after]
    for name, start, end in selected[:ENTRIES]:
        signature = lines[start - 1].strip() if 0 < start <= len(lines) else ""
        out.append("%6d-%-6d %s | %s" % (start, end, name, signature[:135]))
    if len(selected) > ENTRIES:
        out.append("[%d more definitions; continue: %s read %s --after %d]" %
                   (len(selected) - ENTRIES, TOOL, _quote(path), selected[ENTRIES - 1][1]))
    if not selected:
        out.append("[No definitions found here; search for text or request a line span.]")
    return out


def _span(path, lines, start, end, after=0):
    first = max(1, start, after + 1)
    end = min(end, len(lines))
    stop = min(end, first + PAGE - 1)
    out = ["%s:%d-%d" % (path, first, end)]
    if first > end:
        return out + ["[No lines in this range.]"]
    out += _number(lines, first, stop)
    if stop < end:
        out.append("[%d lines remain in this span: %s lines %s %d %d]" %
                   (end - stop, TOOL, _quote(path), stop + 1, end))
    return out


def _definition(path, lines, item, defs, full, after):
    name, start, end = item
    end = min(end, len(lines))
    if full or end - start + 1 <= SHORT:
        return ["%s (%s)" % (name, "implementation" if full else "complete")] + _span(path, lines, start, end, after)
    out = ["%s:%d-%d %s [preview; body omitted in part]" % (path, start, end, name)]
    # Preserve the signature and leading documentation, plus the start of the body.
    out += _number(lines, start, min(start + 19, end))
    inner = [d for d in defs if d[1] > start and d[2] <= end]
    if inner:
        out.append("  ... members / nested definitions:")
        for q, a, b in inner[:35]:
            out.append("%6d-%-6d %s | %s" % (a, b, q, lines[a - 1].strip()[:120]))
        if len(inner) > 35:
            out.append("[%d more members; search for the relevant name.]" % (len(inner) - 35))
    else:
        out.append("  ... %d lines omitted ..." % max(0, end - start - 24))
        out += _number(lines, max(start + 20, end - 4), end)
    out.append("[Read body: %s read %s %s --full; or request a member / exact span.]" %
               (TOOL, _quote(path), _quote(name)))
    return out


def _search(path, lines, terms, defs, regex=False, after=0):
    if not terms:
        return ["search requires FILE TEXT [TEXT ...]"]
    patterns = [re.compile(t if regex else re.escape(t)) for t in terms]
    hits = [i for i, line in enumerate(lines, 1)
            if i > after and any(p.search(line) for p in patterns)]
    out = ["%s: %d matching lines%s" % (path, len(hits), " after %d" % after if after else "")]
    if not hits:
        return out
    shown = hits[:12]
    contexts = []
    for hit in shown:
        a, b = max(1, hit - 2), min(len(lines), hit + 3)
        if contexts and a <= contexts[-1][1] + 1:
            contexts[-1][1] = max(contexts[-1][1], b)
        else:
            contexts.append([a, b])
    for a, b in contexts:
        owners = []
        for hit in shown:
            if not a <= hit <= b:
                continue
            enclosing = [d for d in defs if d[1] <= hit <= d[2]]
            if enclosing:
                q, s, e = min(enclosing, key=lambda d: d[2] - d[1])
                label = "%s (%d-%d)" % (q, s, e)
                if label not in owners:
                    owners.append(label)
        out.append("-- %d-%d%s" % (a, b, "; " + ", ".join(owners) if owners else ""))
        out += _number(lines, a, b)
    if len(hits) > len(shown):
        out.append("[%d more matches; narrow the terms or repeat search with --after %d.]" %
                   (len(hits) - len(shown), shown[-1]))
    return out


def _run(argv, mode):
    try:
        groups, full, regex, after = _options(argv)
    except (ValueError, TypeError) as error:
        return "Invalid arguments: %s" % error
    if not groups:
        return ("Usage: %s %s FILE %s; separate file groups with --" %
                (TOOL, mode, {"read": "[NAME ...] [--full]", "search": "TEXT [TEXT ...] [--regex]",
                              "lines": "START END"}[mode]))
    parts = []
    for index, group in enumerate(groups):
        path, selectors = group[0], group[1:]
        try:
            lines = _load(path)
            if mode == "lines":
                if len(selectors) != 2:
                    raise ValueError("lines requires FILE START END")
                parts += _span(path, lines, int(selectors[0]), int(selectors[1]), after)
            elif mode == "read" and full and not selectors:
                parts += _span(path, lines, 1, len(lines), after)
            else:
                defs = definitions(path, lines)
                if mode == "search":
                    parts += _search(path, lines, selectors, defs, regex, after)
                elif not selectors:
                    if len(lines) <= SHORT and after == 0:
                        parts += _span(path, lines, 1, len(lines))
                    else:
                        parts += _map(path, lines, defs, after)
                else:
                    emitted = set()
                    for selector in selectors:
                        matches = find(defs, selector)
                        if not matches:
                            parts.append("%s: no parsed definition %s; text matches:" % (path, selector))
                            parts += _search(path, lines, [selector], defs, False, after)
                        else:
                            if len(matches) > 3:
                                parts.append("[%s matches %d definitions; first 3 shown. Qualify the name.]" %
                                             (selector, len(matches)))
                            for item in matches[:3]:
                                if item not in emitted:
                                    parts += _definition(path, lines, item, defs, full, after)
                                    emitted.add(item)
        except (OSError, ValueError, TypeError, re.error) as error:
            parts.append("%s: %s" % (path, error))
        if sum(len(p) + 1 for p in parts) > MAX_CHARS - 500:
            if index + 1 < len(groups):
                parts.append("[%d file groups not shown; request them separately.]" % (len(groups) - index - 1))
            break
    return _finish(parts)


def read(argv):
    return _run(argv, "read")


def search(argv):
    return _run(argv, "search")


def lines(argv):
    return _run(argv, "lines")


def view(cmd, out, ctx):
    # Only reshape a recognized whole-file source read, never test output or mixed commands.
    if not re.match(r"^\s*(?:cat|nl\s+-ba)\s", cmd):
        return None
    target = read_target(cmd)
    if target is None:
        return None
    path, first, numbered = target
    if not _SOURCE.search(path) or first != 1:
        return None
    body = out.splitlines()
    if len(body) < 110:
        return None
    if numbered:
        body = [_NUMBERED.sub("", line) for line in body]
    defs = definitions(path, body)
    if not defs:
        return None
    shown = _finish(_map(path, body, defs) + [
        "[Source map; bodies omitted. Read: %s read %s NAME --full. "
        "Repeating the original command reveals the whole file.]" % (TOOL, _quote(path))])
    if len(shown) >= len(out):
        return None
    count = ctx.get("tokens")
    if count is not None and count(shown) >= count(out):
        return None
    return shown


TOOLS = {"read": read, "search": search, "lines": lines}

if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
