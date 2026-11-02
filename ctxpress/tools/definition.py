"""`python3 -m ctxpress.tools.definition FILE [NAME ...]`: read source code by definition name instead of by line range.

With names (a function, class or method; `Class.method` or `Type.Method` to qualify) it prints each definition's full
text with its line numbers; without names it prints the file's outline (every definition with its line span). Python
files are parsed with ast (decorators included); other languages use the outline's line rules and close a braced
definition at its matching brace. A definition longer than MAX_LINES that has nested definitions is shown as its
first lines and the outline of its members, so that one member can be asked for next.
"""
from __future__ import annotations
import ast, re, sys
from ctxpress.tools.outline import outline, render

MAX_LINES = 300
HEAD = 15
MAX_MATCHES = 3


def _py_defs(text):
    """[(qualified name, start, end)] of a Python file's functions and classes, None if it does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = prefix + child.name
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                out.append((q, start, child.end_lineno))
                walk(child, q + ".")
            elif not isinstance(child, (ast.expr, ast.Lambda)):
                walk(child, prefix)
    walk(tree, "")
    return out


def _brace_end(lines, start, fallback):
    """The line closing the brace opened on or right after `start` (1-based), else `fallback`."""
    depth, opened = 0, False
    for i in range(start - 1, min(len(lines), start + 20000)):
        s = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//.*$', "", lines[i])
        for ch in s:
            if ch == "{":
                depth += 1; opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth == 0:
                    return i + 1
        if not opened and i - start > 4:
            break
    return fallback


def _other_defs(lines):
    """[(qualified name, start, end)] from the outline rules; the name chain is the enclosing definitions' names."""
    defs = outline(lines)
    out, stack = [], []
    for s, e, ind, text in defs:
        while stack and not (stack[-1][1] <= s and e <= stack[-1][2] and stack[-1][3] < ind):
            stack.pop()
        name = _decl_name(text) or text
        recv = re.match(r"func\s*\(\s*\w*\s*\*?\s*([\w.]+)", text)         # Go method receiver type
        chain = [x[0] for x in stack] + ([recv.group(1).split(".")[-1]] if recv else [])
        end = _brace_end(lines, s, e) if "{" in "".join(lines[s - 1:s + 4]) else e
        out.append((".".join(chain + [name]), s, max(end, s)))
        stack.append((name, s, end, ind))
    return out


_KEYWORDS = r"(?:function\*?|class|interface|enum|type|namespace|module|fn|struct|trait|impl|mod|union|def|func)"


def _decl_name(text):
    m = re.search(rf"\b{_KEYWORDS}\s+(?:\([^)]*\)\s*)?([A-Za-z_$][\w$]*)", text)
    if m:
        return m.group(1)
    m = re.search(r"([~A-Za-z_$][\w$]*)\s*(?:<[^>()]*>)?\s*\(", text)       # C-like method: the word before "("
    if m:
        return m.group(1)
    m = re.search(r"(?:const|let)\s+([\w$]+)\s*=", text)
    return m.group(1) if m else None


def definitions(path, lines):
    if path.endswith((".py", ".pyi")):
        got = _py_defs("\n".join(lines))
        if got is not None:
            return got
    return _other_defs(lines)


def find(defs, name):
    """Definitions matching `name`: exact qualified name first, then a matching suffix (`b` or `A.b`)."""
    exact = [d for d in defs if d[0] == name]
    if exact:
        return exact
    return [d for d in defs if d[0].endswith("." + name) or ("." not in name and d[0].split(".")[-1] == name)]


def show(path, lines, name, defs):
    hits = find(defs, name)
    if not hits:
        near = sorted({d[0] for d in defs if name.split(".")[-1].lower() in d[0].lower()})[:10]
        return (f"{path}: no definition named {name}" + (f"; similar: {', '.join(near)}" if near else "")
                + f". `python3 -m ctxpress.tools.definition {path}` lists them all.")
    out = []
    also = [d for d in defs if d not in hits and (d[0].endswith("." + name) or d[0].split(".")[-1] == name)]
    if also:
        out.append(f"(also named {name}: " + ", ".join(f"{q} ({s}-{e})" for q, s, e in also[:10]) + ")")
    if len(hits) > MAX_MATCHES:
        out.append(f"{path}: {len(hits)} definitions match {name}; showing {MAX_MATCHES}. Qualify it (e.g. "
                   f"{hits[0][0]}). All: " + ", ".join(f"{q} ({s}-{e})" for q, s, e in hits))
    for q, s, e in hits[:MAX_MATCHES]:
        n = e - s + 1
        inner = [d for d in defs if d[0].startswith(q + ".") and d[1] > s]
        out.append(f"{path}:{s}-{e}  {q}  ({n} lines)")
        if n > MAX_LINES and inner:
            out += [f"{i:>6}\t{lines[i - 1]}" for i in range(s, min(e, s + HEAD - 1) + 1)]
            out.append(f"  ... members (ask for one by name, e.g. {inner[0][0]}):")
            out += [f"{a:>6}-{b:<6}{d}" for d, a, b in inner if d.count(".") == q.count(".") + 1][:150]
        else:
            stop = min(e, s + MAX_LINES - 1)
            out += [f"{i:>6}\t{lines[i - 1]}" for i in range(s, stop + 1)]
            if stop < e:
                out.append(f"  ... {e - stop} more lines: sed -n '{stop + 1},{e}p' {path}")
    return "\n".join(out)


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if not args:
        print("usage: python3 -m ctxpress.tools.definition FILE [NAME ...]", file=sys.stderr)
        return 2
    path, names = args[0], args[1:]
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except OSError as e:
        print(f"{path}: {e.strerror}"); return 1
    if lines and lines[-1] == "":
        lines.pop()
    if not names:
        print(render(path, lines)); return 0
    defs = definitions(path, lines)
    print("\n\n".join(show(path, lines, n, defs) for n in names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
