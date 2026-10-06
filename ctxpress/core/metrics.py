"""Performance proxies and the hard constraint.

Requirement (preliminary study §8.7): on every session a method must not be worse than the standard
compaction (Codex default auto-compact at 230k); otherwise it should not compress beyond it. The replay
can only measure proxies; three definitions, from strict to lenient:
  strict    every expected silent miss + requirement-doc gaps
  harm      requirement-doc gaps and misses count 1; code / other misses count harm_other (0.3, §3)
  measured  as harm, but misses after a summary count 0 (§3: 9/40 vs 7/39; §5: 7/20 vs 7/20)
"""
from __future__ import annotations

KINDS = ("strict", "harm", "measured")


def perf(r, kind, harm_other=0.3):
    """Lower is better."""
    g = r.get
    if kind == "strict":
        return g("silent", 0) + g("spec_gap", 0)
    if kind == "harm":
        return g("spec_gap", 0) + g("silent_spec", 0) + harm_other * (g("silent_code", 0) + g("silent_other", 0))
    if kind == "measured":
        return g("spec_gap", 0) + g("silent_ph_spec", 0) + harm_other * (g("silent_ph_code", 0) + g("silent_ph_other", 0))
    raise ValueError(kind)


def ok(r, ref, kind, eps=0.05, harm_other=0.3):
    return perf(r, kind, harm_other) <= perf(ref, kind, harm_other) + eps


def score(r, ref, base, kind, eps=0.05, harm_other=0.3, penalty=10.0):
    """One number per session for 'cheaper, but never worse than the standard': relative cost if the
    constraint holds; otherwise relative cost plus a large penalty per unit of extra harm."""
    rel = r["cost"] / base["cost"]
    d = perf(r, kind, harm_other) - perf(ref, kind, harm_other)
    return rel if d <= eps else rel + penalty * (1 + d)


HARM = {
    "harm": {"spec": 1.0, "code": 0.3, "other": 0.3},
    "measured": {"placeholder": {"spec": 1.0, "code": 0.3, "other": 0.3}, "summary": {"spec": 1.0, "code": 0.0, "other": 0.0}},
}
