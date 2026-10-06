"""Every number the simulator uses, with where it comes from.

Each parameter is a `P(value, source, note)`. `source` is one of
  measured     measured in our own preliminary study (section of the preliminary study given in `note`)
  fitted       fitted on our traces by a script in this repository
  literature   taken from a paper (named in `note`)
  price        provider price list
  assumed      not measured yet; a conservative guess that experiments should replace
`Params` groups them; `Params.override({...})` returns a copy with some values changed (used by configs and
sensitivity sweeps). `Params.table()` lists everything with provenance (python -m ctxpress params).
"""
from __future__ import annotations
import copy
from dataclasses import dataclass, field


@dataclass
class P:
    value: object
    source: str = "assumed"
    note: str = ""


def _q():
    """Probability that the agent re-fetches content it needs, by (form the content is in, content type).
    '*' is the default for a form. Forms: see ctxpress.context.FORMS."""
    return {
        ("placeholder", "spec"): P(1 / 8, "measured", "§5: 1/8 runs re-read the requirements file after it became a placeholder"),
        ("placeholder", "*"): P(0.87, "measured", "§3: 47/54 re-read the file to be edited when its output was gone"),
        ("gone", "spec"): P(1 / 8, "measured", "same as placeholder (§5)"),
        ("gone", "*"): P(0.87, "measured", "same as placeholder (§3)"),
        ("summary", "spec"): P(1.0, "measured", "§5: 8/8 re-read the requirements file after Codex compaction"),
        ("summary", "*"): P(0.95, "measured", "§3: 38/40 re-read after Codex compaction"),
        ("segsummary", "spec"): P(1 / 8, "assumed", "segment summary sits mid-context like a placeholder; conservative"),
        ("segsummary", "*"): P(0.95, "assumed", "taken equal to a whole-session summary"),
        # truncated / structured: the kept part may already cover the need (see coverage); if not, the
        # truncation marker is a visible signal, and §3 finding 10 shows a signal + missing info -> re-read
        ("truncated", "spec"): P(1 / 8, "assumed", "the agent does not notice a missing requirement (§5)"),
        ("truncated", "*"): P(0.87, "assumed", "signal present as with a placeholder (§3 finding 10)"),
        ("structured", "spec"): P(1 / 8, "assumed", "as truncated"),
        ("structured", "*"): P(0.87, "assumed", "as truncated"),
        # dedicated memory: placeholder carries an id ('memid') or an id plus a label of what it was ('memlabel')
        ("memid", "spec"): P(1 / 8, "assumed", "an id alone does not tell the agent it lacks the requirements"),
        ("memid", "*"): P(0.87, "assumed", "as placeholder; retrieval instead of re-execution"),
        ("memlabel", "spec"): P(0.5, "assumed", "label 'requirements doc' may prompt retrieval; to be measured"),
        ("memlabel", "*"): P(0.9, "assumed", "to be measured"),
    }


def _coverage():
    """When content is truncated or structured and a later call needs it: the probability the kept part is
    enough. Used only when the trace has no text (with text, coverage is checked on the actual kept lines)."""
    return {
        ("truncated", "spec"): P(0.5, "assumed", ""),
        ("truncated", "*"): P(0.5, "assumed", ""),
        ("structured", "spec"): P(0.2, "assumed", "a requirements doc has no structure to keep"),
        ("structured", "*"): P(0.4, "assumed", ""),
    }


@dataclass
class Params:
    # ---------------- billing (relative to 1 uncached input token)
    cached: P = field(default_factory=lambda: P(0.1, "price", "OpenAI and Anthropic: cached input = 10% of input"))
    write: P = field(default_factory=lambda: P(1.0, "price", "price of an input token not read from cache; Anthropic 5-min cache write = 1.25, 1-h = 2.0"))
    out: P = field(default_factory=lambda: P(8.0, "price", "output / input price ratio (GPT-5.x: 10 / 1.25)"))
    cache_mode: P = field(default_factory=lambda: P("lcp", "assumed", "lcp: longest common prefix with the previous request; checkpoint: only up to the end of an earlier request; §8.3 shows real behaviour lies between"))
    cache_ttl: P = field(default_factory=lambda: P(None, "price", "seconds a cache entry lives (None = forever; Anthropic 300 or 3600). Uses the trace's timestamps"))
    # ---------------- context
    window: P = field(default_factory=lambda: P(230000, "measured", "Codex auto-compact limit for the models in our traces"))
    summary_tokens: P = field(default_factory=lambda: P(8000, "measured", "typical Codex compaction summary"))
    placeholder_tokens: P = field(default_factory=lambda: P(20, "assumed", "'[removed: <call>]'"))
    label_tokens: P = field(default_factory=lambda: P(15, "assumed", "extra tokens when a placeholder names what it was and its memory id"))
    segsummary_ratio: P = field(default_factory=lambda: P(0.05, "assumed", "segment summary length / segment length"))
    segsummary_min: P = field(default_factory=lambda: P(200, "assumed", ""))
    bill_summary_call: P = field(default_factory=lambda: P(True, "measured", "the compaction request reads the whole context; Codex does not report it in token_count"))
    # ---------------- recovery and misses
    q: dict = field(default_factory=_q)
    coverage: dict = field(default_factory=_coverage)
    coverage_threshold: P = field(default_factory=lambda: P(0.9, "assumed", "fraction of a patch's anchor lines that must still be in context for a truncated read to count as enough"))
    spec_recovery: P = field(default_factory=lambda: P(True, "measured", "a requirements doc the agent re-reads (probability q) is back in context afterwards, so the gap counts once; False reproduces §8 (gap counted at every edit, re-read not billed). §5: 20/20 re-read after one compaction; recompaction-20261003: 19/20 after three"))
    dedupe_recovery: P = field(default_factory=lambda: P(True, "measured", "a re-fetched search result or requirements doc replaces the original as what later calls need, so it is not fetched again while the copy is in context; False reproduces §8, which re-fetched the same search result for every file it listed"))
    extra_out: P = field(default_factory=lambda: P(150, "assumed", "output tokens of an extra (recovery / re-exploration) request"))
    recompress_retention: P = field(default_factory=lambda: P(0.975, "measured", "content passed through k summaries is re-fetched with q * r^(k-1). recompaction-20261003: after 3 Codex compactions the agent still re-read the requirements 19/20 (vs 20/20 after one), r = (19/20)^(1/2); recall of the identifiers themselves fell 0.37 -> 0.30 (about 0.9 per round). 1.0 reproduces §8"))
    # ---------------- re-exploration
    w_warm: P = field(default_factory=lambda: P(10, "fitted", "'in active use' = used within the last 10 requests"))
    e_warm: P = field(default_factory=lambda: P(0.373, "fitted", "§4: extra calls per removed tool output in active use (calibrate_e.py)"))
    e_cold: P = field(default_factory=lambda: P(0.0154, "fitted", "§4: extra calls per removed older tool output"))
    # ---------------- performance proxies
    harm_other: P = field(default_factory=lambda: P(0.3, "measured", "§3: extra first-edit failure when a removed file was edited without re-reading (2/6 vs 1/31)"))
    eps: P = field(default_factory=lambda: P(0.05, "assumed", "tolerance of the 'not worse than the standard compaction' check, in expected misses"))
    # ---------------- decider overhead
    llm_score_tokens: P = field(default_factory=lambda: P(400, "assumed", "tokens an LLM reads per item to estimate its remaining value (TokenPilot-style)"))
    small_model_price: P = field(default_factory=lambda: P(0.02, "assumed", "price of a small scoring model per token relative to the main model's uncached input"))
    # ---------------- latency (secondary metric)
    latency_extra_request: P = field(default_factory=lambda: P(None, "measured", "seconds per extra model request; None = median model time of the trace"))

    def get(self, name):
        v = getattr(self, name)
        return v.value if isinstance(v, P) else v

    def qv(self, form, kind):
        t = self.q
        return (t.get((form, kind)) or t[(form, "*")]).value

    def cov(self, form, kind):
        t = self.coverage
        return (t.get((form, kind)) or t[(form, "*")]).value

    def override(self, d=None, **kw):
        """Copy with values replaced. Keys are attribute names; q / coverage keys may be 'form/kind'."""
        new = copy.deepcopy(self)
        for k, v in {**(d or {}), **kw}.items():
            if k.startswith(("q/", "coverage/")):
                tab, form, kind = k.split("/")
                getattr(new, tab)[(form, kind)] = P(v, "override")
            else:
                cur = getattr(new, k)
                setattr(new, k, P(v, "override", cur.note) if isinstance(cur, P) else v)
        return new

    def table(self):
        rows = []
        for name, v in self.__dict__.items():
            if isinstance(v, P):
                rows.append((name, v.value, v.source, v.note))
            elif isinstance(v, dict):
                for (f, k), p in v.items():
                    rows.append((f"{name}/{f}/{k}", p.value, p.source, p.note))
        return rows


DEFAULT = Params()


def anthropic(ttl=300):
    """Anthropic-style billing: explicit cache with a write premium and a time to live."""
    return DEFAULT.override(write=1.25 if ttl <= 300 else 2.0, out=5.0, cache_ttl=ttl)
