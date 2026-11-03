"""Retention rules of a joint tool policy: how long a tool output stays whole once it is in the context.

A joint policy (ctxpress.evolve.policy) decides both halves of an observation's life: view() its entry form (served
by ReadLess) and, optionally,

    retain(info, ctx) -> str | None      the shorter text this output should become now (None: keep it)
    RETAIN = {switch: value}             overrides of our method's own retention switches (t, keep_recent, ...),
                                         clamped to RETAIN_BOUNDS by ctxpress.evolve.policy.retain_overrides

info = dict(cmd, text, tokens, age (requests since it entered), later (agent-written text since, capped),
refetched (the same command ran again later)). This method sits after infinite-context in a Composed method and
applies retain()'s shrinks at a point where they cost the least cache:

  free      the output lies after the first position another method rewrote this step (our method's commit
            already invalidates the cache from there on), so shrinking it costs no extra rebuild;
  paid      otherwise the shrinks of this step are applied together only when the saving pays for the rebuild:
            removed tokens x expected remaining requests x cached price > tokens after the first shrink x
            (uncached - cached price). Prices of gpt-6.1-sol: cached / uncached = 0.05.

With no retain() in the policy the method does nothing, so a tool-only policy behaves as before.
"""
from __future__ import annotations
from ctxpress.core import tokens
from ctxpress.core.trace import shell_command, unescape
from ctxpress.methods.base import Method
from ctxpress.methods.readless import load_policy, norm_cmd

CACHED_RATIO = 0.05
LATER_CHARS = 20000


class RetainHook(Method):
    name = "Retention rules"
    framework = dict(L1="无", L2="工具输出用完后按策略缩短（缓存失效成本门槛；与上下文方法的提交同时做时不额外付费）",
                     L3="无", cross="无", memory="无", decider="联合策略的 retain()")
    source = "本文"

    def __init__(self, policy, horizon=60, min_tokens=400, fresh_only=False):
        self.policy_spec = policy
        load_policy(policy)
        self.horizon, self.min_tokens, self.fresh_only = horizon, min_tokens, fresh_only
        self.reset(None)

    def reset(self, sim):
        self.prev = None                 # (id, form, kept) of every item at the end of the last step
        self.started = False
        self.done = set()                # ids already shrunk by retain()
        self.skip = set()                # ids in the history at the first request (a resumed session, a fork)

    def _signature(self, sim):
        return [(x["id"], x.get("form", "full"), x.get("kept")) for x in sim.ctx]

    def _free_from(self, sig):
        """First position another method changed since the last step (len(prev) if it only appended)."""
        if self.prev is None:
            return len(sig)
        for i, (a, b) in enumerate(zip(self.prev, sig)):
            if a != b:
                return i
        return len(self.prev) if len(sig) >= len(self.prev) else len(sig)

    def step(self, sim, r):
        policy = load_policy(self.policy_spec)
        fn = getattr(policy, "retain", None)
        if not self.started:
            self.started = True
            if self.fresh_only:
                self.skip = {x["id"] for x in sim.ctx}
        if fn is None:
            self.prev = self._signature(sim)
            return
        sig = self._signature(sim)
        free = self._free_from(sig)
        calls = [(i, unescape(x.get("text") or "")) for i, x in enumerate(sim.ctx) if x.get("seg") == "call"]
        proposals = []
        for i, item in enumerate(sim.ctx):
            if (item.get("seg") != "out" or item.get("protected") or item.get("has_media") or item["id"] in self.done
                    or item["id"] in self.skip or item.get("form", "full") not in ("full", "structured")):
                continue
            text = item.get("kept_text") if item.get("form") == "structured" else (item.get("text") or "")
            n = tokens.count(text or "")
            if n < self.min_tokens:
                continue
            cmd = shell_command(unescape(item.get("call_text") or ""))
            later = [t for j, t in calls if j > i]
            info = dict(cmd=cmd, text=text, tokens=n, age=max(0, sim.r - item.get("born", sim.r)),
                        later="\n".join(later)[-LATER_CHARS:],
                        refetched=bool(cmd) and any(norm_cmd(cmd) in norm_cmd(t) for t in later))
            try:
                new = fn(info, dict(tokens=tokens.count, request=sim.r))
            except Exception:
                sim.M["retain_error"] += 1
                continue
            if isinstance(new, str) and tokens.count(new) < n:
                proposals.append((i, item, new, n - tokens.count(new)))
        if proposals:
            paid = [p for p in proposals if p[0] < free]
            apply = [p for p in proposals if p[0] >= free]
            if paid:
                first = min(p[0] for p in paid)
                saved = sum(p[3] for p in paid)
                suffix = sum(sim.seg_size(x) for x in sim.ctx[first:]) - saved
                remaining = max(5, self.horizon - sim.r)
                if saved * remaining * CACHED_RATIO > suffix * (1 - CACHED_RATIO):
                    apply += paid
                    sim.M["retain_paid_commits"] += 1
                else:
                    sim.M["retain_deferred"] += len(paid)
            for i, item, new, d in apply:
                if sim.keep_text(item, new, form="structured"):
                    self.done.add(item["id"])
                    sim.M["retain_shrunk"] += 1
                    sim.M["retain_saved_tokens"] += d
                    if i >= free:
                        sim.M["retain_free"] += 1
        self.prev = self._signature(sim)
