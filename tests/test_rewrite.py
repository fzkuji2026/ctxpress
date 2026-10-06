"""The request rewriter on the real request sequence of a recorded Codex session (every request carries the full
history; we rebuild it from the rollout's response items). Skipped when the session is not on this machine."""
import copy, glob, json, os, tempfile
import pytest
from ctxpress.replay.loaders.codex import requests_from_rollout
from ctxpress.live.rewrite import Rewriter, OUT_TYPES, CALL_TYPES, output_text
import ctxpress.methods as M

DATA = os.environ.get("CTXPRESS_DATA", os.path.join(os.path.dirname(__file__), "..", "..", "data"))
ROLL = glob.glob(os.path.join(DATA, "tb4-jobs",
                              "milestone-official-002-evidence", "sessions", "2026", "09", "22", "rollout-2026-09-22T05-22-24-*.jsonl"))


def requests(path, limit=60):
    """The `input` of each model request of a recorded session."""
    return requests_from_rollout(path, limit)[1]


pytestmark = pytest.mark.skipif(not ROLL, reason="recorded session not available")


def run(method, reqs, **kw):
    rw = Rewriter(lambda: method, **kw)
    res = []
    for inp in reqs:
        body, info = rw.rewrite_body({"input": copy.deepcopy(inp), "model": "m"}, "s")
        res.append((inp, body["input"], info))
    return res


def test_no_compaction_is_identity():
    reqs = requests(ROLL[0], 30)
    for inp, out, info in run(M.NoCompaction(), reqs):
        assert out == inp


def test_complexity_trap_matches_codex_masking():
    reqs = requests(ROLL[0], 60)
    for inp, out, info in run(M.ComplexityTrap(5), reqs):
        assert len(out) == len(inp)                       # nothing removed
        paired = [i for i, x in enumerate(inp) if x.get("type") in OUT_TYPES]
        changed = [i for i, (a, b) in enumerate(zip(inp, out)) if a != b]
        assert changed == paired[:max(0, len(paired) - 5)]   # every output but the newest 5, as the Rust masking
        for i in changed:
            assert out[i]["type"] == inp[i]["type"] and out[i]["call_id"] == inp[i]["call_id"]
            assert output_text(out[i]).startswith("Old environment output: (")


def test_pairs_kept_after_sliding_window():
    reqs = requests(ROLL[0], 60)
    for inp, out, info in run(M.SlidingWindow(20000), reqs):
        calls = {x["call_id"] for x in out if x.get("type") in CALL_TYPES}
        outs = {x["call_id"] for x in out if x.get("type") in OUT_TYPES}
        assert calls == outs
        assert out[0] == inp[0]                           # instructions stay


def test_memory_store_and_placeholder_path():
    reqs = requests(ROLL[0], 40)
    with tempfile.TemporaryDirectory() as d:
        res = run(M.ARC(5), reqs, store_dir=d, store_prefix="/ctx_store")
        inp, out, info = res[-1]
        changed = [(a, b) for a, b in zip(inp, out) if a != b]
        assert changed
        a, b = changed[0]
        txt = output_text(b)
        assert "/ctx_store/" in txt
        fid = txt.split("/ctx_store/")[1].split(".txt")[0]
        assert open(os.path.join(d, f"{fid}.txt"), encoding="utf-8").read() == output_text(a)


def test_pichay_faults_on_reread():
    reqs = requests(ROLL[0], 60)
    m = M.PichayApprox(3, memory=None)
    res = run(m, reqs)
    assert all(len(o) == len(i) for i, o, _ in res)
