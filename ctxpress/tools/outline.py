"""`python3 -m ctxpress.tools.outline FILE [FILE ...]`: the definitions of source files with their exact line spans.

A read tool for agents: instead of printing a long file to find where something is, print its outline and then only
the span needed (sed -n 'A,Bp' FILE). Language-agnostic line rules (Go, Python, TypeScript/JavaScript, Rust, C/C++,
Java/Kotlin); a definition's span ends before the next definition at the same or a smaller indentation.
"""
from __future__ import annotations
import re, sys

_DEF = re.compile(
    r"^(?P<ind>\s*)(?:"
    r"(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?(?:function\*?|class|interface|enum|type|namespace|module)\s+[\w$]+"
    r"|(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:fn|struct|enum|trait|impl|mod|type|union)\b"
    r"|func\s+(?:\([^)]*\)\s*)?\w+|type\s+\w+\s+(?:struct|interface|func|\w)"
    r"|(?:async\s+)?def\s+\w+|class\s+\w+"
    r"|(?:template\s*<.*>\s*)?(?:class|struct|union|enum(?:\s+class)?|namespace)\s+[\w:]+\s*(?:final\s*)?(?::[^;{]*)?\{?\s*$"
    r"|(?:(?:public|private|protected|internal|static|final|abstract|override|readonly|async|get|set|virtual|inline|constexpr|explicit|friend|open|suspend)\s+)*"
    r"(?:[\w:<>,\[\]*&\s]+\s+)?[~\w$:]+(?:<[^>()]*>)?\s*\([^;]*\)\s*(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?(?::\s*[^={;]+)?(?:->\s*[^{;]+)?\s*\{\s*$"
    r"|(?:export\s+)?(?:const|let)\s+[\w$]+\s*=\s*(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*=>"
    r")")
_SKIP = re.compile(r"^\s*(?:if|for|while|switch|catch|else|return|do|try|with|elif|except|match)\b")
MAX_ENTRIES = 150


def outline(lines):
    """[(start, end, indent, text)] with 1-based inclusive line numbers."""
    defs = []
    for i, line in enumerate(lines):
        if len(line) > 300 or _SKIP.match(line) or line.rstrip().endswith(","):
            continue
        m = _DEF.match(line)
        if m:
            defs.append([i + 1, len(lines), len(m.group("ind").expandtabs(4)), line.strip()])
    for k, d in enumerate(defs):
        for e in defs[k + 1:]:
            if e[2] <= d[2]:
                d[1] = e[0] - 1
                break
        while d[1] > d[0] and not lines[d[1] - 1].strip():
            d[1] -= 1
    return [tuple(d) for d in defs]


def render(path, lines, max_entries=MAX_ENTRIES):
    defs = outline(lines)
    singles = 0
    if len(defs) > max_entries:                    # one-line definitions (aliases, fields) go first, counted
        singles = sum(1 for d in defs if d[0] == d[1])
        defs = [d for d in defs if d[0] != d[1]]
    if len(defs) > max_entries:                    # then keep the outer levels
        levels = sorted({d[2] for d in defs})
        while len(levels) > 1 and sum(1 for d in defs if d[2] <= levels[-1]) > max_entries:
            levels.pop()
        hidden = len(defs)
        defs = [d for d in defs if d[2] <= levels[-1]][:max_entries]
        hidden -= len(defs)
    else:
        hidden = 0
    base = min((d[2] for d in defs), default=0)
    out = [f"{path} ({len(lines)} lines)"]
    out += [f"{s:>6}-{e:<6}{' ' * (d - base)}{t[:160]}" for s, e, d, t in defs]
    if singles:
        out.append(f"   ({singles} one-line definitions not listed)")
    if hidden:
        out.append(f"   ({hidden} nested definitions not listed)")
    return "\n".join(out)


def main(argv=None):
    paths = (argv if argv is not None else sys.argv[1:])
    if not paths:
        print("usage: python3 -m ctxpress.tools.outline FILE [FILE ...]", file=sys.stderr)
        return 2
    status = 0
    for p in paths:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")
        except OSError as e:
            print(f"{p}: {e.strerror}"); status = 1; continue
        if lines and lines[-1] == "":
            lines.pop()
        print(render(p, lines))
    return status


if __name__ == "__main__":
    sys.exit(main())
