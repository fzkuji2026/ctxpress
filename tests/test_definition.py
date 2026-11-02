"""Definition read tool and the definition-first ReadLess policy."""
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import ReadLess
from ctxpress.tools.definition import definitions, show
from tests.test_outline import GO
from tests.test_readless import code_mode_pair, head, sent

POLICY = "ctxpress.methods.readless_policies.definition"
PY = "\n".join(["import os", "", "class A:", "    @property", "    def b(self):", "        return 1", "",
                "    def c(self):"] + [f"        x = {i}" for i in range(400)] + ["", "def b():", "    return 2"])
TS = "\n".join(["export class T {", "  f(x: number) {", "    if (x) {", "      return '}';", "    }", "  }", "}"])


def test_names_resolve_to_full_definitions():
    lines = GO.split("\n")
    d = definitions("m.go", lines)
    text = show("m.go", lines, "Table.M3", d)
    assert text.startswith("m.go:109-141  Table.M3") and "func (t *Table) M3" in text and "M4" not in text
    lines = PY.split("\n")
    d = definitions("a.py", lines)
    assert ("A.b", 4, 6) in d                          # decorators belong to the definition
    both = show("a.py", lines, "b", d)
    assert "also named b: A.b (4-6)" in both and "    return 2" in both
    assert "more lines: sed -n" in show("a.py", lines, "A.c", d)
    assert "members" in show("a.py", lines, "A", d)
    assert "no definition named zz" in show("a.py", lines, "zz", d)
    lines = TS.split("\n")
    assert ("T.f", 2, 6) in definitions("t.ts", lines)


def test_outline_view_points_to_the_definition_tool():
    m = ReadLess(policy=POLICY, ranged=False)
    assert "ctxpress.tools.definition" in m.instructions
    rw = Rewriter(lambda: ReadLess(policy=POLICY, ranged=False))
    items = head() + code_mode_pair('a', [('cat m.go', GO)])
    out, _ = rw.rewrite_body(dict(input=items), 's')
    text = sent(out, 'a')
    assert "python3 -m ctxpress.tools.definition m.go NAME" in text and "y := x + 17" not in text
