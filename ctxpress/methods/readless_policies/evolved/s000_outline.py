"""Seed s000_outline of the tool evolution (ctxpress.evolve): the hand-written outline policy, unchanged in what the
agent sees (its runs in G1 are this seed's measurements), in the evolvable format (ctxpress.evolve.policy).
"""
from __future__ import annotations
import re
from ctxpress.evolve.runtime import cli
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


TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
TOOLS = {}        # the seed's commands are ctxpress.tools.outline / ctxpress.tools.definition, named in GUIDANCE

if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
