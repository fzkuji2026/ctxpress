"""Unit tests of the engine on small synthetic traces (no data needed)."""
from ctxpress.core import engine
from ctxpress.methods.base import Method
from ctxpress.core import textops
from ctxpress.core.params import DEFAULT, anthropic
import ctxpress.methods as M
from ctxpress.methods import cost_model


def seg_out(kind, res, text, **kw):
    return dict(seg="out", size=len(text) // 4 + 8, kind=kind, res=res, outpaths=kw.pop("outpaths", []), text=text, **kw)


def seg_call(kind, res, text="x", **kw):
    return dict(seg="call", size=len(text) // 4 + 8, kind=kind, res=res, text=text, **kw)


CODE = "\n".join(["package main", "import \"fmt\""] + [f"func f{i}() int {{\n    return {i} + compute_value_{i}()\n}}" for i in range(400)])


def synthetic(n_filler=30, t_step=10.0):
    """read a spec and a code file, do unrelated work, then edit the code file (anchors near the end)."""
    reqs = [dict(input=1000, cached=0, t=0.0, before=[dict(seg="msg", size=50, role="user"),
                                                         seg_call("spec", ["docs/SPEC.md"]), seg_out("spec", ["docs/SPEC.md"], "requirement " * 400)])]
    reqs.append(dict(input=0, cached=0, t=t_step, before=[seg_call("read", ["a/main.go"]), seg_out("read", ["a/main.go"], CODE, sub="code")]))
    for i in range(n_filler):
        reqs.append(dict(input=0, cached=0, t=t_step * (i + 2), before=[seg_call("command", []), seg_out("command", [], "ok\n" * 200)]))
    edit = seg_call("edit", ["a/main.go"], "patch", anchors={"a/main.go": ["return 399 + compute_value_399()"]})
    reqs.append(dict(input=0, cached=0, t=t_step * (n_filler + 2), before=[edit]))
    reqs.append(dict(input=0, cached=0, t=t_step * (n_filler + 3), before=[seg_out("edit", ["a/main.go"], "Done")]))
    return dict(name="syn", reqs=reqs, prefix=500, alpha=1.0)


def test_no_compaction_has_no_misses():
    r = engine.run(synthetic(), M.NoCompaction())
    assert r.get("silent", 0) == 0 and r.get("spec_gap", 0) == 0


def test_placeholder_causes_recovery_and_misses():
    r = engine.run(synthetic(), M.ComplexityTrap(3))
    assert r["recover"] > 0 and r["silent"] > 0 and r["spec_gap"] > 0


def test_truncation_keeps_tail_anchor():
    class TruncAll(Method):
        def step(self, sim, rr):
            for s in sim.outputs():
                if s.get("form", "full") == "full" and s["kind"] == "read":
                    sim.truncate(s, 300)
    r = engine.run(synthetic(), TruncAll())
    assert r.get("covered", 0) == 1 and r.get("recover", 0) == 0     # head/tail keeps the last function


def test_structure_drops_bodies():
    class StructAll(Method):
        def step(self, sim, rr):
            for s in sim.outputs():
                if s.get("form", "full") == "full" and s["kind"] == "read":
                    sim.structure(s)
    r = engine.run(synthetic(), StructAll())
    assert r.get("covered", 0) == 0 and r["recover"] > 0               # the body line is gone -> re-read


def test_memory_label_changes_q():
    r_id = engine.run(synthetic(), M.ARC(3))
    r_lab = engine.run(synthetic(), M.ARC(3, label=True))
    assert r_lab["spec_gap"] < r_id["spec_gap"]                        # assumed q: label 0.5 vs id 1/8
    assert r_id.get("retrieved", 0) > 0


def test_ttl_expiry_costs_more():
    tr = synthetic(t_step=600.0)                                       # 10 minutes between requests
    cheap = engine.run(tr, M.NoCompaction(), DEFAULT)
    expired = engine.run(tr, M.NoCompaction(), anthropic(300))
    assert expired["uncached"] > cheap["uncached"]


def test_recompression_retention():
    tr = synthetic()
    class TwoSummaries(Method):
        def step(self, sim, rr):
            if rr in (2, 20):
                sim.summarize()
    a = engine.run(tr, TwoSummaries(), DEFAULT)
    b = engine.run(tr, TwoSummaries(), DEFAULT.override(recompress_retention=0.5))
    assert b["silent"] > a["silent"]


def test_segment_summary_in_place():
    tr = synthetic()
    for i in range(2, 30, 5):                                           # mark some test runs to create segments
        tr["reqs"][i]["before"][1]["test"] = True
        tr["reqs"][i]["before"].insert(0, seg_call("edit", ["b/x.go"], "p"))
    r = engine.run(tr, M.AgentFold(keep_segments=1))
    assert r.get("segsummaries", 0) > 0


def test_textops():
    assert len(textops.head_tail("a\n" * 1000, 100)) < 200
    s = textops.structured(CODE, "read_code")
    assert "func f10() int {" in s and "return 10" not in s


def test_cost_model_runs_with_all_switches():
    tr = synthetic()
    ops = {k: ["placeholder", "truncate", "structure"] for k in cost_model.ALL_TYPES}
    m = cost_model.CostModel([synthetic(), synthetic(40)], lam=1e5, ops=ops, segments=True, memory="label", hint=True, lookahead=8)
    r = engine.run(tr, m)
    assert r["cost"] > 0
