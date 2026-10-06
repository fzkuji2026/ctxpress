"""The package must reproduce the §8 simulator (data/tb4-jobs/cost-model-20261002/sim2.py) exactly for the
configurations used in experiment.zh.html. Skipped when the old scripts or the traces are not on this machine."""
import os, sys, pytest

DATA = os.environ.get("CTXPRESS_DATA", os.path.join(os.path.dirname(__file__), "..", "..", "data"))
OLD = os.path.join(DATA, "tb4-jobs", "cost-model-20261002")
if not os.path.exists(os.path.join(OLD, "sim2.py")):
    pytest.skip("old simulator not available", allow_module_level=True)
sys.path.insert(0, OLD)
import sim as old_sim, sim2 as old_sim2            # noqa: E402
from ctxpress.replay import corpus
from ctxpress.core import engine
from ctxpress.core import metrics       # noqa: E402
import ctxpress.methods as M
from ctxpress.methods import cost_model  # noqa: E402
from ctxpress.core.params import DEFAULT as _D           # noqa: E402
DEFAULT = _D.override(spec_recovery=False, recompress_retention=1.0, dedupe_recovery=False)   # the §8 accounting

KEYS = ["cost", "input", "uncached", "requests", "silent", "spec_gap", "recover", "reexplore", "forced", "summaries"]


@pytest.fixture(scope="module")
def traces(tmp_path_factory):
    import yaml
    # Use the chosen data tree even when the checked-in manifest names the
    # original Windows checkout. Leave the user's manifest unchanged.
    with open(corpus.DEFAULT_MANIFEST, encoding="utf-8") as source:
        manifest = yaml.safe_load(source)
    manifest["root"] = os.path.abspath(os.path.join(DATA, "tb4-jobs"))
    path = tmp_path_factory.mktemp("repro-manifest") / "sessions.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    try:
        return corpus.load_manifest(path)
    except FileNotFoundError:
        pytest.skip("traces not available")


def close(a, b):
    for k in KEYS:
        x, y = a.get(k, 0), b.get(k, 0)
        assert abs(x - y) <= 1e-6 * max(1.0, abs(y)), (k, x, y)


PAIRS = [
    (lambda: M.CodexAutoCompact(230000), lambda: old_sim.Summarize(230000)),
    (lambda: M.CodexAutoCompact(64000), lambda: old_sim.Summarize(64000)),
    (lambda: M.ClaudeCode(64000), lambda: old_sim.ClearThenSummarize(64000)),
    (lambda: M.ClearThenSummarize(64000), lambda: old_sim.ClearThenSummarize(64000, all_outputs=True)),
    (lambda: M.SlidingWindow(64000), lambda: old_sim.SlidingWindow(64000)),
    (lambda: M.ComplexityTrap(5), lambda: old_sim.KeepLastN(5)),
    (lambda: M.KeepLastTokens(16000), lambda: old_sim.KeepLastTokens(16000)),
    (lambda: M.PichayApprox(5, memory=None), lambda: old_sim.IdlePaging(5)),
    (lambda: M.ClawVMApprox(5, simplified=True), lambda: old_sim.KeepLastN(5, pin_spec=True)),
]


@pytest.mark.parametrize("i", range(len(PAIRS)))
def test_baselines(traces, i):
    new, old = PAIRS[i]
    for tr in traces:
        a = engine.run(tr, new(), DEFAULT)
        b = old_sim2.run(tr, old(), alpha=tr["alpha"])
        close(a, b)


def test_no_compaction(traces):
    for tr in traces:
        close(engine.run(tr, M.NoCompaction(), DEFAULT), old_sim2.run(tr, old_sim.NoCompaction(), alpha=tr["alpha"], window=None))


@pytest.mark.parametrize("kw", [dict(lam=1e6), dict(lam=5e6, lookahead=8, harm=metrics.HARM["harm"]),
                                dict(lam=2e7, lookahead=8, harm=metrics.HARM["measured"])])
def test_cost_model(traces, kw):
    for tr in traces[:3]:
        rest = [u for u in traces if u is not tr]
        a = engine.run(tr, cost_model.CostModel(rest, **kw), DEFAULT)
        b = old_sim2.run(tr, old_sim2.FullCostModel(rest, **kw), alpha=tr["alpha"])
        close(a, b)
