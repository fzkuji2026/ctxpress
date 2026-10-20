"""infinite-context V5 switch ablations on recorded sessions: what each switch does to the requests Codex would send.

Replay only (simulation): the recorded agent's actions are fixed, so this measures context size and cache breaks,
never task quality. Starting from the Cliff settings, one switch changes per arm. Model writers (llm, hybrid) need a
summary service and are left out here.

    python repro/infinite_v5_ablation.py [--threshold 100000] [--limit N]
"""
from __future__ import annotations
import argparse, copy, glob, hashlib, json, os, sys, tempfile
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("CTXPRESS_DATA", os.path.join(ROOT, "data"))
sys.path.insert(0, REPO)
from ctxpress.replay.loaders.codex import requests_from_rollout as requests  # noqa: E402
from ctxpress.core.hostsize import billable_chars  # noqa: E402

CACHED_PRICE = 0.1                     # a cached input token costs a tenth of an uncached one
ARMS = {
    "codex": None,                    # standard Codex: no request rewritten (HostDefault); its own compaction as recorded
    "cliff": {},
    "dialect_full": dict(tool_dialect="full"),
    "recall": dict(recall=True),
    "full_recall": dict(tool_dialect="full", recall=True),
    "protect_user": dict(protect_user=True),
    "protect_spec": dict(protect_spec=True),
    "previous_keep": dict(previous="keep"),
    "keep_recent_1": dict(keep_recent=1),
    "keep_recent_6": dict(keep_recent=6),
    "output_keep_2000": dict(output_keep_chars=2000),
    "cost": dict(decider="cost"),
    "cost_lp0.05": dict(decider="cost", length_penalty=0.05),
    "L1_entry": dict(entry=True),
    "L2_segment": dict(segment=True),
    "L3_topic": dict(topic=True),
    "adaptive": dict(adaptive=True),
    "min_growth_0.2": dict(min_growth=0.2),
    "layers": dict(tool_dialect="full", entry=True, segment=True, topic=True),
    "L2_seg0.15": dict(segment=True, segment_min_frac=0.15),
    "L2_seg0.3": dict(segment=True, segment_min_frac=0.3),
    "L3_user_only": dict(topic=True, topic_shift=False),
    "L3_gap24": dict(topic=True, topic_gap=24),
    "L1_L2_0.3": dict(entry=True, segment=True, segment_min_frac=0.3),
    "L1_L2_0.3_full": dict(tool_dialect="full", entry=True, segment=True, segment_min_frac=0.3),
    "layers_adaptive": dict(tool_dialect="full", entry=True, segment=True, topic=True, adaptive=True, min_growth=0.2),
    # the three layers under the hard constraints the cost chooser always has (fair fixed-switch comparison)
    "layers_guard": dict(tool_dialect="full", entry=True, segment=True, topic=True, guard=True, protect_spec=True,
                         recall=True),
    "guard_only": dict(tool_dialect="full", guard=True, protect_spec=True, recall=True),
    # §9.7: every operator a candidate at every request, the cost model (with the provider cache) picks
    "choose_cost": dict(tool_dialect="full", choose="cost"),
    "choose_cost_topic": dict(tool_dialect="full", choose="cost", summary_at="topic"),
    "choose_ph_only": dict(tool_dialect="full", choose="cost", candidates=("placeholder",)),
    "choose_sum_only": dict(tool_dialect="full", choose="cost", candidates=("summary",)),
    "choose_cost_lp0.02": dict(tool_dialect="full", choose="cost", length_penalty=0.02),
    "choose_cost_lp0.05": dict(tool_dialect="full", choose="cost", length_penalty=0.05),
}
COUNTERS = ("v5_entry", "v5_segment", "v5_topic", "v5_refetch", "v5_choose_placeholder", "v5_choose_summary_mechanical",
            "v5_choose_forced", "v5_choose_nothing")


def measure(path, threshold, switches):
    from ctxpress.live.contract import HostContractError
    from ctxpress.live.rewrite import Rewriter
    from ctxpress.methods import HostDefault, InfiniteContext
    from ctxpress.core.cache_model import Cache
    instr, reqs = requests(path)
    rw = Rewriter(HostDefault if switches is None else lambda: InfiniteContext(version=5, t=threshold, **switches))
    sent = breaks = refused = 0
    cost = observed = 0.0
    prev = None
    cache = Cache(fixed=len(instr or "") // 4)   # the provider as measured (core/cache_model.py)
    for n, inp in enumerate(reqs):
        body = {"model": "gpt-5", "instructions": instr, "input": inp, "tools": [], "stream": True}
        try:
            out, _ = rw.rewrite_body(copy.deepcopy(body), "s")
        except HostContractError:          # live, the proxy answers 422 and the run stops here
            refused, reqs = 1, reqs[:n]
            break
        cur = [json.dumps(x, sort_keys=True) for x in out["input"]]
        chars = [billable_chars(x) for x in out["input"]]   # images by dimensions, not base64 length
        size = sum(chars) // 4
        sent += size
        j = 0                                # items served from the cache: the prefix shared with the last request
        if prev is not None:
            while j < min(len(prev), len(cur)) and prev[j] == cur[j]:
                j += 1
            if j < len(prev):
                breaks += 1
        cached = sum(chars[:j]) // 4
        cost += CACHED_PRICE * cached + (size - cached)
        sizes = [c // 4 for c in chars]
        hit = cache.expect(cur, sizes)
        observed += CACHED_PRICE * hit + (cache.fixed + sum(sizes) - hit)
        cache.record(cur, sizes)
        prev = cur
    raw = sum(billable_chars(x) for inp in reqs for x in inp) // 4
    M = next(iter(rw.sessions.values())).ctx.M if rw.sessions else {}
    layers = {k: int(M.get(k, 0)) for k in COUNTERS}
    return dict(requests=len(reqs), sent_tokens=sent, raw_tokens=raw, cache_breaks=breaks, refused=refused,
                cost=round(cost), cost_observed=round(observed), **layers)


def job(arg):
    path, threshold, arm = arg
    return arm, path, measure(path, threshold, ARMS[arm])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=int, default=100_000)
    ap.add_argument("--sessions", default=os.path.join(DATA, "tb4-jobs", "**", "rollout-*.jsonl"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--arms", nargs="+", default=list(ARMS))
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--out", default=os.path.join(REPO, "runs", "repro", "infinite_v5_ablation.jsonl"))
    a = ap.parse_args()
    os.environ.setdefault("CTXPRESS_STORE_DIR", tempfile.mkdtemp(prefix="v5-ablation-store-"))
    paths, seen = [], set()
    for p in sorted(glob.glob(a.sessions, recursive=True)):  # the job folders hold copies of the same sessions
        digest = hashlib.sha256(open(p, "rb").read()).hexdigest()
        if digest not in seen:
            seen.add(digest)
            paths.append(p)
    paths = paths[: a.limit or None]
    print(len(paths), "distinct sessions", flush=True)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    totals = {arm: dict(sessions=0, requests=0, sent_tokens=0, raw_tokens=0, cache_breaks=0, refused=0, cost=0,
                        cost_observed=0, **dict.fromkeys(COUNTERS, 0)) for arm in a.arms}
    jobs = [(p, a.threshold, arm) for arm in a.arms for p in paths]
    with open(a.out, "w", encoding="utf-8") as fh, ProcessPoolExecutor(a.workers) as pool:
        for n, (arm, p, r) in enumerate(pool.map(job, jobs, chunksize=1), 1):
            fh.write(json.dumps(dict(arm=arm, threshold=a.threshold, session=os.path.relpath(p, ROOT), **r)) + "\n")
            fh.flush()
            totals[arm]["sessions"] += 1
            for k in r:
                totals[arm][k] += r[k]
            if n % 50 == 0:
                print(n, "/", len(jobs), flush=True)
    for arm, tot in totals.items():
        print(arm, json.dumps(tot), flush=True)
    base = totals.get("cliff")
    if base:
        print("\ncost: lcp = cache hits the prefix unchanged since the last request; observed = the provider as measured"
              " (core/cache_model.py)")
        print("arm                  sent/raw  sent vs cliff  cost(lcp)  cost(obs)  breaks  refused    L1    L2    L3"
              "  forced nothing")
        for arm, t in totals.items():
            print(f"{arm:<20} {t['sent_tokens'] / t['raw_tokens']:8.3f}  {t['sent_tokens'] / base['sent_tokens']:12.3f}"
                  f"  {t['cost'] / base['cost']:9.3f}  {t['cost_observed'] / base['cost_observed']:9.3f}"
                  f"  {t['cache_breaks']:6d}  {t['refused']:7d}  {t['v5_entry']:4d} {t['v5_segment']:5d} {t['v5_topic']:5d}"
                  f"  {t['v5_choose_forced']:6d} {t['v5_choose_nothing']:7d}")


if __name__ == "__main__":
    main()
