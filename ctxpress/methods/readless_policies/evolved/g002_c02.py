"""Bound exploratory reads while preserving direct access to implementation."""
from __future__ import annotations
import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.outline import outline
from ctxpress.tools.definition import definitions, find

TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
READ_LINES = 120
READ_CHARS = 10000
MAP_ENTRIES = 32
_NUMBERED = re.compile(r"^\s*\d+\t")
_RANGE = re.compile(r"^(\d+)(?::|-)(\d+)$")

GUIDANCE = (
    "Keep exploratory reads focused on the change: first locate relevant symbols with rg -n in likely paths, "
    "then read the implementation you need. Use `" + TOOL + " read FILE A:B [A:B ...]` or "
    "`" + TOOL + " read FILE NAME [NAME ...]` inside shell commands. These return numbered source directly, "
    "sharing a 120-line/10000-character budget per invocation; omissions include exact expansion commands. "
    "For an unfamiliar file only, `" + TOOL + " map FILE [REGEX]` lists at most 32 definition spans "
    "(REGEX filters signatures). Skip the map when you already know the name or range. "
    "Prefer these bounded reads over broad cat/sed dumps; batch independent focused reads, but do not print "
    "every related file or combine a map with bodies for the same file. Expand omitted code when a concrete "
    "question or edit needs it; you need not read every omitted line. Long whole-file cat output may become "
    "a compact map; use an explicit range for its body. Read enough surrounding code before editing."
)


def _quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command(path, start, end):
    return TOOL + " read " + _quote(path) + " " + str(start) + ":" + str(end)


def _load(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _bounded(text):
    if len(text) <= 18000:
        return text
    return text[:17500] + "\n[Output capped; request one narrower range or map filter.]"


def _map_text(path, lines, pattern="", offset=0, entries=MAP_ENTRIES):
    items = outline(lines)
    if pattern:
        regex = re.compile(pattern)
        items = [item for item in items if regex.search(item[3])]
    offset = max(0, offset)
    result = [str(path) + " (" + str(len(lines)) + " lines; " + str(len(items)) + " definitions)"]
    if not items:
        result.append("No matching definitions; locate text with rg -n, then read its line range.")
        return "\n".join(result)
    shown = 0
    size = len(result[0])
    for start, end, indent, signature in items[offset:offset + entries]:
        row = "%d-%d  %s" % (start, end, signature[:150])
        if size + len(row) > 4600:
            break
        result.append(row)
        shown += 1
        size += len(row) + 1
    if offset + shown < len(items):
        result.append("[%d more definitions; filter signatures or continue: %s map %s %s %d]" % (
            len(items) - offset - shown, TOOL, _quote(path), _quote(pattern), offset + shown))
    return "\n".join(result)


def map_file(argv):
    if not argv or len(argv) > 3:
        return "usage: " + TOOL + " map FILE [REGEX [OFFSET]]"
    try:
        return _bounded(_map_text(argv[0], _load(argv[0]),
                                  argv[1] if len(argv) > 1 else "",
                                  int(argv[2]) if len(argv) > 2 else 0))
    except (OSError, ValueError, re.error) as error:
        return _bounded(str(error))


def read(argv):
    if not argv:
        return "usage: " + TOOL + " read FILE [A:B | NAME ...]"
    path = argv[0]
    try:
        lines = _load(path)
    except OSError as error:
        return _bounded(str(error))
    count = len(lines)
    if not count:
        return str(path) + ": empty file"
    selectors = argv[1:]
    if not selectors:
        selectors = ["1:" + str(count)]
    if len(selectors) > 8:
        return "Request at most eight names/ranges per read; all share the output budget."
    spans, messages = [], []
    defs = None
    for selector in selectors:
        match = _RANGE.match(selector)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if start < 1 or end < start or start > count:
                messages.append("Invalid range %s: %s has %d lines." % (selector, path, count))
                continue
            spans.append((start, min(end, count)))
        else:
            if defs is None:
                defs = definitions(path, lines)
            hits = find(defs, selector)
            if not hits:
                messages.append("No definition %s; locate it with rg -n or map FILE REGEX." % selector)
                continue
            if len(hits) > 3:
                messages.append("Ambiguous %s: %s; qualify the name or request a line range." % (
                    selector, ", ".join("%s:%d-%d" % hit for hit in hits[:6])))
                continue
            spans.extend((max(1, start), min(end, count)) for name, start, end in hits)
    # Merge overlapping requests so class/method or overlapping ranges are never printed twice.
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    result = [str(path) + " (%d lines)" % count] + messages
    remaining_lines, remaining_chars = READ_LINES, READ_CHARS
    for start, end in merged:
        result.append("[%d-%d]" % (start, end))
        cursor = start
        while cursor <= end and remaining_lines > 0:
            row = "%d\t%s" % (cursor, lines[cursor - 1])
            if len(row) + 1 > remaining_chars:
                break
            result.append(row)
            remaining_chars -= len(row) + 1
            remaining_lines -= 1
            cursor += 1
        if cursor <= end:
            if cursor == start and len(lines[cursor - 1]) + 16 > READ_CHARS:
                # A single huge/minified line cannot fit: give an unbounded shell escape.
                result.append("[%d-%d omitted; unusually long line. Read explicitly: sed -n %s %s]" % (
                    cursor, end, _quote(str(cursor) + "," + str(end) + "p"), _quote(path)))
            else:
                result.append("[%d-%d omitted (%d lines); expand if needed: %s]" % (
                    cursor, end, end - cursor + 1, _command(path, cursor, end)))
    return _bounded("\n".join(result))


def view(cmd, out, ctx):
    # read_target deliberately limits this to recognizable standalone whole-file reads.
    if not re.match(r"^\s*(cat|nl\s+-ba)\s", cmd):
        return None
    target = read_target(cmd)
    if target is None:
        return None
    path, first, numbered = target
    basename = re.split(r"[/\\]", path)[-1].lower()
    if basename in ("agents.md", "agents.override.md", "skill.md", "claude.md", "instructions.md"):
        return None
    body = out.splitlines()
    if len(body) <= READ_LINES and len(out) <= READ_CHARS:
        return None
    if numbered:
        body = [_NUMBERED.sub("", line) for line in body]
    # Do not replace prose/configuration/error output with an empty source outline.
    if not outline(body):
        return None
    summary = _map_text(path, body, entries=24)
    footer = ("[Whole-file body omitted. Read a needed definition or range: " + TOOL
              + " read " + _quote(path) + " NAME  |  " + _command(path, 1, min(120, len(body))) + "]")
    result = summary + "\n" + footer
    if len(result) >= len(out):
        return None
    tokens = ctx.get("tokens")
    if tokens is not None and tokens(result) >= tokens(out):
        return None
    return result


TOOLS = {"read": read, "map": map_file}

if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))

