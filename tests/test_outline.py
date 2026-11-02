"""Outline read tool and the outline-first ReadLess policy."""
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import ReadLess
from ctxpress.tools.outline import outline, render
from tests.test_readless import code_mode_pair, head, sent

GO = "\n".join(["package m", "", "type Table struct {", "\trows int", "}", ""] +
               sum(([f"func (t *Table) M{i}(x int) int {{"] + [f"\ty := x + {j}" for j in range(30)] + ["\treturn y", "}", ""]
                    for i in range(8)), []))
POLICY = "ctxpress.methods.readless_policies.outline"


def test_outline_gives_each_definition_its_exact_span():
    lines = GO.split("\n")
    defs = outline(lines)
    starts = {t.split("(")[0]: (s, e) for s, e, _, t in defs}
    s, e = starts["type Table struct {".split("(")[0]]
    assert (s, e) == (3, 5)
    m0 = next((s, e) for s, e, _, t in defs if "M0(" in t)
    assert lines[m0[0] - 1].startswith("func (t *Table) M0") and lines[m0[1] - 1] == "}"
    assert "m.go (" in render("m.go", lines)


def test_whole_file_print_becomes_outline_and_repeat_is_full():
    rw = Rewriter(lambda: ReadLess(policy=POLICY, ranged=False))
    items = head() + code_mode_pair('a', [('cat m.go', GO)])
    out, _ = rw.rewrite_body(dict(input=items), 's')
    text = sent(out, 'a')
    assert "func (t *Table) M5(x int) int {" in text and "y := x + 17" not in text
    assert "sed -n 'A,Bp' m.go" in text and "outline view" in text
    items += code_mode_pair('b', [('cat m.go', GO)])
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert "y := x + 17" in sent(out, 'b')


def test_ranged_and_compound_reads_are_left_alone_and_guidance_is_set():
    m = ReadLess(policy=POLICY, ranged=False)
    assert "ctxpress.tools.outline" in m.instructions
    assert ReadLess().instructions == ""
    rw = Rewriter(lambda: ReadLess(policy=POLICY, ranged=False))
    items = head() + code_mode_pair('a', [("sed -n '1,300p' m.go", GO), ('cat m.go; ls', GO)])
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert "y := x + 17" in sent(out, 'a') and "outline view" not in sent(out, 'a')
