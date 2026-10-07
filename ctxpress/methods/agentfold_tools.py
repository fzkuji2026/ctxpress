"""Agent-selected granular/deep folding, adapted from AgentFold §3.2–3.3.

This exposes a native tool instead of parsing the paper's four-block model
response. It uses the host model, not the authors' fine-tuned 30B policy. The
legacy AgentFold class remains the fixed-segment approximation.
"""
from __future__ import annotations

import json

from ctxpress.methods.base import Method


class AgentFoldTools(Method):
    source = "arXiv 2510.24699"
    name = "AgentFold (host-tool adaptation)"
    instructions = (
        "Tool observations carry AgentFold step IDs. Use fold_context to replace completed "
        "steps with a concise, factual summary you write yourself. Set start_step and end_step "
        "to the inclusive range. Fold one step to preserve useful details, or consolidate several "
        "steps and whole prior folds when their intermediate details are no longer needed. "
        "Preserve findings, source references, failures and pending requirements. Never split a "
        "prior folded range. Call fold_context separately from other tools. This tool adapts "
        "AgentFold; it does not load the authors' trained folding policy.")
    framework = dict(L1="无", L2="Agent 自主写摘要并选择折叠范围", L3="可再次合并完整旧折叠",
                     cross="固定指令与用户消息保留", memory="无", decider="宿主 Agent，未训练策略")
    agent_tools = (dict(name="fold_context", description="Replace a complete range of past AgentFold steps with your summary.",
                       inputSchema=dict(type="object", properties=dict(
                           start_step=dict(type="integer", minimum=1),
                           end_step=dict(type="integer", minimum=1),
                           summary=dict(type="string", minLength=1)),
                           required=["start_step", "end_step", "summary"], additionalProperties=False)),)

    def reset(self, sim):
        self.done = set()

    def call_tool(self, name, args):
        if name.rsplit("__", 1)[-1].rsplit(".", 1)[-1] != "fold_context":
            raise KeyError(name)
        if (not isinstance(args, dict) or set(args) != {"start_step", "end_step", "summary"} or
                any(type(args[k]) is not int for k in ("start_step", "end_step")) or
                not 1 <= args["start_step"] <= args["end_step"] or
                not isinstance(args["summary"], str) or not args["summary"].strip()):
            return "Error: supply positive start_step <= end_step and a nonempty summary.", True
        return "recorded", False

    @staticmethod
    def output_text(item, original):
        return f"[AgentFold step {item['turn']}]\n{original}"

    @staticmethod
    def _span(item):
        return item.get("fold_span", (item.get("turn", 0), item.get("turn", 0)))

    def _fold(self, sim, call, args):
        lo, hi = args["start_step"], args["end_step"]
        if hi >= call.get("turn", 0):
            return "Error: range includes the current or a future step."
        before = sim.ctx[:next(i for i, s in enumerate(sim.ctx) if s is call)]
        selected = []
        spans = []
        for item in before:
            a, b = self._span(item)
            if b < lo or a > hi:
                continue
            if item.get("protected") or item.get("role") in ("system", "developer", "user") or item.get("has_media"):
                # No folding across user corrections, host state or media.
                return "Error: selected range contains protected history; nothing was folded."
            if a < lo or b > hi:
                return "Error: cannot split an existing folded range."
            spans.append((a, b))
            selected.append(item)
        if not selected or min(a for a, b in spans) != lo or max(b for a, b in spans) != hi:
            return "Error: range is unavailable or includes the current step."
        # Require complete range coverage, including previously folded ranges.
        end = lo - 1
        for a, b in sorted(spans):
            if a > end + 1:
                return "Error: selected range has a missing step."
            end = max(end, b)
        chosen = {s["id"] for s in selected}
        pairs = {s.get("call_id") for s in selected if s.get("call_id")}
        if any(s.get("call_id") in pairs and s["id"] not in chosen for s in sim.ctx):
            return "Error: range splits a tool call and its result."
        calls = {s.get("call_id") for s in selected if s["seg"] == "call"}
        outputs = {s.get("call_id") for s in selected if s["seg"] == "out"}
        if calls != outputs:
            return "Error: range contains an unfinished tool call."
        text = f"[AgentFold steps {lo}-{hi}]\n{args['summary']}"
        new = sim.summarize_segment(selected, text=text, billed=False, model_written=True,
                                    role="assistant", fold_span=(lo, hi))
        return f"Folded steps {lo}-{hi}." if new is not None else "Error: nothing was folded."

    def step(self, sim, r):
        if not hasattr(self, "done"):
            self.reset(sim)
        calls = {s.get("call_id"): s for s in sim.ctx if s["seg"] == "call"}
        for output in list(sim.ctx):
            if output["seg"] != "out" or output["id"] in self.done:
                continue
            self.done.add(output["id"])
            call = calls.get(output.get("call_id"))
            if not call or (call.get("name") or "").rsplit("__", 1)[-1].rsplit(".", 1)[-1] != "fold_context":
                continue
            try:
                args = json.loads(call.get("args") or "{}")
            except (TypeError, ValueError):
                args = None
            reply, error = self.call_tool("fold_context", args)
            if not error:
                reply = self._fold(sim, call, args)
            sim.keep_text(output, reply)
