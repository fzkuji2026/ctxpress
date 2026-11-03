"""Bounded source reads: spend the output budget on the requested implementation."""
from __future__ import annotations

import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.outline import outline
from ctxpress.tools.definition import definitions

TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
LINES = 96
CHARS = 6500
MAP_ENTRIES = 24
_NUMBERED = re.compile(r"^\s*\d+\t")
_SPAN = re.compile(r"^(\d+)(?:[:-](\d+))?$")

GUIDANCE = (
    "Keep source reads focused: use existing rg -n results to locate the code, then "
    "`" + TOOL + " read FILE A:B [C:D ...]` instead of broad sed/cat batches. "
    "You can substitute an exact definition name (including Type.method) for a range; this reads its body directly. "
    "One invocation shares a 96-line/6500-character budget across all selections, merges overlaps, and gives "
    "an exact continuation for omitted code. Read further when needed to understand or change the implementation. "
    "If the location is already known, request that body directly; an outline or signature preview adds a round trip. "
    "With no selection, `" + TOOL + " read FILE` prints small files or a compact discovery map of long ones. "
    "Use maps only for unfamiliar files, and search for relevant names before paging through a map. "
    "Long standalone whole-file prints also become compact maps. Explicit sed ranges remain available for "
    "larger contiguous code that you need to inspect together."
)


def _quote(text):
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _command(path, first, last):
    return TOOL + " read " + _quote(path) + " " + str(first) + ":" + str(last)


def _protected(path):
    return path.replace("\\", "/").rsplit("/", 1)[-1].lower() in ("agents.md", "skill.md")


def _map(path, lines, start=1):
    """A small discovery result, with a separate path for more declarations."""
    entries = [d for d in outline(lines) if d[0] >= start]
    result = ["{} ({} lines); declaration map".format(path, len(lines))]
    if start == 1:
        for i, line in enumerate(lines[:6]):
            result.append("{:>5}  {}".format(i + 1, line[:160] + (" ..." if len(line) > 160 else "")))
    if entries:
        for first, last, indent, text in entries[:MAP_ENTRIES]:
            result.append("{}-{}  {}".format(first, last, text[:110]))
        if len(entries) > MAP_ENTRIES:
            result.append("[{} more declarations: {} read {} --map {}]".format(
                len(entries) - MAP_ENTRIES, TOOL, _quote(path), entries[MAP_ENTRIES][0]))
        result.append("[Map excludes non-declaration code. Read a needed span: {}]".format(
            _command(path, entries[0][0], min(entries[0][1], entries[0][0] + LINES - 1))))
    else:
        # Text/configuration files without recognized declarations still need useful content.
        first = max(start, 7 if start == 1 else start)
        stop = min(len(lines), first + 25)
        for i in range(first, stop + 1):
            line = lines[i - 1]
            result.append("{:>5}  {}".format(i, line[:180] + (" ..." if len(line) > 180 else "")))
        if stop < len(lines):
            result.append("[{} more lines: {}]".format(
                len(lines) - stop, _command(path, stop + 1, min(len(lines), stop + LINES))))
    return "\n".join(result)


def _body(path, lines, spans, notes=None):
    """Merge requested intervals and enforce one budget, including across names."""
    merged = []
    for first, last in sorted(spans):
        first, last = max(1, first), min(len(lines), last)
        if first > last:
            continue
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
        else:
            merged.append((first, last))
    note = "\n".join(notes or [])[:1000]
    result = [note] if note else []
    result.append("{} ({} lines)".format(path, len(lines)))
    used, count = sum(len(line) + 1 for line in result), 0
    pending = []
    clipped = False
    for first, last in merged:
        if count >= LINES or used >= CHARS:
            pending.append((first, last))
            continue
        header = "--- lines {}-{} ---".format(first, last)
        result.append(header)
        used += len(header) + 1
        for i in range(first, last + 1):
            line = "{:>5}  {}".format(i, lines[i - 1])
            if count >= LINES or used + len(line) + 1 > CHARS:
                if not count and len(line) > CHARS:
                    result.append(line[:max(0, CHARS - used - 80)] + " ... [line clipped]")
                    clipped = True
                    count += 1
                    used = CHARS
                pending.append((i, last))
                break
            result.append(line)
            count += 1
            used += len(line) + 1
    if pending:
        # A normal sed is the explicit, unbounded escape hatch, including for a very long line.
        if clipped:
            first, last = pending[0]
            result.append("[Full line: sed -n '{}p' {}]".format(first, _quote(path)))
            pending[0] = (first + 1, last)
        remaining = [(a, b) for a, b in pending if a <= b]
        if remaining:
            total = sum(b - a + 1 for a, b in remaining)
            selections = " ".join("{}:{}".format(a, b) for a, b in remaining)
            result.append("[{} requested lines omitted; continue: {} read {} {}]".format(
                total, TOOL, _quote(path), selections))
    if not merged:
        result.append("[No lines in the requested range.]")
    return "\n".join(result)


def read(argv):
    """read FILE [A:B|NAME ...], or read FILE --map [START_LINE]."""
    if not argv:
        return "Usage: " + TOOL + " read FILE [A:B|NAME ...] (one shared output budget); FILE --map [START_LINE]"
    path = argv[0]
    if len(path) > 400 or len(argv) > 17 or any(len(arg) > 256 for arg in argv[1:]):
        return "Use one file and at most 16 focused selections per invocation (names/ranges up to 256 characters)."
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except (OSError, ValueError) as error:
        return ("Cannot read {}: {}".format(path, error))[:1500]
    args = argv[1:]
    if args and args[0] == "--map":
        if len(args) > 2 or (len(args) == 2 and not args[1].isdigit()):
            return "Usage: " + TOOL + " read FILE --map [START_LINE]"
        return _map(path, lines, max(1, int(args[1])) if len(args) == 2 else 1)
    if not args:
        if _protected(path) or (len(lines) <= LINES and sum(len(s) + 1 for s in lines) <= CHARS):
            return _body(path, lines, [(1, len(lines))])
        return _map(path, lines)
    spans, notes = [], []
    defs = None
    for arg in args:
        match = _SPAN.match(arg)
        if match:
            first = int(match.group(1))
            last = int(match.group(2) or first)
            if first < 1 or last < first:
                notes.append("Invalid range: " + arg[:80])
            else:
                spans.append((first, last))
            continue
        if defs is None:
            defs = definitions(path, lines)
        hits = [d for d in defs if d[0] == arg]
        if not hits:
            hits = [d for d in defs if d[0].endswith("." + arg)]
        if len(hits) == 1:
            spans.append((hits[0][1], hits[0][2]))
        elif hits:
            notes.append("Ambiguous {}: {}. Select a qualified name or range.".format(
                arg[:80], "; ".join("{} ({}:{})".format(q[:80], a, b) for q, a, b in hits[:6])))
        else:
            notes.append("No definition {}. Use rg -n to locate it, then request its line range.".format(arg[:80]))
    return _body(path, lines, spans, notes[:8])


def view(cmd, out, ctx):
    # Only reshape a recognized standalone whole-file read; never guess boundaries of chained outputs.
    if not re.match(r"^\s*(cat|nl\s+-ba)\s", cmd):
        return None
    target = read_target(cmd)
    if target is None:
        return None
    path, first, numbered = target
    if _protected(path):
        return None
    lines = out.splitlines()
    if len(lines) <= LINES and len(out) <= CHARS:
        return None
    if numbered:
        lines = [_NUMBERED.sub("", line) for line in lines]
    compact = _map(path, lines)
    compact += "\n[Whole-file view shortened; select relevant names/ranges above. Explicit sed ranges show original source.]"
    return compact if len(compact) < len(out) else None


TOOLS = {"read": read}

if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
