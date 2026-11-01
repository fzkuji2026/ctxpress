"""Hand-written seed view policy for ReadLess (the starting point of the automatic search).

read    a long file print (cat / nl -ba / sed -n A,Bp, alone or piped) -> the first lines, then every definition
        line with its line number; footer: how to print exact lines.
search  a long grep / rg listing -> the first matches of each file and how many more there are.
test    a long test or build log -> the failure lines and the last lines.
mixed   a long output of a compound read-only command (head …; cat …; ls …) -> the first lines, the definition
        lines and the last lines.
Anything else, or a short output: None (unchanged).
"""
from __future__ import annotations
import re
from ctxpress.core import textops

READ_LINES = 150        # a read shorter than this is shown in full
HEAD = 15               # lines kept from the top of a long read
SEARCH_LINES = 120
PER_FILE = 3
TEST_LINES = 150

_FILE = r"([\w./@+-]+\.[A-Za-z0-9]+)"
_SEDR = re.compile(r"^\s*sed\s+-n\s+['\"]?(\d+),(\d+)p['\"]?\s+" + _FILE + r"\s*$")
_NLSED = re.compile(r"^\s*nl\s+-ba\s+" + _FILE + r"\s*\|\s*sed\s+-n\s+['\"]?(\d+),(\d+)p['\"]?\s*$")
_WHOLE = re.compile(r"^\s*(cat|nl\s+-ba)\s+" + _FILE + r"\s*$")
_SEARCH = re.compile(r"^\s*(grep|rg|git\s+grep)\b")
_TEST = re.compile(r"\b(go test|cargo test|pytest|python3? -m pytest|npm (run )?test|yarn test|pnpm test|make test|mvn|gradle|jest|vitest|ctest)\b")
_NUMBERED = re.compile(r"^\s*(\d+)\t")
TAIL = 5
# a compound command that only prints files and listings (head …; cat …; ls …)
_READS = re.compile(r"(^|[;&|]\s*)(cat|head|tail|sed\s+-n|nl|git\s+(show|diff))\b")   # a listing alone stays whole
_WRITES = re.compile(r"(>\s*[\w/.]|\bsed\s+-i\b|\btee\b|\bpython3?\b|\bnode\b|\bgo\s+(run|build|test)\b|\bcargo\b|\bnpm\b|\bmake\b|apply_patch)")
_NOTE = re.compile(r"(No such file|not found|Permission denied|^==> .* <==$)")


def read_target(cmd):
    """(path, first line number, lines are already numbered) of a plain file print, else None."""
    c = cmd.strip()
    m = _NLSED.match(c)
    if m:
        return m.group(1), int(m.group(2)), True
    m = _SEDR.match(c)
    if m:
        return m.group(3), int(m.group(1)), False
    m = _WHOLE.match(c)
    if m:
        return m.group(2), 1, m.group(1).startswith("nl")
    return None


def outline(lines, first, numbered):
    out = []
    for i, line in enumerate(lines):
        n = int(_NUMBERED.match(line).group(1)) if numbered and _NUMBERED.match(line) else first + i
        body = _NUMBERED.sub("", line) if numbered else line
        if i < HEAD or textops.SIG.match(body):
            out.append(f"{n:>6}\t{body}")
        elif out and out[-1] != "   ...":
            out.append("   ...")
    return out


def view(cmd, out, ctx):
    lines = out.split("\n")
    target = read_target(cmd)
    if target is not None:
        if len(lines) < READ_LINES:
            return None
        path, first, numbered = target
        shown = outline(lines, first, numbered)
        last = first + len(lines) - 1
        return "\n".join(shown + [
            f"[ctxpress read view: {path} lines {first}-{last} ({len(lines)} lines) shown as the first {HEAD} lines and "
            f"the definition lines. Print exact lines with: sed -n 'A,Bp' {path}. Running the same command again "
            f"returns the full text.]"])
    if _SEARCH.match(cmd.strip()):
        if len(lines) < SEARCH_LINES:
            return None
        per, order, more = {}, [], {}
        for line in lines:
            f = line.split(":", 1)[0] if ":" in line else line
            if f not in per:
                per[f] = []; order.append(f); more[f] = 0
            if len(per[f]) < PER_FILE:
                per[f].append(line)
            else:
                more[f] += 1
        shown = []
        for f in order:
            shown += per[f]
            if more[f]:
                shown.append(f"{f}: (+{more[f]} more matches)")
        return "\n".join(shown + [f"[ctxpress search view: {len(lines)} lines, first {PER_FILE} matches per file. "
                                  f"Narrow the search, or run the same command again for the full list.]"])
    if _TEST.search(cmd):
        if len(lines) < TEST_LINES:
            return None
        kept = textops.structured(out, "run")
        return kept + f"\n[ctxpress test view: {len(lines)} lines, failure lines and the last lines kept. Run the same " \
                      f"command again for the full log.]"
    if _READS.search(cmd) and not _WRITES.search(cmd):
        if len(lines) < READ_LINES:
            return None
        shown = []
        for i, line in enumerate(lines):
            if i < HEAD or i >= len(lines) - TAIL or textops.SIG.match(line) or _NOTE.search(line):
                shown.append(line)
            elif shown and shown[-1] != "   ...":
                shown.append("   ...")
        return "\n".join(shown + [
            f"[ctxpress read view: {len(lines)} lines from several commands, shown as the first {HEAD} lines, the "
            f"definition lines and the last {TAIL} lines. Print exact lines with: sed -n 'A,Bp' FILE (or grep -n), "
            f"or run the same command again for the full text.]"])
    return None
