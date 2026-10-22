"""Text-level compression of a single tool output. Pure functions on strings, so that a real harness can use
exactly what the simulator measured (ctxpress.live.context)."""
from __future__ import annotations
import re
from ctxpress.core import tokens

SIG = re.compile(r"^\s*(?:\d+\s+)?(?:func|type|class|def|interface|struct|enum|impl|trait|fn|pub\s|export\s|package|import|const|var|@|#include|module)\b")
FAIL = re.compile(r"(FAIL|Error|ERROR|error:|panic|Traceback|AssertionError|undefined|cannot|expected|--- FAIL|✗|failed)", re.I)
PATHLINE = re.compile(r"^[\w./-]+\.\w+(?::\d+)?")


def structured(text, ctype, tail_lines=20, keep_also=()):
    """Keep the structure of an output.
    read_code  definition lines (functions, types, imports)
    run        lines that report failures, plus the last `tail_lines` lines
    search     one entry per matching file (path:line only)
    other      head and tail lines, a quarter of its tokens"""
    lines = text.split("\n")
    if ctype == "read_code":
        keep = [l for l in lines if SIG.match(l) or any(a in l for a in keep_also)]
        return "\n".join(keep + [f"[structure only: {len(lines) - len(keep)} body lines removed]"])
    if ctype == "run":
        idx = sorted({i for i, l in enumerate(lines) if FAIL.search(l)} | set(range(max(0, len(lines) - tail_lines), len(lines))))
        keep = [lines[i] for i in idx]
        return "\n".join([f"[failures and last {tail_lines} lines of {len(lines)}]"] + keep)
    if ctype == "search":
        seen, keep = set(), []
        for l in lines:
            m = PATHLINE.match(l)
            p = m.group(0).split(":")[0] if m else None
            if p and p not in seen:
                seen.add(p); keep.append(p)
            elif not m and len(keep) < 5 and l.strip():
                keep.append(l)
        return "\n".join(keep)
    return tokens.head_tail(text, max(1, round(0.25 * tokens.count(text))))
