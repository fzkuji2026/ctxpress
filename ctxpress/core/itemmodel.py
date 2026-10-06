"""Per-item reuse prediction: instead of one curve per type, predict for each tool output / patch at each idle
age whether it will be needed again, from features available at decision time:
  type (one-hot), idle age, size, how many times its file was read before, how many patches the agent already
  wrote to that file, whether its file appeared in a search result.
Training rows come from the same 'needed' definition as sim.hazard (leave-one-session-out in the experiments)."""
import math, collections
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from ctxpress.core.engine import item_type

TYPES = ["read", "spec", "search", "command", "patch", "call", "edit"]


def features(itype, a, size, reads, patches, in_search):
    return [*(1.0 if itype == t else 0.0 for t in TYPES), math.log1p(a), math.log1p(size), math.log1p(reads),
            math.log1p(patches), 1.0 if in_search else 0.0]


def rows(traces, alpha_default=1.17, max_rows_per_item=60):
    X, y = [], []
    for tr in traces:
        reqs = tr["reqs"]; alpha = tr.get("alpha", alpha_default)
        items, latest, hits, patches_of = [], {}, {}, collections.defaultdict(list)
        reads_cnt, patch_cnt, searched = collections.Counter(), collections.Counter(), set()
        for r, q in enumerate(reqs):
            for x in q["before"]:
                if x["seg"] not in ("out", "call"):
                    continue
                res = x.get("res", [])
                it = dict(x, born=r, needs=[], dead=len(reqs), type=item_type(x), size_t=x["size"] * alpha,
                          reads=sum(reads_cnt[p] for p in res), patches=sum(patch_cnt[p] for p in res),
                          in_search=any(p in searched for p in res))
                if x["seg"] == "call" and x.get("kind") == "edit":
                    for p in res:
                        patches_of[p].append(it); patch_cnt[p] += 1
                if x["seg"] == "out":
                    for p in res:
                        if x["kind"] in ("read", "spec"):
                            if p in latest:
                                latest[p]["dead"] = min(latest[p]["dead"], r)
                            for pt in patches_of[p]:
                                pt["dead"] = min(pt["dead"], r)
                            latest[p] = it; patches_of[p] = []; reads_cnt[p] += 1
                    for p in x.get("outpaths", []):
                        hits.setdefault(p, it); searched.add(p)
                items.append(it)
            if r + 1 < len(reqs):
                for c in [x for x in reqs[r + 1]["before"] if x["seg"] == "call"]:
                    for p in c.get("res", []):
                        if c["kind"] == "edit" and p in latest:
                            latest[p]["needs"].append(r)
                            for pt in patches_of[p]:
                                pt["needs"].append(r)
                        elif c["kind"] in ("read", "edit") and p not in latest and p in hits:
                            hits[p]["needs"].append(r)
                    if c["kind"] == "edit":
                        for it in items:
                            if it["type"] == "spec" and it["dead"] > r:
                                it["needs"].append(r)
        for it in items:
            last = it["born"]; nx = sorted(set(it["needs"])); n = 0
            for r in range(it["born"] + 1, it["dead"]):
                while nx and nx[0] < r:
                    last = nx.pop(0)
                a = r - last
                if a < 1:
                    continue
                n += 1
                if n > max_rows_per_item and a > 8 and (r % 3):
                    continue                 # thin out long tails of old items
                X.append(features(it["type"], a, it["size_t"], it["reads"], it["patches"], it["in_search"]))
                y.append(1 if nx else 0)
    return np.array(X), np.array(y)


class ReuseModel:
    def __init__(self, traces):
        X, y = rows(traces)
        self.clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08, max_leaf_nodes=15, random_state=0)
        self.clf.fit(X, y)
        self.n = len(y); self.pos = float(y.mean())

    def p(self, feats):
        return float(self.clf.predict_proba([feats])[0, 1])

    def p_many(self, F):
        return self.clf.predict_proba(np.array(F))[:, 1] if F else []
