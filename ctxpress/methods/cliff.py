"""CliffCompaction (Nguyen et al., arXiv 2609.26779) on the ctxpress framework.

Follows the authors' code (github.com/nguyenvuthientrang/cliffcompaction: cliff.py, engine.py,
dialects/openai_responses.py):
  * size = chars / 4 of the request as the host sends it (images priced by dimensions: ctxpress.hostsize);
    above `t` (default 200k) the history becomes [head] + [one mechanical summary] + [last `keep_recent` turns];
  * the summary, by content class: tool results kept verbatim iff <= 500 chars (else dropped), tool calls as
    one-line signatures (arguments cut at 150 chars), assistant text and reasoning summaries in full, user text
    (cap 20k chars);
  * the head is everything before the first model output; a turn starts at a model item whose predecessor is not
    one (so [reasoning, call] stay together with their outputs);
  * crossings are replayed in order over the items after the current summary, each re-compaction dropping the
    previous summary;
  * still above `t`: escalate (rung 1: keep_recent = 1; rung 2: also assistant text capped at 300 chars and no
    reasoning), then send anyway.
The original keeps its substitution in a store keyed by the hash chain of the original prefix; here the summary
stays in the context, which is the same thing for an append-only history. The shared Responses proxy offers
HTTP 400 length rejections to the reactive ladder; error recognition is more conservative than the authors'
broad raw-body matching. Strict/shadow modes and other dialects are not provided here.
`repro/cliff_compare.py` checks recorded proactive requests; `cliff_reactive_compare.py` checks synthetic ladders.
"""
from __future__ import annotations
import dataclasses, json, re
from ctxpress.methods.base import Method
from ctxpress.core.engine import is_model

SUMMARY_HEADER = "The following is a summary of your previous actions (long observations omitted):"
_TASK_NOTIFICATION_RE = re.compile(r"<task-notification>.*?</task-notification>\s*", re.DOTALL)
K = 1000


@dataclasses.dataclass
class Knobs:
    keep_recent: int = 3
    thought_max_chars: int = 0
    cmd_max_chars: int = 150
    result_max_chars: int = 500
    human_max_chars: int = 20_000
    keep_thinking: bool = True
    thinking_max_chars: int = 0


def truncate(text, n):
    text = text or ""
    return text[:n] + "..." if n and n > 0 and len(text) > n else text


def is_summary(x):
    return x.get("cliff") or (x["seg"] == "msg" and x.get("role") == "user" and (x.get("text") or "").startswith(SUMMARY_HEADER))


def summarize_item(x, k):
    """The summary lines for one context item (the original's summarize_message, per item type)."""
    if x.get("cliff") or x["seg"] == "summary":
        return []
    h = x.get("htype")
    if x["seg"] == "msg":
        role, text = x.get("role", ""), (x.get("text") or "").strip()
        if not text:
            return []
        if role == "assistant":
            return [f"assistant: {truncate(text, k.thought_max_chars)}"]
        if text.startswith(SUMMARY_HEADER):
            return []
        if role == "user":
            text = _TASK_NOTIFICATION_RE.sub("", text).strip()
            return [f"user: {truncate(text, k.human_max_chars)}"] if text else []
        return [f"{role}: {truncate(text, k.human_max_chars)}"]
    if h == "reasoning" or (h is None and x["seg"] == "reason"):
        text = (x.get("text") or "").strip()
        return [f"thinking: {truncate(text, k.thinking_max_chars)}"] if k.keep_thinking and text else []
    if h == "function_call_output" or (h is None and x["seg"] == "out"):
        out = (x.get("text") or "").strip()
        return [f"result: {out}"] if out and len(out) <= k.result_max_chars else []
    if (h or "").endswith("_call") or (h is None and x["seg"] == "call"):
        # The author's Responses dialect reads only the wire `arguments` field,
        # even for freeform calls. Keep that signature while other methods may
        # account for the full custom input through canonical `args`.
        args = x.get("wire_arguments", x.get("args", x.get("text", "") if h is None else ""))
        if not isinstance(args, str):
            import json
            args = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return [f"[{x.get('name') or h or 'call'}] {truncate(args, k.cmd_max_chars)}"]
    return []                    # custom tool outputs, compaction items, ...: the original folds nothing for them


def group_turns(items):
    turns, cur, prev = [], None, False
    for x in items:
        m = is_model(x)
        if m and not prev:
            if cur is not None:
                turns.append(cur)
            cur = [x]
        elif cur is None:
            cur = [x]
        else:
            cur.append(x)
        prev = m
    if cur is not None:
        turns.append(cur)
    return turns


class CliffCompaction(Method):
    """CliffCompaction (2609.26779): above T (chars / 4 of the request), the history becomes the head, one
    mechanical summary and the last `keep_recent` turns."""
    source = "arXiv 2609.26779"
    max_overflow_retries = 4

    def __init__(self, t=200 * K, keep_recent=3, result_max_chars=500, cmd_max_chars=150):
        self.t = t
        self.knobs = Knobs(keep_recent=keep_recent, result_max_chars=result_max_chars, cmd_max_chars=cmd_max_chars)
        self.name = f"CliffCompaction（{t // K}k）"
        self.framework = dict(L1="无", L2="无",
                              L3=f"超过 {t // K}k：开头 + 机械摘要（≤{result_max_chars} 字符的工具输出原文保留，其余删除；调用只留一行签名）+ 最近 {keep_recent} 轮",
                              cross="无（被删的输出要重新执行调用）", memory="无", decider="固定阈值")

    # ------------------------------------------------------------------ per request
    def reset(self, sim):
        self._rung, self._compacted = 0, False

    def step(self, sim, r):
        self.reset(sim)
        if sim.request_chars() // 4 <= self.t:
            return
        self._compacted = self._chain(sim, self.knobs)
        for rung in (1, 2):
            if sim.request_chars() // 4 <= self.t:
                break
            kw = {"keep_recent": 1}
            if rung >= 2:
                cap = self.knobs.thought_max_chars
                kw.update(thought_max_chars=300 if cap <= 0 else min(cap, 300), keep_thinking=False)
            if self._chain(sim, dataclasses.replace(self.knobs, **kw), force=True):
                self._compacted, self._rung = True, rung

    def on_overflow(self, sim, request):
        if not self._compacted and self._rung == 0:
            if self._chain(sim, self.knobs, force=True):
                self._compacted = True
                return True
        while self._rung < 3:
            self._rung += 1
            if self._rung < 3:
                knobs = dataclasses.replace(self.knobs, keep_recent=1)
                if self._rung == 2:
                    cap = self.knobs.thought_max_chars
                    knobs = dataclasses.replace(knobs, thought_max_chars=300 if cap <= 0 else min(cap, 300),
                                                keep_thinking=False)
                if self._chain(sim, knobs, force=True):
                    self._compacted = True
                    return True
            elif self._truncate_summary(sim, request):
                return True
        return False

    def _truncate_summary(self, sim, request):
        """Author's last reactive rung: retain the newest complete summary parts."""
        if not self._compacted:
            return False
        summary = next((s for s in sim.ctx if s.get("cliff")), None)
        if summary is None or summary.get("protected"):
            return False
        text = summary.get("text", "")
        if not text.startswith(SUMMARY_HEADER):
            return False
        # The host has already rendered this exact generated message. Removing
        # one array element costs its serialized length and a comma separator.
        message = sim.host.message(text)
        fixed = len(json.dumps(request, ensure_ascii=False)) - len(json.dumps(message, ensure_ascii=False))
        fixed -= 2 if len(request.get("input", [])) > 1 else 0
        budget = self.t * 4 - fixed - len(SUMMARY_HEADER) - 64
        parts = text[len(SUMMARY_HEADER):].strip().split("\n\n---\n\n")
        kept, used = [], 0
        for part in reversed(parts):
            if used + len(part) > max(budget, 0):
                break
            kept.append(part)
            used += len(part) + 9
        text_new = SUMMARY_HEADER + "\n\n" + "\n\n---\n\n".join(reversed(kept)) if kept else SUMMARY_HEADER
        if len(text_new) >= len(text):
            return False
        summary.update(text=text_new, size=sim.tokens(text_new), chars=sim.message_chars(text_new))
        sim.M["op_summary_trimmed"] += 1
        return True

    def _chars(self, sim, x):
        return sim.message_chars(x["text"]) if x.get("virtual") else sim.host_chars(x)

    def _compact(self, working, k):
        first = next((i for i, x in enumerate(working) if is_model(x)), None)
        if first is None:
            return None
        head = first
        while head > 0 and is_summary(working[head - 1]):
            head -= 1
        turns = group_turns(working[head:])
        keep = max(0, k.keep_recent)
        if len(turns) <= keep:
            return None
        old = [x for t in turns[:len(turns) - keep] for x in t]
        gone = [x for x in old if not x.get("protected")]
        if not gone:
            return None
        parts = [p for x in gone for p in summarize_item(x, k)]
        text = SUMMARY_HEADER + "\n\n" + "\n\n---\n\n".join(parts) if parts else SUMMARY_HEADER
        kept = [x for x in old if x.get("protected")] + [x for t in turns[len(turns) - keep:] for x in t]
        summary = dict(seg="summary", virtual=True, cliff=True, text=text, members=gone)
        new = [*working[:head], summary, *kept]
        if len(new) >= len(working):
            return None
        return dict(new=new, head=head, summary=summary, cut=len(working) - len(kept))

    def _chain(self, sim, k, force=False):
        """Feed the items after the current summary in order, compacting whenever the running size crosses T
        (the original's _compact_chain). Changes the context only if every step succeeds, as the original."""
        items = sim.sent()
        n_orig = sim.n_input if getattr(sim, "n_input", None) is not None else len(items)
        fixed = sim.fixed_chars if sim.fixed_chars is not None else sim.prefix * 4
        limit = self.t * 4
        at = next((i for i, x in enumerate(items) if x.get("cliff")), None)
        if at is not None:
            working, head, cut, have = list(items[:at + 1]), at, n_orig - len(items) + at + 1, True
            feed = items[at + 1:]
        else:
            working, head, cut, have, feed = [], 0, 0, False, items
        size = fixed + sum(self._chars(sim, x) + 2 for x in working)
        ops = []

        def apply(res):
            nonlocal working, size, head, cut, have
            if have:
                if res["cut"] < head + 1:
                    return False
                new_cut = cut + (res["cut"] - (head + 1))
            else:
                new_cut = res["cut"]
            if not 1 <= new_cut <= n_orig:
                return False
            working, head, cut, have = res["new"], res["head"], new_cut, True
            size = fixed + sum(self._chars(sim, x) + 2 for x in working)
            ops.append(res["summary"])
            return True

        for x in feed:
            working.append(x)
            size += self._chars(sim, x) + 2
            if size > limit:
                res = self._compact(working, k)
                if res is None:
                    continue
                if not apply(res):
                    return False                  # no staged operations were committed
        if not ops and force:
            res = self._compact(working, k)
            if res is not None:
                apply(res)
        if not ops:
            return False
        real = {}
        for v in ops:                             # commit: each summary replaces its members in the context
            members = [real.get(id(m), m) for m in v["members"]]
            new = sim.summarize_segment(members, text=v["text"], billed=False, cliff=True)
            new["chars"] = sim.message_chars(v["text"])
            real[id(v)] = new
        return True
