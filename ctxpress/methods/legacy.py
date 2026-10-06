"""Earlier rule approximations kept so that the website's replay tables (§8, §9.3) can be reproduced.
They are not the papers' methods: see cliff.py, pichay.py, clawvm.py for those."""
from __future__ import annotations
from ctxpress.methods.base import Method, K, replaceable
from ctxpress.core.trace import content_type
from ctxpress.methods.budget import BudgetSpec


class ClearThenSummarize(Method):
    """Above T, every tool output becomes its call record (re-runnable); if still above F*T, summarize.
    (Earlier stand-in for CliffCompaction; the original turned out to work differently, see CliffCompaction.)"""
    source = "本研究"

    def __init__(self, t=64 * K, f=0.6, size=None):
        self.t, self.f, self.size = t, f, size
        self.name = f"先清空再摘要（{t // K}k）"
        self.framework = dict(L1=f"超过 {t // K}k 时，全部工具输出换成调用记录", L2="无", L3=f"仍超过 {f:g}×阈值 再整体摘要",
                              cross="保留调用记录，可按原调用找回", memory="无（重新执行原调用）", decider="固定阈值")

    def step(self, sim, r):
        if sim.size() > self.t:
            sim.to_placeholder([s for s in sim.outputs() if replaceable(s)], form="placeholder")
            if sim.size() > self.f * self.t:
                sim.summarize(size=self.size)


class PichayApprox(Method):
    """Earlier idle-based approximation of Pichay (kept for the §8 results; Pichay is the port): demand paging. Tool outputs idle for A requests are paged out to the proxy's store;
    a page fault (the agent needs it again) pins that file for the rest of the session."""
    source = "arXiv 2603.09023"

    def __init__(self, a=5, memory="id"):
        self.a, self.memory = a, memory
        self.name = f"Pichay（A = {a}）"
        self.framework = dict(L1=f"闲置 {a} 步的工具输出换出", L2="无", L3="模型主动发起（未模拟）", cross="被找回过的文件固定保留",
                              memory="有：换出的内容存在代理里，缺页时换回" if memory else "无", decider="固定规则 + 缺页反馈")

    def reset(self, sim):
        self.pinned = set()

    def step(self, sim, r):
        sim.to_placeholder([s for s in sim.outputs() if replaceable(s) and r - s["last"] > self.a
                            and not (set(s.get("res", [])) & self.pinned)])

    def on_fault(self, sim, item):
        self.pinned |= set(item.get("res", []))


class ClawVMApprox(Method):
    """Earlier rule approximation of ClawVM (kept for the §8 results; ClawVM is the port): typed pages with a minimum fidelity per type, backed by a persistent store.
    Every request, outputs older than the newest N drop to their type's minimum fidelity:
    requirements stay; code reads / run output / search results keep their structure; others become
    placeholders. simplified=True is the §8 version (placeholders, requirements pinned, no store)."""
    source = "arXiv 2604.10352"
    budget_spec = BudgetSpec("n", "outputs", "recent full tool outputs in the approximation", 5)

    def __init__(self, n=None, simplified=False, *, budget=None):
        n = self.budget_spec.resolve(budget, n)
        self.budget = n
        self.n, self.simplified = n, simplified
        self.memory = None if simplified else "id"
        self.name = f"ClawVM（{'简化，' if simplified else ''}N = {n}）"
        self.framework = dict(L1=(f"最近 {n} 条之外换成占位符，需求文档固定" if simplified else
                                  f"最近 {n} 条之外降到该类型的最低保真度：代码/运行/搜索留结构，其他换占位符"),
                              L2="无", L3="开（生命周期边界写回，未模拟）", cross="约束类（需求文档）硬固定",
                              memory="无（简化版）" if simplified else "有：持久的后备存储", decider="固定规则")

    def step(self, sim, r):
        outs = sim.outputs()
        for s in outs[:-self.n] if self.n else outs:
            if s.get("form", "full") != "full" or s["kind"] == "spec":
                continue
            if not self.simplified and content_type(s) in ("read_code", "run", "search") and sim.structure(s):
                continue
            sim.to_placeholder([s])
