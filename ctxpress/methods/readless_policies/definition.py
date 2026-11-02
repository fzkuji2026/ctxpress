"""Definition-first read policy for ReadLess: like the outline policy, but the agent reads code by definition name.

A long whole-file print is shown as the file's first lines and its outline (every definition with its line span); the
footer and GUIDANCE point to `python3 -m ctxpress.tools.definition FILE NAME`, which prints just that definition's full
text, so the agent does not pick line ranges itself. Ranged prints, searches, test logs and compound outputs are left
alone (use ReadLess(ranged=False)).
"""
from __future__ import annotations
import re
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.outline import render

READ_LINES = 200        # a whole-file print shorter than this is shown in full
HEAD = 10
_NUMBERED = re.compile(r"^\s*(\d+)\t")

GUIDANCE = ("Read source code by definition, not by line range: `python3 -m ctxpress.tools.definition FILE NAME "
            "[NAME ...]` prints the full text of each named function, class or method with its line numbers "
            "(qualify with the enclosing type when a name is ambiguous: `Table.Refresh`, `Parser.parse`); "
            "`python3 -m ctxpress.tools.definition FILE` lists every definition in the file. Use it instead of "
            "`sed -n 'A,Bp'` or `cat` when you need a function or type; it works inside your chained commands. A long "
            "whole-file print (cat FILE) is shown to you as the file's outline; running the same command again prints "
            "the full file.")


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
        f"and its outline. Read a definition with: python3 -m ctxpress.tools.definition {path} NAME. Running the "
        f"same command again prints all {len(body)} lines.]")])
