"""Working View: this project's earlier method (rule approximation; the original is in adapters/codex)."""
from __future__ import annotations
from ctxpress.methods.base import Method, K


class WorkingView(Method):
    """Working View (this project's earlier method, method.zh.html). Long outputs are archived when they enter
    (a preview within budget stays, the original is on disk under an id); every K new records the
    candidates are evaluated and removed as a batch when that saves input cost over H requests; after M
    cleanings finished topics get a block summary; above `trigger` the context is cleaned down to `target`.
    The small agents' reading cost is charged as overhead."""
    source = "本项目此前的方法"

    def __init__(self, k=3, m=4, h=8, trigger=40 * K, target=24 * K, preview=1 * K):
        self.k, self.m, self.h, self.trigger, self.target, self.preview = k, m, h, trigger, target, preview
        self.memory = "label"
        self.name = f"Working View（K = {k}，M = {m}，H = {h}）"
        self.framework = dict(L1=f"入口处长输出只留 {preview // K}k 预览、原文存档；每 {k} 条新记录评估一次，按 H = {h} 的费用比较批量清理",
                              L2=f"每清理 {m} 次，对已完成的话题做块级摘要", L3=f"超过 {trigger // K}k 时清理到 {target // K}k",
                              cross="无", memory="有：大输出原文存到文件，占位符里给出路径和结论", decider="固定规则 + 小模型标注（计入费用）")

    def reset(self, sim):
        self.new, self.cleanings, self.read = 0, 0, 0

    def on_ingest(self, sim, s):
        if s["seg"] == "out":
            self.new += 1
            if s["size"] > self.preview:
                sim.truncate(s, self.preview)

    def _clean(self, sim, r, force=False):
        cand = [s for s in sim.outputs() if s.get("form", "full") in ("full", "truncated") and r - s["last"] >= 3]
        if not cand:
            return False
        self.read += sum(min(sim.seg_size(s), self.preview) for s in cand)
        saved = sum(sim.seg_size(s) - sim.PH for s in cand) * sim.CACHED * self.h
        first = min(sim.ctx.index(s) for s in cand)
        brk = (sim.WRITE - sim.CACHED) * sum(sim.seg_size(x) for x in sim.ctx[first:])
        if force or saved > brk:
            sim.to_placeholder(cand)
            self.cleanings += 1
            if self.cleanings % self.m == 0:
                segs = list(sim.segments().items())
                for sid, items in segs[:-2]:
                    sim.summarize_segment(items)
            return True
        return False

    def step(self, sim, r):
        if self.new >= self.k:
            self.new = 0
            self._clean(sim, r)
        if sim.size() > self.trigger:
            self._clean(sim, r, force=True)
            if sim.size() > self.target:
                segs = list(sim.segments().items())
                for sid, items in segs[:-1]:
                    sim.summarize_segment(items)
                    if sim.size() <= self.target:
                        break

    def overhead(self, sim, r):
        n, self.read = self.read, 0
        return n * sim.P.get("small_model_price")
