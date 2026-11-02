"""The archive of tool policies (in the manner of the Darwin Goedel Machine and AlphaEvolve): every candidate ever
proposed stays, with its parent, its check result and its scores, and any evaluated one can become a parent again.

<directory>/archive.json   {"entries": [entry, ...]}; an entry:
    id, parent, generation, sha (of the source), note (the proposer's one-line description), status
    ("rejected" | "checked" | "screened" | "full"), problems, screen / full (evaluate.compare results), fitness,
    descriptor (behaviour: read-tool calls per call, read-token cut, unused share), children

Parent choice: rank by fitness, add a novelty bonus (mean distance of its descriptor to its K nearest neighbours),
damp parents that already have many children, and sample from a softmax over that score, so that good but rarely
tried branches keep getting picked.
"""
from __future__ import annotations
import json, math, os, random

K = 3
NOVELTY = 0.5            # weight of the novelty rank against the fitness rank
TEMPERATURE = 0.3


class Archive:
    def __init__(self, directory):
        self.directory = directory
        self.path = os.path.join(directory, "archive.json")
        os.makedirs(directory, exist_ok=True)
        self.entries = []
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.entries = json.load(f)["entries"]

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dict(entries=self.entries), f, indent=1)
        os.replace(tmp, self.path)

    def get(self, cid):
        return next((e for e in self.entries if e["id"] == cid), None)

    def add(self, **entry):
        entry.setdefault("children", 0)
        entry.setdefault("fitness", None)
        self.entries.append(entry)
        if entry.get("parent") and self.get(entry["parent"]):
            self.get(entry["parent"])["children"] += 1
        self.save()
        return entry

    def update(self, cid, **fields):
        self.get(cid).update(fields)
        self.save()

    def next_id(self, generation):
        n = sum(1 for e in self.entries if e.get("generation") == generation)
        return f"g{generation:03d}_c{n:02d}"

    def evaluated(self):
        return [e for e in self.entries if e.get("fitness") is not None]

    def novelty(self, e, pool):
        d = e.get("descriptor")
        if not d:
            return 0.0
        others = [o["descriptor"] for o in pool if o is not e and o.get("descriptor")]
        if not others:
            return 1.0
        dist = sorted(math.sqrt(sum((d[k] - o.get(k, 0)) ** 2 for k in d)) for o in others)
        return sum(dist[:K]) / min(K, len(dist))

    def scores(self):
        pool = self.evaluated()
        if not pool:
            return {}
        by_fit = sorted(pool, key=lambda e: e["fitness"])
        nov = {e["id"]: self.novelty(e, pool) for e in pool}
        by_nov = sorted(pool, key=lambda e: nov[e["id"]])
        n = max(1, len(pool) - 1)
        out = {}
        for e in pool:
            s = by_fit.index(e) / n + NOVELTY * by_nov.index(e) / n
            out[e["id"]] = s / math.sqrt(1 + e.get("children", 0))
        return out

    def choose(self, n, rng=None):
        """n parents (with repetition) by softmax over the selection scores."""
        rng = rng or random.Random(len(self.entries))
        sc = self.scores()
        if not sc:
            return []
        ids = list(sc)
        top = max(sc.values())
        w = [math.exp((sc[i] - top) / TEMPERATURE) for i in ids]
        return [rng.choices(ids, weights=w)[0] for _ in range(n)]

    def best(self, key="fitness"):
        pool = [e for e in self.entries if e.get(key) is not None]
        return max(pool, key=lambda e: e[key]) if pool else None

    def table(self):
        """One line per entry, for the proposer and for reports."""
        rows = []
        for e in self.entries:
            f = "-" if e.get("fitness") is None else f"{e['fitness']:+.1%}"
            rows.append(f"{e['id']} parent={e.get('parent') or '-'} status={e['status']} fitness={f}: {e.get('note', '')[:160]}")
        return "\n".join(rows)
