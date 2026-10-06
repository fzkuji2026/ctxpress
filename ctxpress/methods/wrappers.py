"""Wrappers that combine with any method (kvpress: ComposedPress, AdaKVPress, ...).

    Composed([m1, m2])          run several methods in order (each sees the result of the previous one)
    EntryTruncation(m, budget)  truncate long tool outputs when they enter the context, then run m
    PinRequirements(m)          never let m change requirements documents (spec outputs stay in full)
    WithMemory(m, label=False)  m's placeholders go to the dedicated store (id, or id plus a label)
    Trigger(m, threshold)       run m only while the context is above `threshold` tokens
"""
from __future__ import annotations
from ctxpress.methods.base import Method
from ctxpress.methods.budget import BudgetSpec


class Composed(Method):
    paper = "（组合）"

    def __init__(self, methods):
        self.methods = list(methods)
        self.memory = next((m.memory for m in self.methods if getattr(m, "memory", None)), None)
        self.hint = any(getattr(m, "hint", False) for m in self.methods)
        self.needs_model = any(getattr(m, "needs_model", False) for m in self.methods)
        self.requires_summary = any(m.requires_summary for m in self.methods)
        self.allow_native_compaction = all(m.allow_native_compaction for m in self.methods)
        self.max_overflow_retries = max((m.max_overflow_retries for m in self.methods), default=0)
        self.real = all(getattr(m, "real", True) for m in self.methods)
        self.codex_config = {}
        self.parameters = None
        for method in self.methods:
            if method.parameters is not None:
                from ctxpress.core.policy import same_parameters
                if self.parameters is not None and not same_parameters(self.parameters, method.parameters):
                    raise ValueError("composed methods have conflicting frozen parameters")
                self.parameters = method.parameters
            for key, value in method.codex_config.items():
                if key in self.codex_config and self.codex_config[key] != value:
                    raise ValueError(f"composed methods have conflicting Codex setting: {key}")
                self.codex_config[key] = value
        self.name = " + ".join(m.name for m in self.methods)
        self.framework = {k: "；".join(m.framework.get(k, "无") for m in self.methods if m.framework.get(k, "无") != "无") or "无"
                          for k in ("L1", "L2", "L3", "cross", "memory", "decider")}
        self.agent_tools = tuple(t for m in self.methods for t in m.agent_tools)
        names = [t["name"] for t in self.agent_tools]
        if len(names) != len(set(names)):
            raise ValueError("composed methods offer the same agent tool twice")
        for hook in ("placeholder_text", "output_text"):     # a component's own wording (the original's)
            owner = next((m for m in self.methods if hasattr(m, hook)), None)
            if owner is not None:
                setattr(self, hook, getattr(owner, hook))

    def reset(self, sim):
        for m in self.methods:
            m.reset(sim)

    @property
    def instructions(self):
        # Repeated components need one copy of their guidance, in execution order.
        return "\n\n".join(dict.fromkeys(m.instructions for m in self.methods if m.instructions))

    def validate_live(self):
        for method in self.methods:
            method.validate_live()

    def on_ingest(self, sim, item):
        for m in self.methods:
            m.on_ingest(sim, item)

    def step(self, sim, r):
        for m in self.methods:
            m.step(sim, r)

    def overhead(self, sim, r):
        return sum(m.overhead(sim, r) for m in self.methods)

    def on_fault(self, sim, item):
        for m in self.methods:
            m.on_fault(sim, item)

    def call_tool(self, name, args):
        for m in self.methods:
            if any(t["name"] == name for t in m.agent_tools):
                return m.call_tool(name, args)
        raise KeyError(f"unknown tool {name}")

    def on_overflow(self, sim, request):
        # Stop after a component changes the view: later components must see
        # the matching rendered request on the next rejected attempt.
        for method in self.methods:
            if method.on_overflow(sim, request):
                return True
        return False


class _Wrap(Method):
    def __init__(self, inner):
        self.inner = inner
        for k in ("memory", "hint", "unbounded", "needs_model", "real", "source", "paper", "requires_summary", "codex_config", "parameters",
                  "max_overflow_retries", "allow_native_compaction", "placeholder_text", "output_text", "agent_tools"):
            if hasattr(inner, k):
                setattr(self, k, getattr(inner, k))
        self.framework = dict(getattr(inner, "framework", {}))

    def reset(self, sim):
        self.inner.reset(sim)

    @property
    def instructions(self):
        return self.inner.instructions

    def validate_live(self):
        self.inner.validate_live()

    def on_ingest(self, sim, item):
        self.inner.on_ingest(sim, item)

    def step(self, sim, r):
        self.inner.step(sim, r)

    def overhead(self, sim, r):
        return self.inner.overhead(sim, r)

    def on_fault(self, sim, item):
        self.inner.on_fault(sim, item)

    def on_overflow(self, sim, request):
        return self.inner.on_overflow(sim, request)

    def call_tool(self, name, args):
        return self.inner.call_tool(name, args)


class EntryTruncation(_Wrap):
    budget_spec = BudgetSpec("budget", "tokens", "each entering tool output", 2000, 1)

    def __init__(self, inner, budget=2000):
        budget = self.budget_spec.validate(budget)
        super().__init__(inner)
        self.budget = budget
        self.name = f"{inner.name} + 入口截断（{budget}）"
        self.framework["L1"] = f"入口处截断超过 {budget} token 的输出；" + self.framework.get("L1", "无")

    def on_ingest(self, sim, item):
        if item["seg"] == "out" and item["size"] > self.budget:
            sim.truncate(item, self.budget)
        self.inner.on_ingest(sim, item)


class PinRequirements(_Wrap):
    forwards_budget = True

    def __init__(self, inner):
        super().__init__(inner)
        self.name = f"{inner.name} + 需求文档固定保留"
        self.framework["cross"] = "需求文档固定保留"

    def on_ingest(self, sim, item):
        if item["seg"] == "out" and item.get("kind") == "spec":
            sim.protect([item])
        self.inner.on_ingest(sim, item)

    def step(self, sim, r):
        sim.protect(s for s in sim.ctx if s["seg"] == "out" and s.get("kind") == "spec")
        self.inner.step(sim, r)


class WithMemory(_Wrap):
    forwards_budget = True

    def __init__(self, inner, label=False):
        super().__init__(inner)
        self.memory = "label" if label else "id"
        self.name = f"{inner.name} + 专用记忆{'（带标签）' if label else ''}"
        self.framework["memory"] = "有：被移出的原文按编号存储，占位符给出位置" + ("和类型" if label else "")


class Trigger(_Wrap):
    forwards_budget = True

    def __init__(self, inner, threshold=128000):
        super().__init__(inner)
        self.threshold = threshold
        self.name = f"{inner.name}（仅在超过 {threshold // 1000}k 时）"

    def step(self, sim, r):
        if sim.size() > self.threshold:
            self.inner.step(sim, r)

    def on_overflow(self, sim, request):
        if sim.size() > self.threshold:
            return self.inner.on_overflow(sim, request)
        return False
