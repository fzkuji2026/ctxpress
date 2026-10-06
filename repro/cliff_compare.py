"""CliffCompaction: our port vs the authors' package, request by request.

Every recorded Codex session (rollout files) is turned back into the sequence of requests Codex sent (each
carries the full history). Each request goes through the original `cliffcompaction.engine.Engine` (Responses
dialect) and through ctxpress's Rewriter with CliffCompaction; the outgoing `input` lists must be identical.

    python repro/cliff_compare.py [--orig data/repro/cliffcompaction/src] [--thresholds 20000 50000 100000]
"""
from __future__ import annotations
import argparse, copy, glob, json, os, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # this repository, whatever its folder name
DATA = os.environ.get("CTXPRESS_DATA", os.path.join(ROOT, "data"))   # originals, recorded sessions
sys.path.insert(0, REPO)
from ctxpress.replay.loaders.codex import requests_from_rollout as requests  # noqa: E402


def compare(path, threshold, orig_src):
    sys.path.insert(0, orig_src)
    from cliffcompaction.config import Config
    from cliffcompaction.engine import Engine
    from cliffcompaction.dialects.openai_responses import DIALECT
    from ctxpress.live.rewrite import Rewriter
    from ctxpress.methods import CliffCompaction

    instr, reqs = requests(path)
    eng = Engine(Config(threshold_tokens=threshold))
    rw = Rewriter(lambda: CliffCompaction(t=threshold))
    res = dict(requests=len(reqs), same=0, compacted=0, substituted=0, max_in=0, first_diff=None)
    for i, inp in enumerate(reqs):
        body = {"model": "gpt-5", "instructions": instr, "input": inp, "tools": [], "stream": True}
        ctx = eng.prepare(copy.deepcopy(body), DIALECT)
        theirs = ctx.outgoing_body()["input"]
        ours, info = rw.rewrite_body(copy.deepcopy(body), "s")
        ours = ours["input"]
        res["max_in"] = max(res["max_in"], ctx.est_tokens_in)
        res["compacted"] += ctx.compacted
        res["substituted"] += ctx.modified and not ctx.compacted
        if json.dumps(theirs, sort_keys=True) == json.dumps(ours, sort_keys=True):
            res["same"] += 1
        elif res["first_diff"] is None:
            res["first_diff"] = i
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orig", default=os.path.join(DATA, "repro", "cliffcompaction", "src"))
    ap.add_argument("--thresholds", type=int, nargs="+", default=[20000, 50000, 100000])
    ap.add_argument("--sessions", default=os.path.join(DATA, "tb4-jobs", "**", "rollout-*.jsonl"))
    ap.add_argument("--out", default=os.path.join(REPO, "runs", "repro", "cliff.jsonl"))
    a = ap.parse_args()
    paths = sorted(glob.glob(a.sessions, recursive=True))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    tot = dict(sessions=0, requests=0, same=0, compacted=0, substituted=0)
    with open(a.out, "w", encoding="utf-8") as fh:
        for t in a.thresholds:
            for p in paths:
                r = compare(p, t, a.orig)
                r.update(session=os.path.relpath(p, ROOT), threshold=t)
                fh.write(json.dumps(r) + "\n"); fh.flush()
                tot["sessions"] += 1
                for k in ("requests", "same", "compacted", "substituted"):
                    tot[k] += r[k]
                if r["first_diff"] is not None:
                    print("DIFF", t, r["session"], "request", r["first_diff"])
    print(json.dumps(tot))


if __name__ == "__main__":
    main()
