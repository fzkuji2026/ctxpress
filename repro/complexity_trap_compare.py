"""The Complexity Trap: our ComplexityTrap vs the authors' released trajectories.

Each SWE-agent .traj of the masking run (config default_no_demo_N=1_M=10.yaml: last_n_observations, n=10) keeps
the raw history (`history`) and, for every step, the messages after the history processor ran
(`trajectory[i].messages`), i.e. what the model saw. We feed the raw history step by step into LiveContext with
ComplexityTrap(10) and check, for every step and every tool observation, that our text equals theirs.

Part 2 recomputes the paper's headline numbers from the released run metadata and evaluation files of the
masking run and the unmanaged ("raw") run: solve rate, total and per-instance cost, and the cost reduction.

    python repro/complexity_trap_compare.py [--dir data/repro/ct-traj/trajectories/lindenbauer/main_experiments]
"""
from __future__ import annotations
import argparse, glob, json, os, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # this repository, whatever its folder name
DATA = os.environ.get("CTXPRESS_DATA", os.path.join(ROOT, "data"))   # originals, recorded sessions
sys.path.insert(0, REPO)
from ctxpress.live.context import LiveContext
from ctxpress.methods import ComplexityTrap

DIR = os.path.join(DATA, "repro", "ct-traj", "trajectories", "lindenbauer", "main_experiments")


def text(m):
    c = m.get("content")
    if isinstance(c, list):
        return "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c or ""


def check_traj(path, n=10):
    t = json.load(open(path, encoding="utf-8"))
    hist = t["history"]
    ctx = LiveContext(ComplexityTrap(n))
    ids, fed = {}, 0
    res = dict(steps=0, obs=0, same=0, masked_theirs=0, masked_ours=0, first_diff=None)
    for step, st in enumerate(t["trajectory"]):
        theirs = st["messages"]
        L = len(theirs)
        for j in range(fed, L):                      # feed the raw history up to this step
            m = hist[j]
            if m["role"] == "system":
                ids[j] = ctx.add_message("system", text(m), fixed=True)
            elif m["role"] == "assistant":
                cid = ",".join(x.get("id", "") for x in m.get("tool_calls") or []) or f"a{j}"
                ids[j] = ctx.add_call(cid, m.get("action") or text(m))
            elif m.get("message_type") == "observation" and j > 1:
                cid = ",".join(m.get("tool_call_ids") or []) or f"o{j}"
                ids[j] = ctx.add_output(cid, text(m))
            else:                                    # the task (first user observation)
                ids[j] = ctx.add_message(m["role"], text(m))
        fed = L
        view = {v["id"]: v for v in ctx.before_request()}
        res["steps"] += 1
        for j in range(L):
            m = theirs[j]
            if hist[j].get("message_type") != "observation" or j <= 1:
                continue
            ours = view[ids[j]]["text"]
            res["obs"] += 1
            res["masked_theirs"] += text(m).startswith("Old environment output:")
            res["masked_ours"] += view[ids[j]]["form"] != "full"
            if ours == text(m):
                res["same"] += 1
            elif res["first_diff"] is None:
                res["first_diff"] = (step, j)
    return res


def headline(d):
    meta = json.load(open(os.path.join(d, "run_metadata.json"), encoding="utf-8"))
    ev = json.load(open(glob.glob(os.path.join(d, "evaluation_*.json"))[0], encoding="utf-8"))
    insts = meta["instances"]
    tok = lambda k: sum(i["model_stats"]["tokens"][k] for i in insts)
    return dict(run=os.path.basename(d), instances=len(insts), resolved=ev["resolved_instances"],
                solve_rate=ev["resolved_instances"] / 500, total_cost=meta["total_cost"],
                per_instance_cost=meta["total_cost"] / 500, input_tokens=tok("raw_input"), cached=tok("cached_input"),
                output_tokens=tok("output"), api_calls=sum(i["model_stats"]["api_calls"] for i in insts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DIR)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=os.path.join(REPO, "runs", "repro", "complexity_trap.json"))
    a = ap.parse_args()
    masking = glob.glob(os.path.join(a.dir, "*_N_1_M_10*"))[0]
    trajs = sorted(glob.glob(os.path.join(masking, "*", "*.traj")))[:a.limit]
    tot = dict(instances=0, steps=0, obs=0, same=0, masked_theirs=0, masked_ours=0, diffs=[])
    for p in trajs:
        r = check_traj(p)
        tot["instances"] += 1
        for k in ("steps", "obs", "same", "masked_theirs", "masked_ours"):
            tot[k] += r[k]
        if r["first_diff"] is not None:
            tot["diffs"].append((os.path.basename(p), r["first_diff"]))
    runs = [headline(d) for d in sorted(glob.glob(os.path.join(a.dir, "gemini_2.5_flash-agent-*")))]
    raw = next(r for r in runs if "raw" in r["run"]); mask = next(r for r in runs if "N_1_M_10" in r["run"])
    out = dict(equivalence=tot, runs=runs, cost_reduction=1 - mask["total_cost"] / raw["total_cost"])
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(out, open(a.out, "w"), indent=1)
    print(json.dumps(dict(out, equivalence=dict(tot, diffs=tot["diffs"][:5], n_diffs=len(tot["diffs"]))), indent=1))


if __name__ == "__main__":
    main()
