"""TokenPilot (arXiv 2606.17016)."""
from __future__ import annotations
from ctxpress.methods.base import Method, K
from ctxpress.methods.budget import BudgetSpec


class TokenPilot(Method):
    """TokenPilot (2606.17016): long outputs are truncated when they enter the context, the full text is kept
    by content hash and can be retrieved; outputs are cleared at the end of their estimated lifetime.
    This historical approximation uses idle age A instead of an LLM. Its optional
    llm_score_tokens overhead is a replay assumption, never a real model charge.
    Use TokenPilotLifecycle for the model-driven task-lifecycle adaptation."""
    source = "arXiv 2606.17016"
    budget_spec = BudgetSpec("entry_budget", "tokens", "each entering tool output", 2 * K, 1)

    def __init__(self, entry_budget=None, a=8, score=True, *, budget=None):
        if type(a) is not int or a < 0:
            raise ValueError("a must be a non-negative integer")
        if type(score) is not bool:
            raise ValueError("score must be a boolean")
        entry_budget = self.budget_spec.resolve(budget, entry_budget)
        self.budget, self.a, self.score = entry_budget, a, score
        self.memory = "id"
        self.name = f"TokenPilot（入口 {entry_budget // K}k，A = {a}）"
        self.framework = dict(L1=f"入口处截断超过 {entry_budget // K}k 的输出；闲置 {a} 步后清理", L2="无", L3="无",
                              cross="可按需取回全文", memory="有：按内容哈希存原文，取回工具按需取回", decider="闲置步数近似；LLM 开销仅用于模拟")

    def reset(self, sim):
        self.new = 0

    def on_ingest(self, sim, s):
        if s["seg"] == "out":
            self.new += 1
            if s["size"] > self.budget:
                sim.truncate(s, self.budget)

    def step(self, sim, r):
        sim.to_placeholder([s for s in sim.outputs() if s.get("form", "full") in ("full", "truncated") and r - s["last"] > self.a])

    def overhead(self, sim, r):
        n, self.new = self.new, 0
        return n * sim.P.get("llm_score_tokens") if self.score else 0.0
