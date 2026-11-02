"""Outline-first read policy for ReadLess: a long whole-file print is shown as the file's outline (every definition
with its exact line span, from ctxpress.tools.outline) plus its first lines; the agent then prints only the span it
needs. Ranged prints, searches, test logs and compound outputs are left alone (use ReadLess(ranged=False)).
GUIDANCE tells the agent about the outline command, which it can chain inside its own commands at no extra request.
"""
from __future__ import annotations
import re
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.outline import render

READ_LINES = 200        # a whole-file print shorter than this is shown in full
HEAD = 10
_NUMBERED = re.compile(r"^\s*(\d+)\t")

GUIDANCE = ("To find your way in a source file, print its outline instead of the whole file: "
            "`python3 -m ctxpress.tools.outline FILE [FILE ...]` lists every definition with its exact line span "
            "(e.g. `  120-188    func (t *Table) Refresh()`), so you can then print only the span you need with "
            "`sed -n '120,188p' FILE`. It works inside your chained commands. A long whole-file print (cat FILE) is "
            "shown to you as this outline; running the same command again prints the full file.")


def view(cmd, out, ctx):
    target = read_target(cmd)
    if target is None or not re.match(r"^\s*(cat|nl\s+-ba)\s", cmd):
        return None
    lines = out.split("\n")
    if len(lines) < READ_LINES:
        return None
    path, first, numbered = target
    body = [_NUMBERED.sub("", l) for l in lines] if numbered else lines
    head = [f"{i + 1:>6}\t{l}" for i, l in enumerate(body[:HEAD])]
    full = ctx["tokens"](out)
    return "\n".join(head + ["", render(path, body), (
        f"[ctxpress outline view: {path} has {len(body)} lines (about {full} tokens); shown as its first {HEAD} lines "
        f"and its outline. Print a span with: sed -n 'A,Bp' {path}. Running the same command again prints all "
        f"{len(body)} lines.]")])
