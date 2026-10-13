"""Long- and short-term memory for reuse prediction: does combining them predict better than either alone?

Question at a decision point (an item idle for `a` requests at request r): will it be needed within the next H
requests? (`needed` as in core/reuse.py: re-read, edited, a listed path opened, a spec consulted by an edit.)
Decision points are taken at the ages where the reuse buckets start, as an operator would ask.

Estimators, all keyed by (item type, age bucket) and all causal:
  global    pooled over the training sessions                                  (long-term, no topic)
  lang      training sessions of the same language, shrunk to global (n0)      (long-term topic habit)
  phase     training sessions, same phase at the decision (explore / work), shrunk to global (n0)
  lang+phase  same language and phase, shrunk to phase (n0)
  session   this session's resolved decision points only (a point resolves H requests later), shrunk to 0.5
  global+session, phase+session
            this session's resolved points shrunk to the long-term estimate (m0)
  phase+session+decay
            as above, the session counts halved at every new user message (a new topic)
  lang+phase+session+decay
            the same, shrunk to lang+phase: all three memories
  mix       the long-term estimates (global, lang, phase, lang+phase) weighted by how well each has predicted this
            session's resolved points so far (Bayesian model averaging): the session picks the habit that fits
  mix+session  this session's counts shrunk to mix (m0)
Long-term estimates are fitted leave-one-repository-out: a session's repository never trains its own prior.
n0, m0, H and the decay are fixed before looking at the results.

Replay only: the labels are what the recorded agent did; how removals would change its behaviour is not measured.

    python repro/memory_reuse_check.py --index runs/repro/rollout_index.jsonl [--out runs/repro/memory_reuse.json]
"""
from __future__ import annotations
import argparse, collections, hashlib, json, math, os, re, sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ctxpress.core.calibration import bucket  # noqa: E402
from ctxpress.core.engine import item_type  # noqa: E402
from ctxpress.core.reuse import _items  # noqa: E402
from ctxpress.replay.loaders.codex import load  # noqa: E402

H = 20                      # horizon: needed within the next H requests
N0 = 50                     # long-term shrinkage (as core/reuse.smoothed)
M0 = 16                     # short-term shrinkage (infinite-context prior_strength)
DECAY = 0.5                 # session counts kept at a new user message
AGES = (1, 2, 3, 5, 8, 13, 21, 34, 55, 89)
WORK_WINDOW = 16
EXPERTS = ("global", "lang", "phase", "lang+phase")
LANG = {"navidrome": "go", "zeromicro": "go", "BurntSushi": "rust", "nushell": "rust", "scikit-learn": "python",
        "apache": "java", "element-hq": "typescript"}


def language(task, bench):
    if bench == "swe-bench-verified":
        return "python"
    return next((v for k, v in LANG.items() if task.startswith(k)), "other")


def points(path):
    """Decision points of one session in request order: (r, type, bucket, phase, label, user_turn)."""
    tr = load(path, stop_at_compaction=False, keep_text=False, fit_alpha=False)
    reqs = tr["reqs"]
    phase, user_at, calls, seen_call = [], [], collections.deque(maxlen=WORK_WINDOW), False
    for q in reqs:                                   # phase and user turns as known at each request
        new_user = False
        for x in q["before"]:
            if x["seg"] == "call":
                calls.append(x.get("kind") == "edit")
                seen_call = True
            elif x["seg"] == "out" and x.get("test"):
                calls.append(True)
            elif x["seg"] == "msg" and x.get("role") == "user" and seen_call:
                new_user = True
        phase.append("work" if any(calls) else "explore")
        user_at.append(new_user)
    out = []
    for it in _items(tr, item_type):
        needs = sorted(set(it["needs"]))
        last, k = it["born"], 0
        for r in range(it["born"] + 1, it["dead"]):
            while k < len(needs) and needs[k] < r:
                last = needs[k]
                k += 1
            a = r - last
            if a in AGES:
                y = k < len(needs) and needs[k] < min(r + H, it["dead"])
                out.append((r, it["type"], bucket(a), phase[r], int(y)))
    out.sort()
    return dict(points=out, user_at=user_at, requests=len(reqs))


def job(row):
    try:
        return row, points(row["path"])
    except Exception as error:                      # an unreadable rollout is reported, not hidden
        return row, dict(error=repr(error))


class Counts:
    def __init__(self):
        self.c = collections.defaultdict(lambda: [0.0, 0.0])

    def add(self, key, y, w=1.0):
        v = self.c[key]
        v[0] += w
        v[1] += w * y

    def get(self, key):
        return self.c.get(key, (0.0, 0.0))


def shrink(n, y, prior, strength):
    return (y + strength * prior) / (n + strength)


def fit(sessions):
    """Long-term tables from training sessions: global and by language / phase, each (type, bucket)."""
    g, lang, ph, lp, base = Counts(), Counts(), Counts(), Counts(), Counts()
    for s in sessions:
        for r, t, b, p, y in s["points"]:
            g.add((t, b), y)
            base.add("*", y)
            lang.add((s["lang"], t, b), y)
            ph.add((p, t, b), y)
            lp.add((s["lang"], p, t, b), y)
    n, yy = base.get("*")
    p0 = yy / n if n else 0.5

    def glob(t, b):
        n, y = g.get((t, b))
        return shrink(n, y, p0, N0)

    def by_lang(l, t, b):
        n, y = lang.get((l, t, b))
        return shrink(n, y, glob(t, b), N0)

    def by_phase(p, t, b):
        n, y = ph.get((p, t, b))
        return shrink(n, y, glob(t, b), N0)

    def by_lang_phase(l, p, t, b):
        n, y = lp.get((l, p, t, b))
        return shrink(n, y, by_phase(p, t, b), N0)
    return glob, by_lang, by_phase, by_lang_phase


def evaluate(s, glob, by_lang, by_phase, by_lang_phase):
    """Predictions for every decision point of session s, in request order, using only resolved points."""
    sess, sess_d = Counts(), Counts()
    pending, preds, j = collections.deque(), collections.defaultdict(list), 0
    logw = dict.fromkeys(EXPERTS, 0.0)             # how well each long-term habit has predicted this session
    pts = s["points"]
    for r in range(s["requests"]):
        if s["user_at"][r]:
            for v in sess_d.c.values():
                v[0] *= DECAY
                v[1] *= DECAY
        while pending and pending[0][0][0] + H <= r:    # resolved: its horizon has passed
            (_, t, b, p, y), said = pending.popleft()
            for e, q in said.items():
                logw[e] += math.log(q if y else 1 - q)
            sess.add((t, b), y)
            sess_d.add((t, b), y)
        while j < len(pts) and pts[j][0] == r:
            _, t, b, p, y = pts[j]
            g, ph = glob(t, b), by_phase(p, t, b)
            n, yy = sess.get((t, b))
            nd, yd = sess_d.get((t, b))
            lph = by_lang_phase(s["lang"], p, t, b)
            est = {"global": g, "lang": by_lang(s["lang"], t, b), "phase": ph, "lang+phase": lph,
                   "session": shrink(n, yy, 0.5, 1.0),
                   "global+session": shrink(n, yy, g, M0),
                   "phase+session": shrink(n, yy, ph, M0),
                   "phase+session+decay": shrink(nd, yd, ph, M0),
                   "lang+phase+session+decay": shrink(nd, yd, lph, M0)}
            top = max(logw.values())
            w = {e: math.exp(logw[e] - top) for e in EXPERTS}
            mix = sum(w[e] * est[e] for e in EXPERTS) / sum(w.values())
            est["mix"] = mix
            est["mix+session"] = shrink(n, yy, mix, M0)
            for k, v in est.items():
                preds[k].append((min(max(v, 1e-4), 1 - 1e-4), y))
            pending.append((pts[j], {e: min(max(est[e], 1e-4), 1 - 1e-4) for e in EXPERTS}))
            j += 1
    return preds


def metrics(pairs):
    n = len(pairs)
    if not n:
        return {}
    ll = -sum(y * math.log(p) + (1 - y) * math.log(1 - p) for p, y in pairs) / n
    br = sum((p - y) ** 2 for p, y in pairs) / n
    pos = sum(y for _, y in pairs)
    auc = None
    if 0 < pos < n:                                 # rank AUC with ties averaged
        order = sorted(range(n), key=lambda i: pairs[i][0])
        ranks, i = [0.0] * n, 0
        while i < n:
            k = i
            while k + 1 < n and pairs[order[k + 1]][0] == pairs[order[i]][0]:
                k += 1
            for m in range(i, k + 1):
                ranks[order[m]] = (i + k) / 2 + 1
            i = k + 1
        auc = (sum(ranks[i] for i in range(n) if pairs[i][1]) - pos * (pos + 1) / 2) / (pos * (n - pos))
    return dict(points=n, positive_rate=pos / n, logloss=ll, brier=br, auc=auc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True, help="rollout index (JSON lines with path, bench, task)")
    ap.add_argument("--min-bytes", type=int, default=200_000)
    ap.add_argument("--order", choices=("repo-out", "time"), default="repo-out",
                    help="repo-out: long-term tables from the other repositories; time: from earlier sessions only, "
                         "keyed by each session attribute (task, task type, language)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--out", default=os.path.join("runs", "repro", "memory_reuse.json"))
    a = ap.parse_args()
    rows, seen = [], set()
    for line in open(a.index, encoding="utf-8"):
        r = json.loads(line)
        if r["bench"] not in ("swe-milestone", "swe-bench-verified") or r["bytes"] < a.min_bytes:
            continue
        if not os.path.exists(r["path"]):
            continue
        digest = hashlib.sha256(open(r["path"], "rb").read()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        rows.append(r)
    print(len(rows), "sessions", flush=True)
    sessions = []
    with ProcessPoolExecutor(a.workers) as pool:
        for row, res in pool.map(job, rows, chunksize=1):
            if "error" in res:
                print("skip", row["path"][-80:], res["error"], flush=True)
                continue
            repo = row["task"] if row["bench"] == "swe-milestone" else "swe-bench-verified"
            sessions.append(dict(res, repo=repo, lang=language(row["task"] or "", row["bench"]),
                                 method=row.get("cls"), path=row["path"], started=started(row["path"]),
                                 attrs=dict(task=row["task"], task_type=TASK_TYPE.get(row["bench"], row["bench"]),
                                            lang=language(row["task"] or "", row["bench"]))))
    repos = sorted({s["repo"] for s in sessions})
    print({r: sum(s["repo"] == r for s in sessions) for r in repos}, flush=True)
    if a.order == "time":
        return main_time(a, sessions, repos)
    pooled, per_repo = collections.defaultdict(list), {}
    for held in repos:                               # leave one repository out for the long-term tables
        tables = fit([s for s in sessions if s["repo"] != held])
        rp = collections.defaultdict(list)
        for s in sessions:
            if s["repo"] == held:
                for k, v in evaluate(s, *tables).items():
                    rp[k] += v
                    pooled[k] += v
        per_repo[held] = {k: metrics(v) for k, v in rp.items()}
    result = dict(settings=dict(H=H, N0=N0, M0=M0, DECAY=DECAY, ages=AGES, min_bytes=a.min_bytes),
                  sessions=len(sessions), repos={r: sum(s["repo"] == r for s in sessions) for r in repos},
                  pooled={k: metrics(v) for k, v in pooled.items()}, per_repo=per_repo)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    json.dump(result, open(a.out, "w"), indent=1)
    print(f"\n{'estimator':<26} logloss   brier    auc")
    for k, m in result["pooled"].items():
        print(f"{k:<26} {m['logloss']:.4f}  {m['brier']:.4f}  {m['auc']:.4f}")
    print("\nlogloss by held-out repository")
    names = list(result["pooled"])
    print(" " * 40 + "  ".join(f"{n[:12]:>12}" for n in names))
    for r, ms in per_repo.items():
        print(f"{r[:38]:<40}" + "  ".join(f"{ms[n]['logloss']:12.4f}" for n in names))
    print("\nAUC by held-out repository")
    print(" " * 40 + "  ".join(f"{n[:12]:>12}" for n in names))
    for r, ms in per_repo.items():
        print(f"{r[:38]:<40}" + "  ".join(f"{ms[n]['auc'] or 0:12.3f}" for n in names))


TASK_TYPE = {"swe-milestone": "milestone-chain", "swe-bench-verified": "issue-fix"}
ATTRS = ("task", "task_type", "lang")         # any session attribute can be a long-term key (a user type, ...)


def started(path):
    """Start time from the rollout name (rollout-YYYY-MM-DDTHH-MM-SS-...)."""
    m = re.search(r"rollout-(\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d)", path)
    return m.group(1) if m else ""


class Memory:
    """Long-term memory: counts from finished sessions, pooled and per value of each attribute."""
    def __init__(self):
        self.g, self.base, self.by = Counts(), Counts(), {k: Counts() for k in ATTRS}
        self.ph = Counts()

    def learn(self, s):
        for r, t, b, p, y in s["points"]:
            self.g.add((t, b), y)
            self.base.add("*", y)
            self.ph.add((p, t, b), y)
            for k in ATTRS:
                self.by[k].add((s["attrs"][k], t, b), y)

    def experts(self, s):
        """Estimators for session s: global, one per attribute (shrunk to global), phase."""
        n, yy = self.base.get("*")
        p0 = yy / n if n else 0.01

        def glob(t, b, p):
            n, y = self.g.get((t, b))
            return shrink(n, y, p0, N0)
        out = {"global": glob}
        for k in ATTRS:
            def by(t, b, p, k=k, v=s["attrs"][k]):
                n, y = self.by[k].get((v, t, b))
                return shrink(n, y, glob(t, b, p), N0)
            out[k] = by

        def phase(t, b, p):
            n, y = self.ph.get((p, t, b))
            return shrink(n, y, glob(t, b, p), N0)
        out["phase"] = phase
        return out

    def seen(self, s, k):
        return any(v[0] for (val, *_), v in self.by[k].c.items() if val == s["attrs"][k])


def evaluate_experts(s, experts):
    """Each expert, the session alone, global+session, and the mixture of experts weighted by how well each
    predicted this session's resolved points (with and without this session's counts)."""
    names = list(experts)
    sess, pending, preds, j = Counts(), collections.deque(), collections.defaultdict(list), 0
    logw = dict.fromkeys(names, 0.0)
    pts = s["points"]
    clip = lambda v: min(max(v, 1e-4), 1 - 1e-4)
    for r in range(s["requests"]):
        while pending and pending[0][0][0] + H <= r:
            (_, t, b, p, y), said = pending.popleft()
            for e, q in said.items():
                logw[e] += math.log(q if y else 1 - q)
            sess.add((t, b), y)
        while j < len(pts) and pts[j][0] == r:
            _, t, b, p, y = pts[j]
            est = {e: clip(f(t, b, p)) for e, f in experts.items()}
            n, yy = sess.get((t, b))
            top = max(logw.values())
            w = {e: math.exp(logw[e] - top) for e in names}
            mix = sum(w[e] * est[e] for e in names) / sum(w.values())
            out = dict(est, session=shrink(n, yy, 0.5, 1.0), **{"global+session": shrink(n, yy, est["global"], M0),
                       "task+session": shrink(n, yy, est["task"], M0), "mix": mix,
                       "mix+session": shrink(n, yy, mix, M0)})
            for k, v in out.items():
                preds[k].append((clip(v), y))
            pending.append((pts[j], est))
            j += 1
    return preds


def main_time(a, sessions, repos):
    """Sessions in start order; each is predicted from the sessions that finished before it started."""
    sessions.sort(key=lambda s: s["started"])
    mem, pooled, rp, warm = Memory(), collections.defaultdict(list), collections.defaultdict(
        lambda: collections.defaultdict(list)), collections.defaultdict(list)
    for s in sessions:
        has_task = mem.seen(s, "task")
        for k, v in evaluate_experts(s, mem.experts(s)).items():
            pooled[k] += v
            rp[s["repo"]][k] += v
            if has_task:                      # sessions whose task had been seen before
                warm[k] += v
        mem.learn(s)
    result = dict(order="time", settings=dict(H=H, N0=N0, M0=M0, ages=AGES, attrs=ATTRS, min_bytes=a.min_bytes),
                  sessions=len(sessions), repos={r: sum(s["repo"] == r for s in sessions) for r in repos},
                  pooled={k: metrics(v) for k, v in pooled.items()},
                  task_seen_before={k: metrics(v) for k, v in warm.items()},
                  per_repo={r: {k: metrics(v) for k, v in d.items()} for r, d in rp.items()})
    json.dump(result, open(a.out, "w"), indent=1)
    names = list(result["pooled"])
    print(f"\n{'estimator':<16} {'logloss':>8} {'auc':>6}   task seen before: {'logloss':>8} {'auc':>6}")
    for k in names:
        m, w = result["pooled"][k], result["task_seen_before"].get(k, {})
        print(f"{k:<16} {m['logloss']:8.4f} {m['auc'] or 0:6.3f}   {'':18}{w.get('logloss', 0):8.4f} {w.get('auc') or 0:6.3f}")
    print("\nAUC by repository (time order)")
    print(" " * 24 + " ".join(f"{n[:11]:>11}" for n in names))
    for r, ms in result["per_repo"].items():
        print(f"{r[:22]:<24}" + " ".join(f"{ms[n]['auc'] or 0:11.3f}" for n in names))


if __name__ == "__main__":
    main()
