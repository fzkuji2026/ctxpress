"""Evaluate tool policies with real runs: an eval config per arm, planned and run with the existing eval CLI.

An arm is our method (infinite-context v5 as in G1) with or without ReadLess(policy=<candidate>) in front of it.

    screen   forks from the train tasks' checkpoints (a saved session at call CHECKPOINT of our method alone): each
             fork continues for WINDOW tool calls, ungraded. What a window measures: manageable bill, read tokens and
             the unused-read share (ctxpress.evolve.waste). Cheap and paired: every arm starts from the same states.
    full     task-start runs, graded: score and manageable bill of whole tasks.

launch(...) writes runs/prepared/<group>/<name>/config.json, plans it and starts it in the background; collect(...)
reads whatever jobs have finished (results can be read at a fixed time, before every job is done) and cancel(...)
stops the rest.
"""
from __future__ import annotations
import copy, glob, json, os, statistics as st, subprocess, sys, time
from ctxpress.evolve import policy, waste

PREPARED = os.path.join(policy.ROOT, "runs", "prepared")
OURS = {"class": "infinite-context", "args": {
    "version": 5, "t": 72000, "tool_dialect": "full", "choose": "cost", "candidates": [], "writers": ["mechanical"],
    "guard_keep_frac": 0.142, "spec_keep_frac": 0.142, "writer_need": False}}
PROFILE = {"version": 1, "max_context_tokens": 128000, "compression_trigger_tokens": 102400}
CHECKPOINT = 10
WINDOW = 40


def method(cid, fork=False):
    """The method entry of an arm: our method alone (cid None) or the candidate's ReadLess in front of it."""
    if cid is None:
        m = copy.deepcopy(OURS)
        m["args"]["context_profile"] = PROFILE
        return dict(m, label="infinite-context")
    rl = {"class": "ReadLess", "args": {"policy": policy.module_name(cid), "ranged": False, "fresh_only": bool(fork)}}
    return {"class": "Composed", "args": {"methods": [rl, copy.deepcopy(OURS)], "context_profile": PROFILE},
            "label": f"{cid} + infinite-context"}


def config(base, name, cid, tasks, repeats, workers, checkpoints=None):
    """An eval config from `base` (a G1 config: environment, prices, model), for one arm."""
    c = copy.deepcopy(base)
    c["name"] = name
    c["tasks"] = list(tasks)
    c["methods"] = [method(cid, fork=bool(checkpoints))]
    c["repeats"], c["workers"] = repeats, workers
    run = dict(c["run"])
    run.pop("checkpoints", None)
    if checkpoints:
        run.update(max_calls=CHECKPOINT + WINDOW, grade=True,     # benchmark scope always grades
                   fork={t: {"checkpoint": checkpoints[t], "message": "Continue with the task."} for t in tasks})
    else:
        run.update(max_calls=600, grade=True)
        run.pop("fork", None)
    c["run"] = run
    return c


def checkpoints(directory, point=CHECKPOINT):
    """{task: checkpoint directory} saved at `point` calls by the jobs of a run directory."""
    out = {}
    for att in waste.attempts(directory):
        req = glob.glob(att + "/harbor-request.json")
        ck = glob.glob(att + f"/ctxp-hb-*/agent/checkpoints/{point}")
        if req and ck and os.path.exists(ck[0] + "/workspace.tgz"):
            with open(req[0]) as f:
                out.setdefault(json.load(f)["task_id"], ck[0])
    return out


def _cli(*args, timeout=3600):
    r = subprocess.run([sys.executable, "-m", "ctxpress", "eval", *args], cwd=policy.ROOT, capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"ctxpress eval {args[0]} failed: {(r.stderr or r.stdout).strip()[-600:]}")
    return r.stdout


def launch(cfg, group, out_root):
    """Plan and start one arm; returns its run directory."""
    d = os.path.join(PREPARED, group, cfg["name"])
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "config.json"), "w") as f:
        json.dump(cfg, f, indent=1)
    _cli("plan", os.path.join(d, "config.json"), "--output", os.path.join(d, "plan.json"))
    run_dir = os.path.join(out_root, cfg["name"])
    _cli("run", os.path.join(d, "plan.json"), "--directory", run_dir, "--background", timeout=600)
    return run_dir


def cancel(run_dir):
    try:
        _cli("cancel", "--directory", run_dir, timeout=900)
    except (RuntimeError, subprocess.TimeoutExpired):
        pass


def running(run_dir):
    r = subprocess.run(["pgrep", "-f", "worker --directory " + run_dir.rstrip("/") + "$"], capture_output=True)
    return r.returncode == 0


def collect(run_dir):
    """Reports of the finished jobs of a run directory (a job is finished when its trial wrote result.json)."""
    out = []
    for att in waste.attempts(run_dir):
        if not glob.glob(att + "/ctxp-hb-*/result.json"):
            continue
        r = waste.report(att)
        if r is not None and r["requests"] > 0:
            out.append(r)
    return out


def wait(run_dirs, deadline, poll=60):
    """Until every arm's worker exits or the deadline (epoch seconds) passes; then the arms still running are
    cancelled, and their finished jobs still count."""
    while time.time() < deadline and any(running(d) for d in run_dirs):
        time.sleep(poll)
    for d in run_dirs:
        if running(d):
            cancel(d)


def per_task(reports):
    tasks = {}
    for r in reports:
        tasks.setdefault(r["task"], []).append(r)
    return {t: dict(n=len(v), manage=st.mean(x["manage"] for x in v), total=st.mean(x["total"] for x in v),
                    reward=(st.mean(x["reward"] for x in v) if all(x["reward"] is not None for x in v) else None),
                    unused_share=st.mean(x["unused_share"] for x in v), read_tokens=st.mean(x["read_tokens"] for x in v),
                    tool_calls=st.mean(x["tool_calls"] for x in v), calls=st.mean(x["calls"] for x in v))
            for t, v in tasks.items()}


def compare(cand, base, max_drop=0.03):
    """Paired over the tasks both arms finished: saving of the manageable bill (summed task means, as in G1), the
    score difference (mean of task means), the per-task savings and the unused-read share of each arm."""
    c, b = per_task(cand), per_task(base)
    both = sorted(set(c) & set(b))
    if not both:
        return dict(tasks=0)
    saving = 1 - sum(c[t]["manage"] for t in both) / sum(b[t]["manage"] for t in both)
    graded = [t for t in both if c[t]["reward"] is not None and b[t]["reward"] is not None]
    dr = st.mean(c[t]["reward"] - b[t]["reward"] for t in graded) if graded else None
    per = {t: round(1 - c[t]["manage"] / b[t]["manage"], 4) if b[t]["manage"] else None for t in both}
    return dict(tasks=len(both), saving=saving, reward_diff=dr, feasible=dr is None or dr >= -max_drop,
                per_task=per, median=st.median(v for v in per.values() if v is not None),
                unused=st.mean(c[t]["unused_share"] for t in both), unused_base=st.mean(b[t]["unused_share"] for t in both),
                read_cut=1 - sum(c[t]["read_tokens"] for t in both) / max(1, sum(b[t]["read_tokens"] for t in both)),
                tool_calls=sum(c[t]["tool_calls"] for t in both), calls=sum(c[t]["calls"] for t in both),
                jobs=sum(c[t]["n"] for t in both))


def fitness(cmp, max_drop=0.03):
    """The number the archive ranks by: the manageable-bill saving; a score drop past max_drop costs 5x its size."""
    if not cmp.get("tasks"):
        return None
    f = cmp["saving"]
    if cmp.get("reward_diff") is not None and cmp["reward_diff"] < -max_drop:
        f -= 5 * (-max_drop - cmp["reward_diff"])
    return f
