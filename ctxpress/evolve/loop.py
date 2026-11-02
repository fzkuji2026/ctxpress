"""The evolution loop.

    python -m ctxpress.evolve.loop seed ARCHIVE --base CONFIG --ours DIR[:PREFIX] ... --seed ID=DIR[:PREFIX] ...
        put the seed policies (evolved/<ID>.py) into the archive, scored by their existing full runs against our
        method alone (G1: outline and definition reads); PREFIX is the method prefix of the jobs (default m000)
    python -m ctxpress.evolve.loop generation ARCHIVE --gen N --candidates K --checkpoints DIR --tasks T,T,...
            [--repeats R] [--minutes M] [--out RUNS_ROOT] [--parents ID,ID]
        choose K parents, let Codex write K candidates, check them, screen the ones that pass by forks from the
        checkpoints (our method alone as the paired control, run once per checkpoint set and reused), read the
        results after M minutes (unfinished forks are cancelled), and record every candidate's fitness
    python -m ctxpress.evolve.loop screen ARCHIVE --ids ID,... --checkpoints DIR --tasks ...
        recheck and screen candidates already written (e.g. after a change to the checks)
    python -m ctxpress.evolve.loop full ARCHIVE --ids ID,... --tasks ... [--repeats R] [--minutes M]
        full graded task runs for the best candidates; the control is our method's existing runs (--ours)

Fitness is the saving of the manageable bill against our method alone (ctxpress.evolve.evaluate.fitness); an entry
carries the screen and full results separately and ranks by the screen result (see _rank_fitness). Every generation writes
<ARCHIVE>/gen-NNN.json.
"""
from __future__ import annotations
import argparse, glob, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from ctxpress.evolve import archive as archive_mod, evaluate, policy, propose, waste


def _dirs(specs):
    out = []
    for s in specs or []:
        d, _, p = s.partition(":")
        out.append((d, p or "m000"))
    return out


def _reports(dirs):
    """Reports of the finished jobs (result.json written) of run directories."""
    return [r for d, p in dirs for a in waste.attempts(d, p) if glob.glob(a + "/ctxp-hb-*/result.json")
            for r in [waste.report(a)] if r]


def _save_reports(arch, cid, kind, reports):
    d = os.path.join(arch.directory, "reports")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{cid}-{kind}.json"), "w") as f:
        json.dump(reports, f, indent=1)


def _load_reports(arch, cid):
    for kind in ("full", "screen"):
        p = os.path.join(arch.directory, "reports", f"{cid}-{kind}.json")
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    return []


def _descriptor(cmp):
    calls = max(1, cmp.get("calls") or 1)
    return dict(tool_rate=cmp.get("tool_calls", 0) / calls, read_cut=cmp.get("read_cut", 0.0), unused=cmp.get("unused", 0.0))


def _rank_fitness(e):
    """Parents are ranked on one footing: the screen result when there is one (every candidate has it once seeds are
    screened too), else the full one. Full results decide confirmation and G2, not parent choice."""
    for k in ("screen", "full"):
        if e.get(k) and e[k].get("tasks"):
            return evaluate.fitness(e[k]), k
    return None, None


def seed(args):
    arch = archive_mod.Archive(args.archive)
    ours = _reports(_dirs(args.ours))
    _save_reports(arch, "ours", "full", ours)
    for spec in args.seed:
        cid, _, rest = spec.partition("=")
        if arch.get(cid):
            continue
        with open(policy.path_of(cid)) as f:
            src = f.read()
        problems, _ = policy.check(cid, src)
        reps = _reports(_dirs(rest.split(",")))
        _save_reports(arch, cid, "full", reps)
        cmp = evaluate.compare(reps, ours)
        e = arch.add(id=cid, parent=None, generation=0, sha=policy.sha(src), note=(src.split('"""')[1].strip().splitlines()[0]),
                     status="full", problems=problems, full=cmp, descriptor=_descriptor(cmp))
        arch.update(cid, fitness=_rank_fitness(e)[0], fitness_from="full")
        print(f"{cid}: {cmp.get('jobs')} jobs on {cmp.get('tasks')} tasks, saving {cmp.get('saving', 0):+.1%}, "
              f"score {cmp.get('reward_diff') or 0:+.3f}")


def _control(base, ckpts, tasks, repeats, out_root, group):
    """The screen control, our method alone forked from the same checkpoints: one run directory per checkpoint set
    and repeat count, launched the first time and re-read afterwards (evaluate.launch reuses it)."""
    key = "control-" + policy.sha(json.dumps([ckpts[t] for t in tasks]) + str(repeats))
    cfg = evaluate.config(base, key, None, tasks, repeats, workers=min(6, len(tasks) * repeats), checkpoints=ckpts)
    return evaluate.launch(cfg, group, out_root)


def _screen(arch, base, ids, ckpts, tasks, args, group):
    """Screen checked candidates by forks from the checkpoints, paired with the (cached) control; record fitness."""
    control_dir = _control(base, ckpts, tasks, args.repeats, args.out, group)
    arms = {cid: evaluate.launch(evaluate.config(base, f"screen-{cid}", cid, tasks, args.repeats,
                                                 workers=min(6, len(tasks) * args.repeats), checkpoints=ckpts), group, args.out)
            for cid in ids}
    evaluate.wait(list(arms.values()) + [control_dir], time.time() + args.minutes * 60)
    control = evaluate.collect(control_dir, screen=True)
    _save_reports(arch, os.path.basename(control_dir), "screen", control)
    for cid, d in arms.items():
        reps = evaluate.collect(d, screen=True)
        _save_reports(arch, cid, "screen", reps)
        cmp = evaluate.compare(reps, control)
        arch.update(cid, status="full" if arch.get(cid).get("full") else "screened", screen=cmp, descriptor=_descriptor(cmp) if cmp.get("tasks") else None)
        e = arch.get(cid)
        f, src = _rank_fitness(e)
        arch.update(cid, fitness=f, fitness_from=src)
        print(f"{cid} (parent {e['parent']}): {cmp.get('jobs', 0)} forks, manageable {cmp.get('saving', 0):+.1%} vs control, "
              f"read tokens {cmp.get('read_cut', 0):+.1%}, tool calls {cmp.get('tool_calls', 0)}/{cmp.get('calls', 0)}: {e['note'][:120]}")
    return dict(arms=arms, control=control_dir, control_jobs=len(control))


def screen(args):
    """Screen candidates already in the archive (rechecked first: a candidate rejected by an older check may pass)."""
    arch = archive_mod.Archive(args.archive)
    with open(args.base) as f:
        base = json.load(f)
    tasks = args.tasks.split(",")
    ckpts = evaluate.checkpoints(args.checkpoints)
    ids = []
    for cid in args.ids.split(","):
        written = os.path.join(arch.directory, "proposer", cid, "candidate.py")
        with open(written if os.path.exists(written) else policy.path_of(cid)) as f:      # seeds have no proposer file
            src = f.read()
        problems, _ = policy.check(cid, src)
        arch.update(cid, problems=problems, status="checked" if not problems else "rejected", sha=policy.sha(src))
        print(cid, "check:", problems or "passed")
        if not problems:
            ids.append(cid)
    _screen(arch, base, ids, ckpts, tasks, args, f"evolve-{time.strftime('%Y%m%d')}")
    print(arch.table())


def generation(args):
    arch = archive_mod.Archive(args.archive)
    with open(args.base) as f:
        base = json.load(f)
    tasks = args.tasks.split(",")
    ckpts = evaluate.checkpoints(args.checkpoints)
    missing = [t for t in tasks if t not in ckpts]
    if missing:
        raise SystemExit(f"no checkpoint for {missing}")
    group = f"evolve-{time.strftime('%Y%m%d')}"
    parents = args.parents.split(",") if args.parents else arch.choose(args.candidates)
    log = dict(generation=args.gen, parents=parents, tasks=tasks, repeats=args.repeats, started=time.time(), candidates=[])
    work = os.path.join(arch.directory, "proposer")

    def one(k_parent):
        k, pid = k_parent
        cid = f"g{args.gen:03d}_c{k:02d}"
        with open(policy.path_of(pid)) as f:
            psrc = f.read()
        t = time.time()
        src, note, problems, wd = propose.propose(cid, arch.get(pid), psrc, _load_reports(arch, pid), arch.table(), work)
        return dict(id=cid, parent=pid, src=src, note=note, problems=problems, workdir=wd, propose_seconds=time.time() - t)

    with ThreadPoolExecutor(max_workers=len(parents) or 1) as ex:
        made = list(ex.map(one, enumerate(parents)))
    for m in made:
        ok = m["src"] is not None and not m["problems"]
        arch.add(id=m["id"], parent=m["parent"], generation=args.gen, sha=policy.sha(m["src"] or ""), note=m["note"],
                 status="checked" if ok else "rejected", problems=m["problems"])
        log["candidates"].append({k: v for k, v in m.items() if k != "src"})
    ids = [m["id"] for m in made if m["src"] is not None and not m["problems"]]
    log.update(_screen(arch, base, ids, ckpts, tasks, args, group))
    log["finished"] = time.time()
    with open(os.path.join(arch.directory, f"gen-{args.gen:03d}.json"), "w") as f:
        json.dump(log, f, indent=1)
    print(arch.table())


def full(args):
    arch = archive_mod.Archive(args.archive)
    with open(args.base) as f:
        base = json.load(f)
    ours = _reports(_dirs(args.ours)) if args.ours else _load_reports(arch, "ours")
    group = f"evolve-{time.strftime('%Y%m%d')}"
    tasks = args.tasks.split(",")
    arms = {cid: evaluate.launch(evaluate.config(base, f"full-{cid}", cid, tasks, args.repeats, workers=6), group, args.out)
            for cid in args.ids.split(",")}
    evaluate.wait(list(arms.values()), time.time() + args.minutes * 60)
    for cid, d in arms.items():
        reps = evaluate.collect(d)
        _save_reports(arch, cid, "full", reps)
        cmp = evaluate.compare(reps, ours)
        arch.update(cid, status="full", full=cmp)
        arch.update(cid, fitness=_rank_fitness(arch.get(cid))[0], fitness_from="full")
        print(f"{cid}: {cmp.get('jobs')} runs, manageable {cmp.get('saving', 0):+.1%}, score {cmp.get('reward_diff') or 0:+.3f}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m ctxpress.evolve.loop")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("seed", "generation", "screen", "full"):
        p = sub.add_parser(name)
        p.add_argument("archive")
        p.add_argument("--base", required=True, help="an eval config of our method to copy environment and prices from")
        p.add_argument("--ours", nargs="*", help="run directories of our method alone (DIR[:PREFIX])")
        p.add_argument("--out", default=os.path.expanduser("~/ctxpress-runs/evolve"))
        p.add_argument("--minutes", type=float, default=90)
        p.add_argument("--repeats", type=int, default=1)
        p.add_argument("--tasks", default="")
    sub.choices["seed"].add_argument("--seed", nargs="+", required=True, help="ID=DIR[:PREFIX][,DIR...]")
    g = sub.choices["generation"]
    g.add_argument("--gen", type=int, required=True)
    g.add_argument("--candidates", type=int, default=3)
    g.add_argument("--checkpoints", required=True, help="run directory whose jobs saved the fork checkpoints")
    g.add_argument("--parents", default="")
    sub.choices["full"].add_argument("--ids", required=True)
    sub.choices["screen"].add_argument("--ids", required=True)
    sub.choices["screen"].add_argument("--checkpoints", required=True)
    args = ap.parse_args(argv)
    if args.cmd != "seed" and not os.path.isfile(os.environ.get("CTXPRESS_CODEX_AUTH_FILE", "")):
        raise SystemExit("set CTXPRESS_CODEX_AUTH_FILE: the proposer and every run need it")
    dict(seed=seed, generation=generation, screen=screen, full=full)[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
