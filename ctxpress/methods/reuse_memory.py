"""Long- and short-term memory of how content gets reused, for infinite-context V5.

The question: an item idle for `a` requests, will it be needed within the next H requests? `needed` follows
core/reuse.py: a read (or spec) output is needed when its file is edited while it is the latest read of that file,
and it dies when the file is read again; a search or command output is needed when a file it listed is first read or
edited; spec outputs are needed by every edit.

Long-term memory (a profile, built from finished sessions only): counts of (needed, asked) per (item type, age bucket)
pooled over all sessions, and per value of each session attribute (task, task type, language, ...; any attribute can
be added) and per phase (explore / work). Each table is shrunk to the pooled one with strength N0.

Short-term memory (this session): every long-term estimate ("expert") is scored on this session's resolved decision
points (a point resolves H requests after it was asked), and the prediction is the experts' average weighted by
how well each has predicted this session so far (Bayesian model averaging). So the session decides which habit
fits it; before it has evidence the weights are equal.

repro/memory_reuse_check.py measured these estimators on recorded sessions (experiment-method.zh.html, finding 21);
repro/build_reuse_profile.py writes a profile for one session's attributes.
"""
from __future__ import annotations
import collections, math
from ctxpress.core.calibration import bucket

H = 20                      # horizon in requests
N0 = 50                     # long-term shrinkage
AGES = (1, 2, 3, 5, 8, 13, 21, 34, 55, 89)
PROFILE_SCHEMA = "ctxpress.reuse-memory"


def item_type(x):
    """As core/engine.item_type for outputs (the only items this memory is asked about)."""
    return x.get("kind") or "other"


def _key(*parts):
    return "|".join(str(p) for p in parts)


def profile(points_by_session, attrs):
    """A profile for a session with attributes `attrs` from finished sessions.

    points_by_session: iterable of (session_attrs, points), points = [(r, type, bucket, phase, label)].
    Only the tables the target session can use are kept: pooled, phase, and each of its attribute values."""
    tables = collections.defaultdict(lambda: [0, 0])
    sessions = 0
    for sattrs, pts in points_by_session:
        sessions += 1
        for _, t, b, p, y in pts:
            for k in ("*", _key("phase", p)) + tuple(_key(a, v) for a, v in attrs.items() if sattrs.get(a) == v):
                cell = tables[_key(k, t, b)]
                cell[0] += 1
                cell[1] += y
    return dict(schema=PROFILE_SCHEMA, version=1, attrs=dict(attrs), sessions=sessions, H=H, N0=N0,
                tables={k: v for k, v in sorted(tables.items())})


class ReuseMemory:
    """Online predictor for one session: long-term profile + this session's evidence."""

    def __init__(self, prof):
        if not prof or prof.get("schema") != PROFILE_SCHEMA:
            raise ValueError("reuse profile must come from ctxpress.methods.reuse_memory.profile")
        self.tables = prof["tables"]
        self.attrs = prof.get("attrs") or {}
        self.h = int(prof.get("H", H))
        self.n0 = float(prof.get("N0", N0))
        n = y = 0
        for k, (c, p) in self.tables.items():
            if k.startswith("*|"):
                n += c
                y += p
        self.p0 = y / n if n else 0.01
        self.experts = ["global", *self.attrs, "phase"]
        self.logw = dict.fromkeys(self.experts, 0.0)
        self.r = 0
        self.items = {}           # id -> dict(type, born, last, needs, dead)
        self.latest = {}          # path -> id of the latest read / spec output of that path
        self.listed = {}          # path -> id of the first search / command output listing it
        self.pending = collections.deque()      # (r, id, {expert: p})
        self.resolved = 0

    # ------------------------------------------------------------------ long-term estimates
    def _cell(self, *k):
        return self.tables.get(_key(*k), (0, 0))

    def estimates(self, t, b, phase):
        n, y = self._cell("*", t, b)
        g = (y + self.n0 * self.p0) / (n + self.n0)
        out = {"global": g}
        for a, v in self.attrs.items():
            n, y = self._cell(_key(a, v), t, b)
            out[a] = (y + self.n0 * g) / (n + self.n0)
        n, y = self._cell(_key("phase", phase), t, b)
        out["phase"] = (y + self.n0 * g) / (n + self.n0)
        return {e: min(max(p, 1e-4), 1 - 1e-4) for e, p in out.items()}

    def _mix(self, est):
        top = max(self.logw.values())
        w = {e: math.exp(self.logw[e] - top) for e in self.experts}
        return sum(w[e] * est[e] for e in self.experts) / sum(w.values())

    # ------------------------------------------------------------------ events
    def call(self, kind, res):
        """A tool call was made at the current request."""
        res = list(res or ())
        for p in res:
            if kind == "edit" and p in self.latest:
                self._need(self.latest[p])
            elif kind in ("read", "edit") and p not in self.latest and p in self.listed:
                self._need(self.listed[p])
        if kind == "edit":
            for i, it in self.items.items():
                if it["type"] == "spec" and it["dead"] is None:
                    self._need(i)

    def output(self, iid, kind, res, outpaths):
        """A tool output arrived at the current request."""
        if iid is None or iid in self.items:
            return
        self.items[iid] = dict(type=kind or "other", born=self.r, last=self.r, needs=[], dead=None)
        if kind in ("read", "spec"):
            for p in res or ():
                old = self.latest.get(p)
                if old is not None and old in self.items and self.items[old]["dead"] is None:
                    self.items[old]["dead"] = self.r
                self.latest[p] = iid
        for p in outpaths or ():
            self.listed.setdefault(p, iid)

    def _need(self, iid):
        it = self.items.get(iid)
        if it is not None and it["dead"] is None:
            it["needs"].append(self.r)
            it["last"] = self.r

    def request(self, r, phase):
        """A new request: resolve old decision points, then ask at the ages where buckets start."""
        self.r = r
        while self.pending and self.pending[0][0] + self.h <= r:
            asked, iid, said = self.pending.popleft()
            it = self.items[iid]
            end = min(asked + self.h, it["dead"] if it["dead"] is not None else asked + self.h)
            y = any(asked <= n < end for n in it["needs"])
            for e, q in said.items():
                self.logw[e] += math.log(q if y else 1 - q)
            self.resolved += 1
        for iid, it in self.items.items():
            if it["dead"] is None and (r - it["last"]) in AGES:
                self.pending.append((r, iid, self.estimates(it["type"], bucket(r - it["last"]), phase)))

    # ------------------------------------------------------------------ prediction
    def p(self, iid, kind, phase):
        """Chance the item is needed within H requests (mixture of the long-term habits, weighted by this session)."""
        it = self.items.get(iid)
        age = max(1, self.r - it["last"]) if it else 1
        return self._mix(self.estimates(it["type"] if it else (kind or "other"), bucket(age), phase))

    def weights(self):
        top = max(self.logw.values())
        w = {e: math.exp(self.logw[e] - top) for e in self.experts}
        z = sum(w.values())
        return {e: round(v / z, 3) for e, v in w.items()}
