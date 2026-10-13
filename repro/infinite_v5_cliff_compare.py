"""infinite-context V5 with its default (Cliff) switches vs CliffCompaction, request by request.

CliffCompaction is itself byte-identical to the authors' engine on these sessions (repro/cliff_compare.py), so
equality here shows Cliff is a special case of infinite-context V5. Every recorded Codex session is turned back into
the requests Codex sent; each goes through a Rewriter with each method; the outgoing `input` lists must match. A
rewrite the host contract refuses (the proxy answers 422 and sends nothing) counts as equal when both methods refuse
the same request; the session ends there, as it would live.

    python repro/infinite_v5_cliff_compare.py [--thresholds 20000 50000 100000]
"""
from __future__ import annotations
import argparse, copy, glob, json, os, sys
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("CTXPRESS_DATA", os.path.join(ROOT, "data"))
sys.path.insert(0, REPO)
from ctxpress.replay.loaders.codex import requests_from_rollout as requests  # noqa: E402


def compare(path, threshold, switches=None):
    from ctxpress.live.contract import HostContractError
    from ctxpress.live.rewrite import Rewriter
    from ctxpress.methods import CliffCompaction, InfiniteContext
    instr, reqs = requests(path)
    cliff = Rewriter(lambda: CliffCompaction(t=threshold))
    v5 = Rewriter(lambda: InfiniteContext(version=5, t=threshold, **(switches or {})))
    res = dict(requests=len(reqs), same=0, changed=0, refused=0, first_diff=None)

    def send(rw, body):
        try:
            return json.dumps(rw.rewrite_body(copy.deepcopy(body), "s")[0]["input"], sort_keys=True)
        except HostContractError as error:
            return "refused: " + str(error)
    for i, inp in enumerate(reqs):
        body = {"model": "gpt-5", "instructions": instr, "input": inp, "tools": [], "stream": True}
        a, b = send(cliff, body), send(v5, body)
        res["changed"] += a != json.dumps(inp, sort_keys=True)
        if a == b:
            res["same"] += 1
        elif res["first_diff"] is None:
            res["first_diff"] = i
        if a.startswith("refused: ") or b.startswith("refused: "):
            res["refused"] = 1
            res["requests"] = i + 1
            break
    return res


def job(arg):
    path, t = arg
    return path, t, compare(path, t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--thresholds", type=int, nargs="+", default=[20000, 50000, 100000])
    ap.add_argument("--sessions", default=os.path.join(DATA, "tb4-jobs", "**", "rollout-*.jsonl"))
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--out", default=os.path.join(REPO, "runs", "repro", "infinite_v5_cliff.jsonl"))
    a = ap.parse_args()
    paths = sorted(glob.glob(a.sessions, recursive=True))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    tot = dict(sessions=0, requests=0, same=0, changed=0, refused=0)
    with open(a.out, "w", encoding="utf-8") as fh, ProcessPoolExecutor(a.workers) as pool:
        for p, t, r in pool.map(job, [(p, t) for t in a.thresholds for p in paths], chunksize=1):
            r.update(session=os.path.relpath(p, ROOT).replace(os.sep, "/"), threshold=t)
            fh.write(json.dumps(r) + "\n")
            fh.flush()
            tot["sessions"] += 1
            for k in ("requests", "same", "changed", "refused"):
                tot[k] += r[k]
            if r["first_diff"] is not None:
                print("DIFF", t, r["session"], "request", r["first_diff"], flush=True)
    print(json.dumps(tot))


if __name__ == "__main__":
    main()
