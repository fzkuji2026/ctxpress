"""Observation masking: keep the newest tool outputs, replace the rest by placeholders."""
from __future__ import annotations
from ctxpress.methods.base import Method, K, replaceable
from ctxpress.methods.budget import BudgetSpec


class ComplexityTrap(Method):
    """The Complexity Trap (Lindenbauer et al.): every request, tool outputs older than the newest N become
    placeholders (observation masking). As SWE-agent's LastNObservations in the authors' fork: the task is never
    masked, `polling` > 1 updates the cut only every `polling` outputs (cache friendly), and the placeholder reads
    "Old environment output: (K lines omitted)"."""
    context_role = 'local'  # edits tool outputs only; the host keeps its native compaction
    source = "arXiv 2508.21433"
    budget_spec = BudgetSpec("n", "outputs", "recent tool outputs", 10)

    def __init__(self, n=None, pin_spec=False, polling=1, *, budget=None, trigger_tokens=None):
        n = self.budget_spec.resolve(budget, n)
        if trigger_tokens is not None and (type(trigger_tokens) is not int or trigger_tokens <= 0):
            raise ValueError('trigger_tokens must be a positive integer or None')
        self.budget = n
        self.n, self.pin_spec, self.polling = n, pin_spec, polling
        # Window adaptation (not the author's rule): mask only on requests whose complete input reaches
        # trigger_tokens; outputs masked then stay masked. None keeps the author's every-request rolling mask.
        self.trigger_tokens = trigger_tokens
        self.name = f"Complexity Trap（N = {n}）"
        when = "每次请求" if trigger_tokens is None else f"输入达到 {trigger_tokens} token 时"
        self.framework = dict(L1=f"{when}，最近 {n} 条之外的工具输出换成占位符", L2="无", L3="无", cross="无", memory="无", decider="固定规则")

    def step(self, sim, r):
        if self.trigger_tokens is not None:
            from ctxpress.methods.experiment_context import context_tokens
            if context_tokens(sim) < self.trigger_tokens:
                return
        outs = sim.outputs()
        cut = max(0, ((len(outs) + 1) // self.polling) * self.polling - self.n - 1)   # +1/-1: their count includes the task
        sim.to_placeholder([s for s in outs[:cut] if replaceable(s) and not (self.pin_spec and s["kind"] == "spec")])

    @staticmethod
    def placeholder_text(s, text):
        return f"Old environment output: ({len(text.splitlines())} lines omitted)"


class KeepLastTokens(Method):
    """Every request: keep tool outputs from newest to oldest while they fit in B tokens (the §4 arm)."""
    source = "本研究 §4 的对照组"
    budget_spec = BudgetSpec("b", "tokens", "recent original tool outputs", 16 * K)

    def __init__(self, b=None, *, budget=None):
        b = self.budget_spec.resolve(budget, b)
        self.budget = b
        self.b = b
        self.name = f"按 token 只留最近工具输出（{b // K}k）"
        self.framework = dict(L1=f"每次请求，最近 {b // K}k token 之外的工具输出换成占位符", L2="无", L3="无", cross="无", memory="无", decider="固定规则")

    def step(self, sim, r):
        used, drop = 0, []
        for s in reversed(sim.outputs()):
            used += s["size"]
            if used > self.b and replaceable(s):
                drop.append(s)
        sim.to_placeholder(drop)
