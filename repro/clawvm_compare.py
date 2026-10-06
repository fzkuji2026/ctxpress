"""ClawVM: our port of the selection core vs the authors' artifact.

1. Runs the authors' Tier-2 sweep (4 workloads x 6 policies x 6 budgets) unchanged.
2. Runs it again with their `_select_representations` and `_touch_recency` replaced by ctxpress.methods.clawvm.
Every summary row must be identical; the unchanged run must equal the reference results they committed
(replay_py/examples/prelim_suite/tier2/results/tier2.summary.csv).

    python repro/clawvm_compare.py [--orig data/repro/clawvm]
"""
from __future__ import annotations
import argparse, json, os, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # this repository, whatever its folder name
DATA = os.environ.get("CTXPRESS_DATA", os.path.join(ROOT, "data"))   # originals, recorded sessions
sys.path.insert(0, REPO)


def sweep(t2):
    workloads = t2.generate_tier2_workloads()
    rows = []
    for wid in sorted(workloads):
        for pol in ["retrieval_only", "retrieval_only_cached", "compaction_hybrid", "clawvm", "lru", "oracle_h3"]:
            for b in t2.DEFAULT_BUDGETS:
                m, _ = t2.simulate_workload(workloads[wid], pol, b)
                rows.append(m)
    return t2.summarize_with_oracle_gap(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orig", default=os.path.join(DATA, "repro", "clawvm"))
    ap.add_argument("--out", default=os.path.join(REPO, "runs", "repro", "clawvm.json"))
    a = ap.parse_args()
    sys.path.insert(0, os.path.join(a.orig, "replay_py"))
    from clawvm_replay import tier2 as t2
    from ctxpress.methods import clawvm as ours

    theirs = sweep(t2)

    def select(session, turn, policy_cfg, budget, recency_map, turn_index):
        h = int(policy_cfg.get("horizon", 0))
        return ours.select(session.get("pages", []), turn.get("demands", []), policy_cfg, budget, recency_map,
                           future=lambda pid: t2._demand_future_count(session, turn_index, pid, h))
    t2._select_representations, t2._touch_recency = select, ours.touch_recency
    mine = sweep(t2)

    key = lambda r: json.dumps(r, sort_keys=True, default=str)
    same = sum(key(x) == key(y) for x, y in zip(theirs, mine))
    ref = os.path.join(a.orig, "replay_py", "examples", "prelim_suite", "tier2", "results", "tier2.summary.json")
    ref_rows = json.load(open(ref, encoding="utf-8")) if os.path.exists(ref) else None
    ref_rows = ref_rows.get("rows", ref_rows) if isinstance(ref_rows, dict) else ref_rows
    res = dict(rows=len(theirs), identical_rows=same,
               clawvm_vs_baselines={r["policy"]: sum(x["total_faults"] for x in theirs if x["policy"] == r["policy"]) for r in theirs})
    if ref_rows:
        cols = ["workload_id", "policy", "budget", "total_faults", "thrash_index"]
        pick = lambda rs: sorted(tuple(str(r.get(c)) for c in cols) for r in rs)
        res["matches_committed_reference"] = pick(ref_rows) == pick(theirs)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(res, open(a.out, "w"), indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__" and "--runtime" not in sys.argv:
    main()


def runtime_check(orig, budgets=(120, 180, 240, 300, 380, 500)):
    """Second path: the runtime engine (clawvm_runtime, used for the paper's Table 4) with its selector swapped."""
    sys.path.insert(0, orig); sys.path.insert(0, os.path.join(orig, "workloads")); sys.path.insert(0, os.path.join(orig, "replay_py"))
    import clawvm_runtime.engine as eng
    from clawvm_runtime.selector import SelectionResult
    import generate_runtime_traces as g
    from clawvm_replay.replay import compute_metrics
    from ctxpress.methods import clawvm as ours
    orig_select = eng.select_representations

    def run_all():
        out = []
        for sc in sorted(g.SCENARIOS):
            for b in budgets:
                for pol in g.POLICIES:
                    r = g.run_scenario(g.SCENARIOS[sc](b), g.POLICIES[pol])
                    m = compute_metrics(r["trace_events"])
                    out.append((sc, b, pol, json.dumps(m, sort_keys=True, default=str)))
        return out

    def swapped(page_table, demands, policy, budget, future_demand_fn=None, turn_index=0):
        pages = [dict(e.page.to_dict(), page_id=pid) for pid, e in page_table.entries.items()]
        cfg = policy.to_dict()
        sel, used, unmet = ours.select(pages, [dict(page_id=d.page_id, required_repr=d.required_repr) for d in demands], cfg, budget,
                                       page_table.recency_map(),
                                       future=(lambda pid: future_demand_fn(pid, turn_index, policy.horizon)) if future_demand_fn else None)
        return SelectionResult(selected=sel, tokens_used=used, budget=budget, unmet_required=unmet)

    theirs = run_all()
    eng.select_representations = swapped
    mine = run_all()
    eng.select_representations = orig_select
    return dict(rows=len(theirs), identical_rows=sum(a == b for a, b in zip(theirs, mine)))


if __name__ == "__main__" and "--runtime" in sys.argv:
    print(json.dumps(runtime_check(os.path.join(DATA, "repro", "clawvm"))))
