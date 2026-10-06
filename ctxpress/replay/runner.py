"""Run an experiment config: every method on every session, ours with lambda chosen leave-one-session-out,
then check the hard constraint against the reference (standard compaction) under each performance proxy.

Config (YAML)
  name        output folder under results/
  manifest    session manifest (relative to the config's folder)       default sessions.yaml
  sessions    optional subset of session names
  preset      openai | anthropic-5m | anthropic-1h                      default openai
  params      overrides of ctxpress.params (e.g. {q/memlabel/spec: 0.5, recompress_retention: 0.85})
  reference   method entry of the standard compaction
  baselines   list of method entries {class, args, label}
  ours        list of {label, class: CostModel, args, lambdas: [...], kinds: [...]}
  kinds       performance proxies to report                              default [strict, harm, measured]
"""
from __future__ import annotations
import json, os, sys, time
from concurrent.futures import ProcessPoolExecutor
import yaml
os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count() or 4))
from ctxpress.replay import corpus
from ctxpress.core import engine
from ctxpress.core import metrics
from ctxpress.core import params as P
from ctxpress.methods import build

_traces = {}


def preset(name):
    return {"openai": P.DEFAULT, "anthropic-5m": P.anthropic(300), "anthropic-1h": P.anthropic(3600)}[name or "openai"]


def _load(manifest, names):
    key = (manifest, tuple(names or ()))
    if key not in _traces:
        _traces[key] = corpus.load_manifest(manifest, names)
    return _traces[key]


def _job(args):
    manifest, names, i, entry, lam, prm = args
    traces = _load(manifest, names)
    tr = traces[i]
    train = [u for j, u in enumerate(traces) if j != i]
    m = build(entry, train=train, **({"lam": lam} if lam is not None else {}))
    t0 = time.time()
    r = engine.run(tr, m, prm)
    r["seconds"] = time.time() - t0
    return (entry.get("label") or m.name, lam, tr["name"]), r


def run_config(path, workers=None, out_root=None):
    """Run a config. With `sweep: [{name, preset, params}, ...]` the whole experiment is repeated once per entry
    (parameter sensitivity); results go to results/<name>/<sweep name>/ plus a combined sweep.md."""
    cfg = yaml.safe_load(open(path, encoding="utf-8"))
    base_dir = os.path.dirname(os.path.abspath(path))
    if cfg.get("sweep"):
        root = os.path.join(out_root or os.path.join(os.path.dirname(base_dir), "results"), cfg["name"])
        parts, combined = [], []
        for i, sw in enumerate(cfg["sweep"]):
            c = dict(cfg); c.pop("sweep"); c["name"] = sw["name"]; c["dir"] = sw.get("dir", f"{i:02d}")
            if "preset" in sw:
                c["preset"] = sw["preset"]
            c["params"] = {**(cfg.get("params") or {}), **(sw.get("params") or {})}
            summary, md, out = run_cfg(c, base_dir, workers, root)
            parts.append((sw, summary)); combined.append(md)
        md = sweep_report(cfg, parts)
        open(os.path.join(root, "sweep.md"), "w", encoding="utf-8").write(md)
        return parts, md, root
    return run_cfg(cfg, base_dir, workers, out_root)


def run_cfg(cfg, base_dir, workers=None, out_root=None):
    manifest = os.path.join(base_dir, cfg.get("manifest", "sessions.yaml"))
    names = cfg.get("sessions")
    prm = preset(cfg.get("preset")).override(cfg.get("params") or {})
    traces = _load(manifest, names)
    snames = [t["name"] for t in traces]
    ref_entry = dict(cfg["reference"]); ref_entry.setdefault("label", "标准压缩")
    jobs = []
    for i in range(len(traces)):
        jobs.append((manifest, names, i, {"class": "NoCompaction", "label": "不压缩"}, None, prm))
        jobs.append((manifest, names, i, ref_entry, None, prm))
        for b in cfg.get("baselines", []):
            jobs.append((manifest, names, i, b, None, prm))
        for o in cfg.get("ours", []):
            for lam in o.get("lambdas", [None]):
                jobs.append((manifest, names, i, o, float(lam) if lam is not None else None, prm))
    runs = {}
    workers = workers or min(8, os.cpu_count() or 2)
    t0 = time.time()
    if workers > 1:
        with ProcessPoolExecutor(workers) as ex:
            for n, (k, r) in enumerate(ex.map(_job, jobs, chunksize=1)):
                runs[k] = r
                if n % 20 == 0:
                    print(f"  {n + 1}/{len(jobs)} runs  {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
    else:
        for job in jobs:
            k, r = _job(job); runs[k] = r
    summary = summarize(cfg, runs, snames, prm, ref_entry["label"])
    out = os.path.join(out_root or os.path.join(os.path.dirname(base_dir), "results"), cfg.get("dir", cfg["name"]))
    os.makedirs(out, exist_ok=True)
    json.dump({"config": cfg, "runs": {"|".join(map(str, k)): v for k, v in runs.items()}}, open(os.path.join(out, "runs.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(summary, open(os.path.join(out, "summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    md = report(cfg, summary, snames)
    open(os.path.join(out, "report.md"), "w", encoding="utf-8").write(md)
    return summary, md, out


def summarize(cfg, runs, snames, prm, ref):
    eps, ho = prm.get("eps"), prm.get("harm_other")
    base = {s: runs[("不压缩", None, s)] for s in snames}
    total = lambda rows: sum(r["cost"] * base[r["session"]]["cost"] for r in rows) / sum(base[s]["cost"] for s in snames)
    out = {"sessions": snames, "kinds": {}}
    for kind in cfg.get("kinds", list(metrics.KINDS)):
        K = out["kinds"][kind] = {"baselines": {}, "ours": {}, "oracle": {}}
        for b in [{"label": "不压缩"}, {"label": ref}] + cfg.get("baselines", []):
            label = b.get("label") or build(b, train=[]).name
            rows = []
            for s in snames:
                r, rf = runs[(label, None, s)], runs[(ref, None, s)]
                rows.append(dict(session=s, cost=r["cost"] / base[s]["cost"], perf=metrics.perf(r, kind, ho),
                                 ref=metrics.perf(rf, kind, ho), ok=metrics.ok(r, rf, kind, eps, ho)))
            K["baselines"][label] = dict(rows=rows, n_ok=sum(x["ok"] for x in rows), total=total(rows))
        for o in cfg.get("ours", []):
            if kind not in o.get("kinds", list(metrics.KINDS)):
                continue
            lams = [float(x) for x in o.get("lambdas", [None])]
            label = o["label"]
            rows, orows = [], []
            for s in snames:
                train = [u for u in snames if u != s]
                chosen = next((lam for lam in lams if all(metrics.ok(runs[(label, lam, u)], runs[(ref, None, u)], kind, eps, ho) for u in train)), None)
                rf = runs[(ref, None, s)]
                r = runs[(label, chosen, s)] if chosen is not None else rf
                rows.append(dict(session=s, lam=chosen, cost=r["cost"] / base[s]["cost"], perf=metrics.perf(r, kind, ho),
                                 ref=metrics.perf(rf, kind, ho), ok=metrics.ok(r, rf, kind, eps, ho), fallback=chosen is None))
                good = [lam for lam in lams if metrics.ok(runs[(label, lam, s)], rf, kind, eps, ho)]
                best = min(good, key=lambda lam: runs[(label, lam, s)]["cost"]) if good else None
                orows.append(dict(session=s, lam=best, cost=(runs[(label, best, s)] if best is not None else rf)["cost"] / base[s]["cost"]))
            K["ours"][label] = dict(rows=rows, n_ok=sum(x["ok"] for x in rows), total=total(rows))
            K["oracle"][label] = dict(rows=orows, total=total(orows))
    return out


def report(cfg, summary, snames):
    L = [f"# {cfg['name']}", "", cfg.get("description", "").strip(), "",
         f"Sessions: {', '.join(snames)}. Cost is relative to no compression (token-weighted over sessions). "
         "✗ = worse than the standard compaction on that session. Ours: λ chosen on the other sessions; [退回] = no λ "
         "satisfied them, so the standard compaction is used.", ""]
    for kind, K in summary["kinds"].items():
        L += [f"## performance = {kind}", "", "| 方法 | 满足 / %d | 总计费 | %s |" % (len(snames), " | ".join(snames)),
              "|" + "---|" * (3 + len(snames))]
        for label, v in K["baselines"].items():
            L.append(f"| {label} | {v['n_ok']} | {v['total']:.2f} | " + " | ".join(f"{r['cost']:.2f}{'' if r['ok'] else ' ✗'}" for r in v["rows"]) + " |")
        for label, v in K["ours"].items():
            L.append(f"| **{label}** | {v['n_ok']} | {v['total']:.2f} | " + " | ".join(
                f"{r['cost']:.2f}{'' if r['ok'] else ' ✗'} [{'退回' if r['fallback'] else format(r['lam'], '.0e')}]" for r in v["rows"]) + " |")
        for label, v in K["oracle"].items():
            L.append(f"| （每会话最优 λ）{label} | | {v['total']:.2f} | " + " | ".join(f"{r['cost']:.2f}" for r in v["rows"]) + " |")
        L.append("")
    return "\n".join(L)


def sweep_report(cfg, parts):
    """One row per sweep setting: for each method, sessions satisfied and total cost under each proxy."""
    L = [f"# {cfg['name']}", "", cfg.get("description", "").strip(), ""]
    for kind in cfg.get("kinds", list(metrics.KINDS)):
        labels = []
        for _, s in parts:
            K = s["kinds"].get(kind, {})
            for grp in ("baselines", "ours"):
                for lab in K.get(grp, {}):
                    if lab not in labels:
                        labels.append(lab)
        L += [f"## performance = {kind}（每格：满足会话数 / 总计费）", "", "| 设置 | " + " | ".join(labels) + " |", "|" + "---|" * (1 + len(labels))]
        for sw, s in parts:
            K = s["kinds"].get(kind, {})
            cells = []
            for lab in labels:
                v = K.get("baselines", {}).get(lab) or K.get("ours", {}).get(lab)
                cells.append(f"{v['n_ok']} / {v['total']:.2f}" if v else "未评估")
            L.append(f"| {sw['name']} | " + " | ".join(cells) + " |")
        L.append("")
    return "\n".join(L)
