"""ARC (arXiv 2607.25066)."""
from __future__ import annotations
from ctxpress.methods.base import Method, replaceable
from ctxpress.methods.budget import BudgetSpec


class ARC(Method):
    """ARC (2607.25066): an append-only log of everything; old tool outputs in context are replaced by
    reference ids that the agent can resolve to the original text."""
    source = "arXiv 2607.25066"
    budget_spec = BudgetSpec("n", "outputs", "recent full tool outputs", 5)

    def __init__(self, n=None, label=False, *, budget=None):
        n = self.budget_spec.resolve(budget, n)
        self.budget = n
        self.n = n
        self.memory = "label" if label else "id"
        self.name = f"ARC（N = {n}{'，带标签' if label else ''}）"
        self.framework = dict(L1=f"最近 {n} 条之外的工具输出换成引用编号", L2="无", L3="无", cross="按编号取回原文",
                              memory="有：只追加的日志，按编号取回原文", decider="固定规则")

    def step(self, sim, r):
        outs = sim.outputs()
        old = outs[:-self.n] if self.n else outs
        sim.to_placeholder([s for s in old if replaceable(s)])
