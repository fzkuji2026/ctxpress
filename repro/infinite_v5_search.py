"""Search over infinite-context V5 settings on recorded sessions, at several thresholds, against CliffCompaction.

Replay only (simulation): the recorded agent's actions are fixed, so this measures what the requests would cost,
never task quality; the chosen settings still need live runs. One set of settings is sampled once and replayed at
every threshold, so a setting is judged by how it does everywhere, not tuned per threshold.

Sessions fall into scenario families (with --manifest, the families it names, e.g. one per SWE-Milestone repository
plus SWE-bench Verified; otherwise E2E long tasks, Terminal-Bench tasks, variable tracking, diagnostic QA). A
setting's score is its worst ratio to Cliff over (family, threshold), so it must not lose anywhere; the leave-one-
family-out check picks on three families and reports the fourth.

    python repro/infinite_v5_search.py [--samples 24] [--thresholds 32000 64000 128000 256000]
"""
from __future__ import annotations
import argparse, glob, hashlib, json, os, random, re, sys, tempfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from infinite_v5_ablation import DATA, REPO, ROOT, measure  # noqa: E402

SPACE = dict(                          # each key is drawn independently; None = operator off
    tool_dialect=["wire", "full"],
    entry_frac=[None, 0.01, 0.02, 0.05],
    segment_min_frac=[None, 0.02, 0.05, 0.1, 0.2],
    topic_min_frac=[None, 0.15, 0.3, 0.5],
    topic_gap=[6, 12, 24],
    topic_shift=[True, False],
    adaptive=[False, True],
    min_growth=[0.0, 0.1, 0.2],
)
ANCHORS = {
    "codex": None,
    "cliff": {},
    "layers": dict(tool_dialect="full", entry=True, segment=True, topic=True),
}


def switches(draw):
    s = dict(tool_dialect=draw["tool_dialect"], adaptive=draw["adaptive"], min_growth=draw["min_growth"])
    if draw["entry_frac"] is not None:
        s.update(entry=True, entry_frac=draw["entry_frac"])
    if draw["segment_min_frac"] is not None:
        s.update(segment=True, segment_min_frac=draw["segment_min_frac"])
    if draw["topic_min_frac"] is not None:
        s.update(topic=True, topic_min_frac=draw["topic_min_frac"], topic_gap=draw["topic_gap"],
                 topic_shift=draw["topic_shift"])
    return s


def sample(n, seed):
    rng, out, seen = random.Random(seed), {}, set()
    while len(out) < n:
        s = switches({k: rng.choice(v) for k, v in SPACE.items()})
        key = json.dumps(s, sort_keys=True)
        if key not in seen and s.keys() - {"tool_dialect", "adaptive", "min_growth"}:
            seen.add(key)
            out[f"s{len(out):02d}"] = s
    return out


def family(path, text):
    if re.search(r"[a-z0-9-]+__[A-Za-z0-9]{7}", path.replace(os.sep, "/")):
        return "terminal-bench"
    for line in text.splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        p = j.get("payload") or {}
        if j.get("type") == "response_item" and p.get("role") == "user":
            t = " ".join(c.get("text", "") for c in p.get("content") or []).lstrip()
            if t.startswith(("<", "# AGENTS")):
                continue
            if t.startswith("# E2E Agent"):
                return "e2e"
            if t.startswith("Track the variable"):
                return "variable-tracking"
            return "diagnostic-qa"
    return "other"


def job(arg):
    path, t, name, sw = arg
    return name, t, path, measure(path, t, sw)


def ratio(rows, base, key="cost"):
    b = sum(base[k][key] for k in rows)
    return sum(rows[k][key] for k in rows) / b if b else 1.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", default=os.path.join(DATA, "tb4-jobs", "**", "rollout-*.jsonl"))
    ap.add_argument("--manifest", help="JSON lines with `local` (path under the repo) and `family`; replaces --sessions")
    ap.add_argument("--thresholds", nargs="+", type=int, default=[32_000, 64_000, 128_000, 256_000])
    ap.add_argument("--samples", type=int, default=24)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--out", default=os.path.join(REPO, "runs", "repro", "infinite_v5_search.jsonl"))
    a = ap.parse_args()
    os.environ.setdefault("CTXPRESS_STORE_DIR", tempfile.mkdtemp(prefix="v5-search-store-"))
    paths, fam, seen = [], {}, set()
    for line in open(a.manifest, encoding="utf-8") if a.manifest else ():
        r = json.loads(line)
        p = os.path.join(REPO, r["local"])
        paths.append(p)
        fam[p] = r["family"]
    for p in [] if a.manifest else sorted(glob.glob(a.sessions, recursive=True)):
        raw = open(p, "rb").read()
        digest = hashlib.sha256(raw).hexdigest()
        if digest not in seen:
            seen.add(digest)
            paths.append(p)
            fam[p] = family(p, raw.decode("utf-8", "ignore"))
    configs = dict(ANCHORS, **sample(a.samples, a.seed))
    print(len(paths), "sessions", {f: list(fam.values()).count(f) for f in sorted(set(fam.values()))}, flush=True)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out.replace(".jsonl", ".configs.json"), "w") as fh:
        json.dump(configs, fh, indent=1)
    res = defaultdict(dict)                       # (name, t) -> path -> row
    jobs = [(p, t, n, sw) for t in a.thresholds for n, sw in configs.items() for p in paths]
    with open(a.out, "w", encoding="utf-8") as fh, ProcessPoolExecutor(a.workers) as pool:
        for k, (n, t, p, r) in enumerate(pool.map(job, jobs, chunksize=2), 1):
            res[n, t][p] = r
            fh.write(json.dumps(dict(config=n, threshold=t, family=fam[p], session=os.path.relpath(p, ROOT), **r)) + "\n")
            fh.flush()
            if k % 200 == 0:
                print(k, "/", len(jobs), flush=True)
    fams = sorted(set(fam.values()))
    summary = {}
    for n in configs:
        cell = {}
        for t in a.thresholds:
            cell[str(t)] = {"all": ratio(res[n, t], res["cliff", t])}
            for f in fams:
                sub = {p: r for p, r in res[n, t].items() if fam[p] == f}
                cell[str(t)][f] = ratio(sub, {p: res["cliff", t][p] for p in sub})
        worst = max(v for c in cell.values() for f, v in c.items() if f != "all")
        summary[n] = dict(worst=worst, cells=cell)
    loo = {}
    for held in fams:                              # pick on three families, report the fourth
        def worst_without(n):
            return max(v for c in summary[n]["cells"].values() for f, v in c.items() if f not in ("all", held))
        pick = min((n for n in configs if n != "codex"), key=worst_without)
        loo[held] = dict(pick=pick, tuned_worst=worst_without(pick),
                         held_out={t: summary[pick]["cells"][str(t)][held] for t in a.thresholds})
    best = min((n for n in configs if n != "codex"), key=lambda n: summary[n]["worst"])
    with open(a.out.replace(".jsonl", ".summary.json"), "w") as fh:
        json.dump(dict(best=best, configs=configs, summary=summary, leave_one_family_out=loo), fh, indent=1)
    print("\nconfig  worst  " + "  ".join(f"{t // 1000}k" for t in a.thresholds))
    for n in sorted(configs, key=lambda n: summary[n]["worst"]):
        print(f"{n:<7} {summary[n]['worst']:6.3f}  " + "  ".join(
            f"{summary[n]['cells'][str(t)]['all']:.3f}" for t in a.thresholds), configs[n])
    print("best", best, configs[best])
    print("leave one family out", json.dumps(loo))


if __name__ == "__main__":
    main()
