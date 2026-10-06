"""How long does a tool output stay useful? Estimated from traces, never set by hand.

p_k(a)  chance that an item of type k, idle for a requests, is needed again before it is superseded
        (its file re-read) or the session ends
m_k(a)  expected number of further requests it would be carried until then
Both are estimated from *other* sessions (leave-one-out in the experiments) and shrunk towards the pooled
curve: (n p_k + n0 p_all) / (n + n0). `remaining` gives the expected number of further requests.
`coverage_rates` estimates, for truncation / structured compression, how often the kept part was enough
for the edits that later needed the item (used by the cost model when deciding; the simulator checks the
actual text)."""
from __future__ import annotations
import collections
from ctxpress.core import engine
from ctxpress.core import textops
from ctxpress.core.trace import content_type
from ctxpress.core.calibration import bucket, remaining_from_lengths

def _items(tr, type_fn):
    """Items of a trace with their need times and death time (same 'needed' definition as the engine)."""
    reqs = tr["reqs"]; items = []; latest = {}; hits = {}; patches = collections.defaultdict(list)
    for r, q in enumerate(reqs):
        for x in q["before"]:
            if x["seg"] not in ("out", "call"):
                continue
            it = dict(x, born=r, needs=[], dead=len(reqs), type=type_fn(x))
            if x["seg"] == "call" and x.get("kind") == "edit":
                for p in x.get("res", []):
                    patches[p].append(it)
            if x["seg"] == "out":
                for p in x.get("res", []):
                    if x["kind"] in ("read", "spec"):
                        if p in latest:
                            latest[p]["dead"] = min(latest[p]["dead"], r)
                        for pt in patches[p]:
                            pt["dead"] = min(pt["dead"], r)
                        latest[p] = it; patches[p] = []
                for p in x.get("outpaths", []):
                    hits.setdefault(p, it)
            items.append(it)
        if r + 1 < len(reqs):
            for c in [x for x in reqs[r + 1]["before"] if x["seg"] == "call"]:
                for p in c.get("res", []):
                    if c["kind"] == "edit" and p in latest:
                        latest[p]["needs"].append(r); latest[p].setdefault("need_calls", []).append((c, p))
                        for pt in patches[p]:
                            pt["needs"].append(r)
                    elif c["kind"] in ("read", "edit") and p not in latest and p in hits:
                        hits[p]["needs"].append(r)
                if c["kind"] == "edit":
                    for it in items:
                        if it["type"] == "spec" and it["dead"] > r:
                            it["needs"].append(r)
    return items


def hazard(traces, type_fn=engine.item_type):
    acc = collections.defaultdict(lambda: [0, 0, 0.0])
    for tr in traces:
        for it in _items(tr, type_fn):
            last = it["born"]; nx = sorted(set(it["needs"]))
            for r in range(it["born"] + 1, it["dead"]):
                if nx and nx[0] < r:
                    last = nx.pop(0)
                    while nx and nx[0] < r:
                        last = nx.pop(0)
                a = r - last
                if a < 1:
                    continue
                future = [n for n in nx if n >= r]
                horizon = (future[0] if future else it["dead"]) - r + 1
                for k in (it["type"], "*"):
                    v = acc[(k, bucket(a))]
                    v[0] += 1; v[1] += bool(future); v[2] += horizon
    tab = collections.defaultdict(dict)
    for (k, b), (n, y, h) in acc.items():
        tab[k][str(b)] = (y / n, h / n)
    return dict(tab)


def hazard_counts(traces, type_fn=engine.item_type):
    """Hazard tables plus an approximate number of observations behind each cell (items per type)."""
    tab = hazard(traces, type_fn)
    counts = collections.Counter()
    for tr in traces:
        alive = collections.Counter()
        for q in tr["reqs"]:
            for x in q["before"]:
                if x["seg"] in ("out", "call"):
                    alive[type_fn(x)] += 1
        for k, n in alive.items():
            for b in tab.get(k, {}):
                counts[(k, b)] += n
    return tab, counts


def smoothed(tab, n_tab, n0=50):
    out = {}
    for k, t in tab.items():
        out[k] = {}
        for b, (p, m) in t.items():
            pa, ma = tab["*"].get(b, (p, m)); n = n_tab.get((k, b), 0)
            w = n / (n + n0)
            out[k][b] = (w * p + (1 - w) * pa, w * m + (1 - w) * ma)
    return out


class Curves:
    """Per-type reuse curves with lookup by idle age."""
    def __init__(self, traces, type_fn=engine.item_type, smooth=True, n0=50):
        tab, counts = hazard_counts(traces, type_fn)
        self.raw = tab
        self.tab = smoothed(tab, counts, n0) if smooth else tab
        self.type_fn = type_fn

    def pm(self, s, a, use_types=True):
        t = self.tab.get(self.type_fn(s)) if use_types else None
        t = t or self.tab["*"]
        b = str(bucket(a))
        return t.get(b, t[max(t, key=lambda x: int(x))])


def remaining(traces):
    return remaining_from_lengths(len(t["reqs"]) for t in traces)


def coverage_rates(traces, budget_tokens=2000):
    """Share of later edit needs that the truncated / structured version of a read still covers, by content
    type, measured on the actual text of the training traces."""
    acc = collections.defaultdict(lambda: [0, 0])
    for tr in traces:
        for it in _items(tr, engine.item_type):
            if it["seg"] != "out" or "text" not in it or not it.get("need_calls"):
                continue
            ct = content_type(it)
            versions = {"truncated": textops.head_tail(it["text"], budget_tokens * 4),
                        "structured": textops.structured(it["text"], ct)}
            for c, p in it["need_calls"]:
                an = (c.get("anchors") or {}).get(p)
                if not an:
                    continue
                for form, kt in versions.items():
                    ok = sum(1 for a in an if a in kt) / len(an) >= 0.9
                    v = acc[(form, ct)]; v[0] += 1; v[1] += ok
    return {k: (y / n, n) for k, (n, y) in acc.items()}
