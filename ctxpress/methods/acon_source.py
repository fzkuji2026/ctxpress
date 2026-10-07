"""ACON public AppWorld guidelines and optimizer protocol on the shared host.

Reference: microsoft/acon@d63f9ae18959dc7215ff62899c94c5e8c56847ae.
Ports prompt arguments, marker parsing and strict threshold semantics. Host
history serialization and tokenizer differ; guideline optimization, distillation
and trained compressor weights are not reproduced. Legacy ACON is unchanged.
"""
from __future__ import annotations

import json
import re

from ctxpress.methods.base import Method
from ctxpress.methods import acon_prompts


def parse_output(response, keyword):
    if keyword in response:
        return response[response.index(keyword) + len(keyword):].strip()
    return response


def history_args(task, history, previous=None):
    return dict(task=task, history=history,
                prev_summary=f"<PREVIOUS_SUMMARY>\n{previous}\n</PREVIOUS_SUMMARY>" if previous else "")


def observation_args(task, observation, history, extra=None):
    return dict(dict(task=task, observation=observation, history=history), **(extra or {}))


def render(template, args):
    # The three pinned author templates only use variable substitution. Do not
    # interpret braces introduced by user content or support executable Jinja.
    return re.sub(r"{{\s*(\w+)\s*}}", lambda m: str(args[m[1]]), template.removesuffix("\n"))


def needs_summary(text, threshold, count_tokens, previous=None):
    if threshold == -1:
        return True
    return count_tokens(f"{previous}\n{text}" if previous else text) > threshold


class ACONSource(Method):
    source = "arXiv 2510.00615"
    name = "ACON (published AppWorld guidelines)"
    requires_summary = True
    framework = dict(L1="作者指南压缩观察", L2="无", L3="作者指南压缩历史，保留最近轮次",
                     cross="固定指令和用户消息保留", memory="无", decider="作者阈值与提示词协议")

    def __init__(self, t_hist=4096, t_obs=256, keep_turns=1, summary_model=None):
        if any(type(t) is not int or t < -1 for t in (t_hist, t_obs)):
            raise ValueError("thresholds must be non-negative integers or -1 (always)")
        if type(keep_turns) is not int or keep_turns < 0:
            raise ValueError("keep_turns must be a non-negative integer")
        self.t_hist, self.t_obs, self.keep_turns, self.summary_model = t_hist, t_obs, keep_turns, summary_model

    @staticmethod
    def _task(sim):
        return "\n".join(s.get("text", "") for s in sim.sent() if s.get("role") == "user")

    @staticmethod
    def _history(sim, items):
        return json.dumps([dict(role=s.get("role", s["seg"]), content=sim.current_text(s) or s.get("text", ""))
                           for s in items], ensure_ascii=False)

    def _ask(self, sim, template, args, marker, purpose):
        answer = sim.model_text(render(acon_prompts.SYSTEM, {}), render(template, args), purpose=purpose, model=self.summary_model)
        if answer is None:
            return None
        result = parse_output(answer.strip(), marker)
        if not result.strip():
            sim.M["acon_empty_summary"] += 1
            return None
        return result

    def on_ingest(self, sim, item):
        if item["seg"] != "out" or item.get("protected") or item.get("has_media"):
            return
        text = sim.current_text(item) or ""
        count = getattr(sim, "tokens", lambda text: max(1, len(text) // 4) if text else 0)
        if not needs_summary(text, self.t_obs, count):
            return
        history = self._history(sim, [s for s in sim.ctx if s is not item])
        result = self._ask(sim, acon_prompts.OBSERVATION, observation_args(self._task(sim), text, history),
                           "# Refined Observation", "observation")
        if result is not None and sim.keep_text(item, result):
            sim.M["op_observation_summary"] += 1

    def step(self, sim, r):
        summaries = [s for s in sim.ctx if s.get("acon_summary")]
        previous = "\n".join(s.get("text", "") for s in summaries) or None
        turns = sorted({s.get("turn", 0) for s in sim.ctx if s["seg"] == "out"})
        recent = set(turns[-self.keep_turns:]) if self.keep_turns else set()
        selected = [s for s in sim.ctx if not s.get("protected") and not s.get("has_media") and
                    s.get("role") not in ("user", "system", "developer") and not s.get("acon_summary") and
                    s.get("turn", 0) not in recent]
        # Complete pairs only; never consume a pending call or a media companion.
        ids = {s["id"] for s in selected}
        crossing = {s.get("call_id") for s in sim.ctx if s["id"] not in ids and s.get("call_id")}
        calls = {s.get("call_id") for s in selected if s["seg"] == "call"}
        outputs = {s.get("call_id") for s in selected if s["seg"] == "out"}
        blocked = crossing | (calls ^ outputs)
        selected = [s for s in selected if not s.get("call_id") or s["call_id"] not in blocked]
        if not selected:
            return
        history = self._history(sim, selected)
        count = getattr(sim, "tokens", lambda text: max(1, len(text) // 4) if text else 0)
        if not needs_summary(history, self.t_hist, count, previous):
            return
        result = self._ask(sim, acon_prompts.HISTORY, history_args(self._task(sim), history, previous),
                           "# History Summary", "history")
        if result is not None:
            sim.summarize_segment(summaries + selected, text=result, billed=False, model_written=True,
                                  role="assistant", acon_summary=True)
