"""Harness-native and classic methods: no compaction, Codex auto-compact, Claude Code, sliding window."""
from __future__ import annotations
from ctxpress.methods.base import Method, K, replaceable


class NoCompaction(Method):
    """Keep history until the native window cap; the proxy rejects compaction.

    A large total-scope threshold alone is clamped by Codex to 90% of its
    window. Body-after-prefix avoids that soft clamp without changing the
    model catalog or context window. The independent hard cap still fires;
    allow_native_compaction makes that a local terminal error, not a summary.
    """
    name = "不压缩"
    unbounded = True
    allow_native_compaction = False
    codex_config = {
        'model_auto_compact_token_limit': 2147483647,
        'model_auto_compact_token_limit_scope': 'body_after_prefix',
        'model_post_turn_compact_threshold_percent': 0,
        'compact_prompt': 'CTXPRESS_NO_COMPACTION_STOP_V1',
    }
    framework = dict(L1="无", L2="无", L3="无（窗口满了就失败）", cross="无", memory="无", decider="无")


class HostDefault(Method):
    """The host exactly as shipped: no request is rewritten and none of its native compaction settings (threshold,
    scope, on/off) is overridden. The proxy only records usage. This is the baseline every method is compared with
    on a pinned host version."""
    name = "宿主默认"
    source = "宿主（Codex / Claude Code）出厂设置"
    host_compaction = "default"               # evaluation launchers pass no compaction override
    framework = dict(L1="无", L2="无", L3="宿主原生压缩（默认阈值）", cross="无", memory="无", decider="宿主默认")


class CodexAutoCompact(Method):
    """Codex CLI auto-compact: when the context exceeds T, replace everything after the fixed prefix with one
    summary. T = 230k is the standard against which every method is checked."""
    source = "Codex CLI"

    def __init__(self, t=230 * K, size=None, keep_user=False):
        self.t, self.size, self.keep_user = t, size, keep_user
        self.codex_config = {"model_auto_compact_token_limit": t}
        self.name = f"Codex auto-compact（{t // K}k）"
        self.framework = dict(L1="无", L2="无", L3=f"超过 {t // K}k 整体摘要", cross="无", memory="无", decider="固定阈值")

    def step(self, sim, r):
        if sim.size() > self.t:
            sim.summarize(size=self.size, keep_user=self.keep_user)


class ClaudeCode(Method):
    """Claude Code: above T, clear old tool results (all but the newest K) first; if still above F*T, /compact."""
    source = "Claude Code"

    def __init__(self, t=64 * K, k=5, f=0.6, size=None):
        self.t, self.k, self.f, self.size = t, k, f, size
        self.name = f"Claude Code（{t // K}k）"
        self.framework = dict(L1=f"超过 {t // K}k 时，最近 {k} 条之外的工具输出换成占位符", L2="无",
                              L3=f"清理后仍超过 {f:g}×阈值 再整体摘要", cross="无", memory="无（API 的 context editing 可另配 memory tool）",
                              decider="固定阈值")

    def step(self, sim, r):
        if sim.size() > self.t:
            outs = sim.outputs()
            sim.to_placeholder([s for s in outs[:-self.k] if replaceable(s)], form="placeholder")
            if sim.size() > self.f * self.t:
                sim.summarize(size=self.size)


class SlidingWindow(Method):
    """Above T, delete the oldest content (calls, outputs, text) until the context fits."""
    source = "classic"

    def __init__(self, t=64 * K):
        self.t = t
        self.name = f"Sliding window（{t // K}k）"
        self.framework = dict(L1="无", L2="无", L3=f"超过 {t // K}k 从最旧的开始删", cross="无", memory="无", decider="固定阈值")

    def step(self, sim, r):
        while sim.size() > self.t and len(sim.ctx) > 1:
            s = next((s for s in sim.ctx if not s.get("protected")), None)
            if s is None:
                break
            sim.delete([s])
