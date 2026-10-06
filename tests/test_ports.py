"""Ported methods against the authors' code, on one recorded session / trajectory each. Skipped when the original
code or data is not on this machine (data/repro/, see repro/README.md)."""
import glob, os, sys
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA = os.environ.get("CTXPRESS_DATA", os.path.join(ROOT, "data"))   # originals, recorded sessions
REPRO = os.path.join(DATA, "repro")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "repro")))
SESSION = glob.glob(os.path.join(DATA, "tb4-jobs", "milestone-official-002-evidence", "**", "rollout-*.jsonl"), recursive=True)
need = lambda *p: pytest.mark.skipif(not all(os.path.exists(os.path.join(REPRO, x)) for x in p) or not SESSION, reason="original not available")


@need("cliffcompaction")
def test_cliff_port_identical():
    import cliff_compare
    for t in (20000, 50000):
        r = cliff_compare.compare(SESSION[0], t, os.path.join(REPRO, "cliffcompaction", "src"))
        assert r["same"] == r["requests"] and r["compacted"] > 0


@need("pichay")
def test_pichay_port_identical():
    sys.path.insert(0, os.path.join(REPRO, "pichay", "src"))
    import pichay_compare
    r = pichay_compare.compare(SESSION[0], 4)
    assert r["same"] == r["requests"] and r["theirs"] == r["ours"] and r["theirs"]["evictions"] > 0


@need("clawvm")
def test_clawvm_port_identical():
    import clawvm_compare
    sys.path.insert(0, os.path.join(REPRO, "clawvm", "replay_py"))
    from clawvm_replay import tier2 as t2
    from ctxpress.methods import clawvm as ours
    w = t2.generate_tier2_workloads()["evidence_heavy"]
    for pol in ("clawvm", "oracle_h3", "compaction_hybrid"):
        theirs, _ = t2.simulate_workload(w, pol, 180)
        saved = t2._select_representations, t2._touch_recency
        try:
            t2._select_representations = lambda session, turn, policy_cfg, budget, recency_map, turn_index: ours.select(
                session["pages"], turn["demands"], policy_cfg, budget, recency_map,
                future=lambda pid: t2._demand_future_count(session, turn_index, pid, int(policy_cfg.get("horizon", 0))))
            t2._touch_recency = ours.touch_recency
            mine, _ = t2.simulate_workload(w, pol, 180)
        finally:
            t2._select_representations, t2._touch_recency = saved
        assert mine == theirs


@need("ct-traj")
def test_complexity_trap_on_released_trajectory():
    import complexity_trap_compare as ct
    trajs = sorted(glob.glob(os.path.join(ct.DIR, "*_N_1_M_10*", "*", "*.traj")), key=os.path.getsize)[-2:]
    for p in trajs:
        r = ct.check_traj(p)
        assert r["same"] == r["obs"] and r["masked_ours"] == r["masked_theirs"] > 0
