"""ScoredMethod: the analogue of kvpress's ScorerPress. Score every tool output in context; while the outputs
in context exceed `budget` tokens, apply `op` to the lowest-scored ones (the newest `protect` are never touched).
Many methods differ only in the score:
    recency  -> Complexity Trap-like (keep the newest)
    idle     -> Pichay-like (keep what was used recently)
    size     -> drop the largest first
    type     -> ClawVM-like (requirements and code edits are worth more than search results)
Subclass and override `score(sim, item, r)` to make a new method."""
from __future__ import annotations
from ctxpress.methods.base import Method, replaceable
from ctxpress.methods.budget import BudgetSpec

TYPE_VALUE = {"spec": 5, "read": 3, "command": 2, "search": 1, "other": 1, "edit": 4}


class ScoredMethod(Method):
    paper = "（基类）"
    needs_model = False
    real = True
    budget_spec = BudgetSpec("budget", "tokens", "mutable tool output representations", 16000)

    def __init__(self, budget=16000, op="placeholder", protect=1, score="recency", truncate_to=1000):
        budget = self.budget_spec.validate(budget)
        if op not in ("placeholder", "truncate", "structure", "delete"):
            raise ValueError("ScoredMethod op must be placeholder, truncate, structure or delete")
        if type(protect) is not int or protect < 0:
            raise ValueError("ScoredMethod protect must be a nonnegative integer")
        if type(truncate_to) is not int or truncate_to < 1:
            raise ValueError("ScoredMethod truncate_to must be a positive integer")
        self.budget, self.op, self.protect, self.score_name, self.truncate_to = budget, op, protect, score, truncate_to
        self.name = f"Scored（{score}，预算 {budget // 1000}k，{op}）"
        self.framework = dict(L1=f"工具输出超过 {budget // 1000}k token 时，按 {score} 分数从低到高{op}", L2="无", L3="无",
                              cross="无", memory="无", decider="打分规则")

    def score(self, sim, s, r):
        if self.score_name == "recency":
            return s["born"]
        if self.score_name == "idle":
            return s["last"]
        if self.score_name == "size":
            return -sim.seg_size(s)
        if self.score_name == "type":
            return TYPE_VALUE.get(s.get("kind"), 1) * 1000 + s["last"]
        raise ValueError(self.score_name)

    def step(self, sim, r):
        outs = [s for s in sim.outputs()]
        eligible = [s for s in outs if self.op == "delete" or replaceable(s)]
        used = sum(sim.seg_size(s) for s in outs)
        if used <= self.budget:
            return
        recent = {s["id"] for s in outs[-self.protect:]} if self.protect else set()
        for s in sorted((s for s in eligible if s["id"] not in recent), key=lambda s: self.score(sim, s, r)):
            if used <= self.budget:
                break
            before = sim.seg_size(s)
            if self.op == "placeholder":
                if sim.placeholder_size() >= before:
                    continue
                sim.to_placeholder([s])
            elif self.op == "truncate":
                if not sim.truncate(s, self.truncate_to):
                    continue
            elif self.op == "structure":
                if not sim.structure(s):
                    continue
            elif self.op == "delete":
                pair = [x for x in sim.ctx if x["seg"] == "call" and x.get("call_id") and x.get("call_id") == s.get("call_id")]
                sim.delete([s] + pair)
                used -= before
                continue
            used -= before - sim.seg_size(s)
