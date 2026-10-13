"""infinite-context V5: one context-update rule R_t with every factor an explicit switch.

C_t = R_t(C_{t-1} + new items). The switches, grouped as in experiment.zh.html §8-9:

  trigger      when R_t acts: request size above `t` tokens (chars / 4 of the request as the host sends it), or
               every request (`always`).
  scope        what it may touch: everything before the last `keep_recent` turns; the head (instructions and
               the task, up to the first model output) is never touched; optionally user messages and spec reads
               stay in place (`protect_user`, `protect_spec`).
  transform    how each content class is rendered when it leaves the context: assistant text, user text and
               reasoning kept or capped; tool calls as one-line signatures; tool outputs kept when short, else
               dropped or replaced by an archive reference (`recall`).
  dialect      how host tool items are read: `wire` as the authors' Responses dialect does (only function_call
               outputs and the wire `arguments` field; Codex Code Mode's custom `exec` calls then leave an empty
               signature and their outputs vanish), or `full` (custom tool outputs and call inputs as well).
  writer       who writes the replacement: the mechanical renderer, a model (`llm`), or a model-written task
               state ahead of the mechanical lines (`hybrid`).
  previous     what happens to an earlier replacement: dropped (each compaction summarises only what came after
               it), or carried into the new one (`keep`).
  decider      `always` compacts at the trigger; `cost` compacts only when the modelled saving pays for the cache
               rebuild, the writer, expected recoveries and the quality cost of carrying context
               (`length_penalty`); above `hard_t` it always compacts.
  recall       originals archived and exposed through infinite_context_search / infinite_context_read.

Three layers sit on top of that rule (all off by default; Cliff is "layer 3 only, mechanical"):

  L1 entry     one tool output, once, when it enters: a long output keeps a structured part (test failures, search
               paths) or its head and tail plus its archive number. Decided once, so it never breaks the cache later.
  L2 segment   a group of outputs, when a piece of work closes (a user message, or a read -> edit -> test cycle;
               the engine's `segid`): the closed segments' long outputs become numbered placeholders in one batch.
  L3 topic     the whole history, also when the topic changes (a later user message, or the files in use change):
               the same summary as at the size trigger, run early because the old topic's detail is rarely needed.

Runtime signals (only past requests) steer the layers with one global set of parameters, all relative to `t`:
  phase        explore (searching / reading) or work (edits or tests among the last calls): work keeps more.
  harm         after a removal, the agent re-reading a removed path counts as harm; harm keeps more everywhere
               (larger L1 previews, more recent turns, sticky paths L2 leaves alone) and decays when a segment
               closes without one.
  min_growth   after a compaction, the next one waits until the request grew by this fraction of `t`.
  reuse        long- and short-term memory of reuse (ctxpress/methods/reuse_memory.py): a profile from finished
               sessions (pooled, per task / task type / language / ..., per phase) and this session's evidence,
               which weights those habits by how well each has predicted it. Every operator leaves alone the
               outputs most likely to be needed again (at least `reuse_min_lift` times the average chance), up to `reuse_keep_frac` of
               `t`, most likely first; their calls stay with them.

With the defaults (CLIFF) R_t is CliffCompaction (arXiv 2609.26779) request for request; repro/infinite_v5_cliff_compare.py
checks that on recorded sessions. Every other setting is a departure from Cliff along named switches, so a
difference in results can be traced to the switches that produced it.
"""
from __future__ import annotations
import dataclasses, json, re
from ctxpress.methods.cliff import (CliffCompaction, Knobs, SUMMARY_HEADER, _TASK_NOTIFICATION_RE, group_turns,
                                    is_summary, truncate)
from ctxpress.core.engine import is_model
from ctxpress.methods.reuse_memory import ReuseMemory

K = 1000
STATE_GUIDANCE = ("Write the working state a coding agent needs to continue after these steps are removed from its "
                  "context: goals, constraints and user requirements, decisions, what was verified, failures and their "
                  "causes, unresolved issues, and the next steps. Keep exact identifiers, paths, commands and error "
                  "messages. Do not invent progress. Plain text with short labelled sections.")
RECALL_NOTE = ("Removed or shortened tool outputs are archived. Their references look like [archived #ID] or "
               "'stored at #ID'; read one with infinite_context_read (id=ID) or find text with "
               "infinite_context_search.")
WRITERS = ("mechanical", "llm", "hybrid")
PREVIOUS = ("drop", "keep")
DECIDERS = ("always", "cost")
TRIGGERS = ("size", "always")
DIALECTS = ("wire", "full")
HOLDS = ("window", "value")


@dataclasses.dataclass
class Spec(Knobs):
    """Knobs carries Cliff's own fields (keep_recent, caps, thinking); the rest are the unified switches."""
    trigger: str = "size"
    protect_user: bool = False
    protect_spec: bool = False
    output_keep_chars: int = 500          # tool outputs up to this length stay verbatim in the replacement
    recall: bool = False                  # longer outputs become [archived #ID] references instead of vanishing
    tool_dialect: str = "wire"
    writer: str = "mechanical"
    writer_tokens: int = 4000             # target size of a model-written summary / state
    previous: str = "drop"
    decider: str = "always"
    hard_t: int = 0                       # cost decider: above this always compact (0 = never forced)
    horizon: int = 100                    # cost decider: expected remaining requests
    cached_price: float = 0.1             # cost unit: one uncached input token
    output_price: float = 8.0
    recovery_rate: float = 0.05           # chance a removed long output is needed again
    recovery_cost: float = 2000.0         # tokens of extra work per recovery (beyond re-reading)
    length_penalty: float = 0.0           # quality cost of carrying one token for one request, in token units
    entry: bool = False                   # L1
    entry_frac: float = 0.02              # L1: outputs above this share of t are shortened at entry
    segment: bool = False                 # L2
    segment_min_frac: float = 0.05        # L2: act only when the closed segments hold this share of t
    topic: bool = False                   # L3 early, on a topic change
    topic_min_frac: float = 0.3           # L3: only when the request is at least this share of t
    topic_shift: bool = True              # L3: a complete change of the files in use also counts as a new topic
    topic_gap: int = 12                   # L3: calls after a topic change before a file change counts again
    adaptive: bool = False                # phase and harm signals
    work_factor: float = 2.0              # work phase: L1 budget multiplier
    work_keep: int = 2                    # work phase: extra recent turns at L3
    harm_max: int = 3
    min_growth: float = 0.0               # hysteresis between compactions, share of t
    ops: object = None                    # explicit operator list (dicts or Op); None = built from the switches above
    host_t: int = 0                       # a `host` operator's limit in tokens; 0 = the host's own default
    reuse_profile: object = None          # reuse_memory.profile(...) dict; None = no reuse memory
    reuse_min_lift: float = 2.0           # protect outputs at least this many times as likely to be needed as average
    reuse_keep_frac: float = 0.1          # protected outputs together at most this share of t
    guard: bool = False                   # rules R1/R2 (framework page): nothing changes before the model has seen it,
                                          # nothing in use changes; "in use" = the model's later calls or text reuse
                                          # distinctive lines or identifiers of the output, or name its paths
    guard_oversize_frac: float = 0.2      # R1 exception: outputs above this share of t may be shortened on arrival
    guard_active: int = 10                # requests since the last reuse during which an output counts as in use
    guard_keep_frac: float = 0.3          # outputs held in use together at most this share of t (R6 wins)
    hold: str = "window"                  # window: held while reused within guard_active requests
                                          # value: held while P(reused within hold_horizon) x cost of getting it back
                                          # exceeds the cost of carrying it (framework page §4); P from this session's
                                          # own reuse by age since last use, with a decaying prior
    hold_horizon: int = 20                # value: requests ahead the decision looks
    hold_floor: int = 2                   # value: reused within this many requests = in use, always held (R2)
    refetch_work: int = 2000              # value: extra tokens of model work to get a removed output back
    supersede: bool = False               # outputs replaced by a newer one from the same source (same path read
                                          # again, same command run again) are never held and go at the next batch
    spec_keep_frac: float = 0.0           # resident requirements: the newest spec outputs (SRS, design docs), together
                                          # at most this share of t, stay verbatim through every operator (0 = off)

    def check(self):
        for name, allowed in (("trigger", TRIGGERS), ("writer", WRITERS), ("previous", PREVIOUS), ("decider", DECIDERS),
                              ("tool_dialect", DIALECTS)):
            if getattr(self, name) not in allowed:
                raise ValueError(f"{name} must be one of {allowed}")
        if self.keep_recent < 0 or self.output_keep_chars < 0 or self.writer_tokens < 1 or self.horizon < 1:
            raise ValueError("counts and sizes must be non-negative")
        if not (0 < self.entry_frac and 0 <= self.segment_min_frac and 0 <= self.topic_min_frac
                and self.work_factor >= 1 and self.work_keep >= 0 and self.harm_max >= 0 and self.min_growth >= 0
                and self.reuse_min_lift > 0 and 0 <= self.reuse_keep_frac <= 1
                and 0 < self.guard_oversize_frac and self.guard_active >= 0 and 0 <= self.guard_keep_frac <= 1
                and self.hold in HOLDS and self.hold_horizon >= 1 and self.hold_floor >= 0 and self.refetch_work >= 0
                and 0 <= self.spec_keep_frac <= 1):
            raise ValueError("layer parameters out of range")
        return self


@dataclasses.dataclass(frozen=True)
class Op:
    """One context operator: when it acts, what it turns content into, which content it touches, one relative size.

    when       entry (an output as it arrives) | segment_end (a piece of work closed) | topic (the topic changed)
               | size (the request crossed t)
    transform  shorten (structured part for tests and searches, else head and tail) | structure | truncate
               | placeholder (numbered, readable again) | summary (the history summary, writer per the spec)
               | host (size only: the host compacts by itself; for Codex 0.161 that keeps user messages within
               64k tokens and the server's compaction item, ctxpress/methods/codex_compact.py; `host_t` = its
               limit, 0 = the host default of 90% of the window)
    kinds      content kinds it may touch (read, spec, search, test, command, edit, other); empty = all
    frac       share of t: entry = largest output left alone; segment_end = least content worth one cache rebuild;
               topic = least request size worth compacting early; size = unused (the trigger is t)
    The layer count is not fixed: a configuration is any list of these, applied in order. New `when` events and
    transforms are added to WHEN / TRANSFORMS."""
    when: str
    transform: str
    kinds: tuple = ()
    frac: float = 0.0

    def touches(self, x):
        return not self.kinds or content_kind(x) in self.kinds

    def label(self):
        return f"{self.when}:{self.transform}" + (f"[{','.join(self.kinds)}]" if self.kinds else "") \
            + (f"@{self.frac:g}" if self.frac else "")

    def describe(self, t):
        what = {"shorten": "留结构化部分或头尾 + 编号", "structure": "结构化压缩 + 编号", "truncate": "头尾截断 + 编号",
                "placeholder": "换成编号占位符", "summary": "开头 + 摘要 + 最近几轮",
                "host": "交给宿主自己压缩（Codex：用户消息 ≤64k + 服务器写的压缩项）"}[self.transform]
        when = {"entry": "进入时", "segment_end": "一段工作结束时", "topic": "话题切换时",
                "size": f"超过 {t // K}k 时"}[self.when]
        if self.transform == "host":
            when = "超过宿主阈值时"
        return when + ("（" + "、".join(self.kinds) + "）" if self.kinds else "") + "：" + what


WHEN = ("entry", "segment_end", "topic", "size")
TRANSFORMS = {"entry": ("shorten", "structure", "truncate"), "segment_end": ("placeholder", "truncate", "structure"),
              "topic": ("summary",), "size": ("summary", "host")}
KINDS = ("read", "spec", "search", "test", "command", "edit", "other")


def content_kind(x):
    if x.get("test"):
        return "test"
    k = x.get("kind")
    return k if k in KINDS else "other"


def build_ops(spec):
    """The operator list: explicit `ops`, or the one the layer switches describe. Cliff is [size:summary]."""
    if spec.ops is not None:
        ops = [o if isinstance(o, Op) else Op(**{**o, "kinds": tuple(o.get("kinds", ()))}) for o in spec.ops]
    else:
        ops = []
        if spec.entry:
            ops.append(Op("entry", "shorten", frac=spec.entry_frac))
        if spec.segment:
            ops.append(Op("segment_end", "placeholder", frac=spec.segment_min_frac))
        if spec.topic:
            ops.append(Op("topic", "summary", frac=spec.topic_min_frac))
        ops.append(Op("size", "summary"))
    for o in ops:
        if o.when not in WHEN or o.transform not in TRANSFORMS[o.when]:
            raise ValueError(f"operator {o.label()}: when must be one of {WHEN} with a transform from {TRANSFORMS}")
        if set(o.kinds) - set(KINDS) or o.frac < 0:
            raise ValueError(f"operator {o.label()}: kinds from {KINDS}, frac >= 0")
    return ops


CLIFF = dict(trigger="size", keep_recent=3, output_keep_chars=500, cmd_max_chars=150, human_max_chars=20_000,
             thought_max_chars=0, keep_thinking=True, protect_user=False, protect_spec=False, recall=False,
             tool_dialect="wire", writer="mechanical", previous="drop", decider="always")


def render_item(x, k, session=None):
    """Replacement lines for one item leaving the context. With Cliff's settings this is Cliff's summarize_item."""
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
    full = k.tool_dialect == "full"
    if h == "function_call_output" or (h is None and x["seg"] == "out") or (full and x["seg"] == "out"):
        out = (x.get("text") or "").strip()
        if out and len(out) <= k.output_keep_chars:
            return [f"result: {out}"]
        if out and k.recall and x.get("id") is not None:
            where = f" session={session}" if session else ""
            return [f"result: [archived #{x['id']}{where}, {len(out)} chars]"]
        return []
    if (h or "").endswith("_call") or (h is None and x["seg"] == "call"):
        args = x.get("wire_arguments", x.get("args", x.get("text", "") if h is None else ""))
        if full and not args:
            args = x.get("args") or x.get("text") or ""
        if not isinstance(args, str):
            args = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return [f"[{x.get('name') or h or 'call'}] {truncate(args, k.cmd_max_chars)}"]
    return []


class InfiniteContextV5(CliffCompaction):
    """One R_t, every factor a switch; the defaults are CliffCompaction."""
    source = "本项目：infinite-context V5（统一模型，CliffCompaction 为默认特例）"

    def __init__(self, **switches):
        unknown = set(switches) - {f.name for f in dataclasses.fields(Spec)} - {"t"}
        if unknown:
            raise ValueError("unknown switches: " + ", ".join(sorted(unknown)))
        t = switches.pop("t", 100 * K)
        spec = Spec(**{**CLIFF, **switches}).check()
        super().__init__(t=t, keep_recent=spec.keep_recent, result_max_chars=spec.output_keep_chars,
                         cmd_max_chars=spec.cmd_max_chars)
        self.knobs = spec
        self.spec = spec
        departures = {k: v for k, v in dataclasses.asdict(spec).items()
                      if k not in ("ops", "reuse_profile") and CLIFF.get(k, getattr(Spec, k, None)) != v}
        if spec.reuse_profile is not None:
            departures["reuse"] = "+".join(f"{a}={v}" for a, v in (spec.reuse_profile.get("attrs") or {}).items())
        self.name = f"infinite-context V5（{t // K}k" + ("" if not departures else "；" + "，".join(f"{k}={v}" for k, v in departures.items())) + "）"
        ops = build_ops(spec)
        if any(o.transform == "host" for o in ops):
            if spec.host_t:
                self.codex_config = {"model_auto_compact_token_limit": spec.host_t}
            else:
                self.host_compaction = "default"     # launchers pass no compaction override, as for HostDefault
        stores = any(o.transform in ("shorten", "structure", "truncate", "placeholder") for o in ops)
        archived = spec.recall or stores
        self.memory = "id" if stores else None
        self.agent_tools = _tools() if archived else ()
        self.instructions = RECALL_NOTE if archived else ""
        by = lambda *whens: "；".join(o.describe(t) for o in ops if o.when in whens) or "无"
        self.framework = dict(L1=by("entry"), L2=by("segment_end"), L3=by("topic", "size"),
                              cross=("可按编号读回原文" if archived else "无"), memory=("归档" if archived else "无"),
                              decider=spec.decider + ("＋运行时信号（阶段、找回）" if spec.adaptive else "")
                              + ("＋长短期再用记忆" if spec.reuse_profile is not None else "")
                              + ("＋先看后动、正在用的不动" if spec.guard else ""))
        self.ops = build_ops(spec)
        if spec.ops is not None:
            self.name = self.name[:-1] + "；ops=" + "+".join(o.label() for o in self.ops) + "）"
        self._recent = []                 # last calls and outputs (phase)
        self._removed = {}                # read command -> its paths, for outputs shortened or removed
        self._sticky = set()              # paths re-read after a removal: L2 leaves them alone
        self._harm = 0
        self._harm_seg = None             # segid of the last harm, for decay
        self._seg_done = 0                # L2 has processed every segment below this segid
        self._new_user = False            # a user message after the model started (topic)
        self._calls = 0
        self._topic_at = -10 ** 9          # call count at the last topic change
        self._paths = []                  # paths of recent calls, oldest first (topic)
        self._after = None                # request size after the last compaction (hysteresis)
        self._reuse = None if spec.reuse_profile is None else ReuseMemory(spec.reuse_profile)
        self._keep = frozenset()          # ids the reuse memory protects at this request (outputs and their calls)
        self._use = Usage()               # seen / in-use signals, kept even without the guard (R1 audit)
        self._held = frozenset()          # ids the guard holds at this request
        self._pressure = 0.0              # value hold: marginal rebuild cost of one kept token per request
        self._since = None                # requests since the last compaction

    # ------------------------------------------------------------------ runtime signals
    def _phase(self):
        work = any((x["seg"] == "call" and x.get("kind") == "edit") or (x["seg"] == "out" and x.get("test"))
                   for x in self._recent)
        return "work" if work else "explore"

    def _keep_recent(self):
        s = self.spec
        if not s.adaptive:
            return s.keep_recent
        return s.keep_recent + (s.work_keep if self._phase() == "work" else 0) + self._harm

    def on_ingest(self, sim, item):
        s = self.spec
        self._use.add(item)
        if item["seg"] in ("call", "out"):
            self._recent = (self._recent + [item])[-16:]
        if (item["seg"] == "msg" and item.get("role") == "user" and self._calls
                and not (item.get("text") or "").startswith(SUMMARY_HEADER)):
            self._new_user = True
        if self._reuse is not None and item["seg"] == "call":
            self._reuse.call(item.get("kind"), item.get("res"))
        if self._reuse is not None and item["seg"] == "out":
            self._reuse.output(item.get("id"), item.get("kind"), item.get("res"), item.get("outpaths"))
        if item["seg"] == "call":
            self._calls += 1
            res = set(item.get("res") or ())
            if res:
                self._paths = (self._paths + [res])[-16:]
            if item.get("kind") == "edit":       # reading an edited file again is normal, not a loss
                self._removed = {c: p for c, p in self._removed.items() if not (p & res)}
            elif s.adaptive and _command(item.get("text")) in self._removed:
                self._sticky |= self._removed.pop(_command(item.get("text")))
                self._harm = min(s.harm_max, self._harm + 1)
                self._harm_seg = getattr(sim, "segid", None)
                sim.M["v5_refetch"] += 1
            return
        if item["seg"] != "out" or item.get("protected") or item.get("has_media"):
            return
        for op in self.ops:
            if op.when == "entry" and op.touches(item):
                self._entry(sim, item, op)

    def _entry(self, sim, item, op):
        s = self.spec
        budget = op.frac * self.t
        if s.adaptive:
            budget *= (s.work_factor if self._phase() == "work" else 1) * (1 + self._harm)
        if sim.seg_size(item) <= budget or set(item.get("res") or ()) & self._sticky:
            return
        if s.guard and sim.seg_size(item) <= s.guard_oversize_frac * self.t:
            sim.M["v5_entry_deferred"] += 1       # R1: wait until the model has seen and used it
            return
        if self._reuse is not None and self._reuse.p(item.get("id"), item.get("kind"), self._phase()) >= self._reuse_floor():
            sim.M["v5_reuse_kept"] += 1
            return
        was = item.get("form", "full")
        if op.transform == "structure" or (op.transform == "shorten" and content_kind(item) in ("test", "search")):
            sim.structure(item)
        if op.transform in ("shorten", "truncate") and sim.seg_size(item) > budget:
            sim.truncate(item, budget)
        if item.get("form", "full") != was:
            self._forget(item)
            sim.M["v5_entry"] += 1
            self._audit(sim, [item])

    def _forget(self, out):
        """Remember a shortened or removed read, so running the same read again counts as a loss."""
        if out.get("kind") in ("read", "spec", "search") and out.get("call_text"):
            self._removed[_command(out["call_text"])] = set(out.get("res") or ())

    def _segment_closed(self, sim):
        """True once per new closed segment; also where harm decays (a whole segment closed without a re-read)."""
        cur = getattr(sim, "segid", 0)
        if cur <= self._seg_done:
            return False
        if self.spec.adaptive and self._harm and self._harm_seg is not None and self._harm_seg < cur - 1:
            self._harm -= 1
            self._harm_seg = cur - 1
        return True

    def _segment_pass(self, sim, op):
        """Once per closed segment, the long outputs of all closed segments are transformed in one batch."""
        s, cur = self.spec, getattr(sim, "segid", 0)
        last_tests = {x["id"] for x in sim.ctx if x["seg"] == "out" and x.get("test") and x.get("segid") == cur - 1}
        old = lambda x: x.get("segid", cur) < cur or (s.supersede and self._use.superseded(x))
        cands = [x for x in sim.ctx if x["seg"] == "out" and old(x) and op.touches(x)
                 and x.get("form", "full") in ("full", "truncated", "structured")
                 and not x.get("protected") and not x.get("has_media") and x["id"] not in last_tests
                 and len(x.get("text") or "") > s.output_keep_chars
                 and not (set(x.get("res") or ()) & self._sticky) and x["id"] not in self._keep
                 and x["id"] not in self._held]
        if sum(sim.seg_size(x) for x in cands) < op.frac * self.t:
            return False                          # too little to pay for the cache rebuild; wait for more
        if op.transform == "placeholder":
            sim.to_placeholder(cands, "memid")
        else:
            for x in cands:
                (sim.structure(x) if op.transform == "structure" else sim.truncate(x, s.entry_frac * self.t))
        self._audit(sim, cands)
        for x in cands:
            self._forget(x)
        sim.M["v5_segment"] += 1
        return True

    def _topic_changed(self, sim):
        new_user, self._new_user = self._new_user, False
        old, recent = self._paths[:-6], self._paths[-6:]
        a = set().union(*old) if old else set()
        b = set().union(*recent) if recent else set()
        cooled = self._calls - self._topic_at >= self.spec.topic_gap
        shifted = self.spec.topic_shift and cooled and len(a) >= 3 and len(b) >= 3 and not (a & b)
        if new_user or shifted:
            self._paths = self._paths[-6:]        # the new topic starts here
            self._topic_at = self._calls
        return new_user or shifted

    def _audit(self, sim, items):
        """R1/R2 counters: changes to outputs the model had not seen, or was still using (0 with the guard, except
        R1's oversize exception, counted apart)."""
        for x in items:
            if not self._use.seen(x):
                big = self.spec.guard and sim.seg_size(x) > self.spec.guard_oversize_frac * self.t
                sim.M["v5_r1_oversize" if big else "v5_r1_unseen_changed"] += 1
            elif self._use.active(x, self.spec.guard_active):
                sim.M["v5_r2_in_use_changed"] += 1

    def _holding(self, sim):
        """R2: outputs not yet seen or reused within guard_active requests, most recently used first, within
        guard_keep_frac of t, with their calls."""
        s = self.spec
        live = [x for x in sim.ctx if x["seg"] == "out" and x.get("form", "full") in ("full", "truncated", "structured")
                and not x.get("has_media")]
        if s.hold == "value":
            size_now = sim.request_chars() // 4
            held = [x for x in live if not self._use.seen(x) or self._use.active(x, s.hold_floor)
                    or (not (s.supersede and self._use.superseded(x)) and self._worth(sim, x, size_now))]
        else:
            held = [x for x in live if not self._use.seen(x) or self._use.active(x, s.guard_active)]
        keep, used, budget = set(), 0, s.guard_keep_frac * self.t
        calls = {x.get("call_id"): x["id"] for x in sim.ctx if x["seg"] == "call" and x.get("call_id")}
        for x in sorted(held, key=lambda x: -self._use.last(x)):
            size = sim.seg_size(x)
            if used + size > budget:
                continue
            used += size
            keep.add(x["id"])
            if x.get("call_id") in calls:
                keep.add(calls[x["call_id"]])
        sim.M["v5_guard_held"] = len(keep)
        return frozenset(keep | self._resident(sim))

    def _resident(self, sim):
        """The newest spec outputs within spec_keep_frac of t, with their calls (framework page: 常驻)."""
        s = self.spec
        if not s.spec_keep_frac:
            return frozenset()
        specs = [x for x in sim.ctx if x["seg"] == "out" and x.get("kind") == "spec"
                 and x.get("form", "full") == "full" and not (s.supersede and self._use.superseded(x))]
        keep, used, budget = set(), 0, s.spec_keep_frac * self.t
        calls = {x.get("call_id"): x["id"] for x in sim.ctx if x["seg"] == "call" and x.get("call_id")}
        for x in reversed(specs):
            size = sim.seg_size(x)
            if used + size > budget:
                break
            used += size
            keep.add(x["id"])
            if x.get("call_id") in calls:
                keep.add(calls[x["call_id"]])
        sim.M["v5_spec_resident"] = len(keep)
        return frozenset(keep)

    def _worth(self, sim, x, size_now):
        """Value hold: P(reused within the horizon) x (re-read + one more request + model work) > carrying it.

        Carrying a token costs a cached read per request plus compaction pressure: what stays after a compaction
        brings the next one closer, and each compaction rebuilds the cache. With growth g per request and A tokens
        left after compacting at t, rebuild cost per request is about A g / (t - A); one more kept token adds
        g t / (t - A)^2 of it."""
        s, size = self.spec, sim.seg_size(x)
        q = self._use.p_reuse(x, s.hold_horizon)
        back = size + size_now * s.cached_price + s.refetch_work
        carry = size * s.hold_horizon * (s.cached_price + self._pressure)
        return q * back > carry

    def _update_pressure(self, size_now):
        """Growth per request since the last compaction and the marginal rebuild cost of a kept token."""
        if self._after is None or self._since is None:
            self._pressure = 0.0
            return
        g = max(0.0, (size_now - self._after) / max(1, self._since))
        a = min(self._after, 0.9 * self.t)
        self._pressure = min(1.0, g * self.t / (self.t - a) ** 2)

    def _reuse_floor(self):
        return max(1e-3, self.spec.reuse_min_lift * self._reuse.p0)

    def _protected(self, sim):
        """Outputs most likely to be needed again (at least reuse_min_lift times the average), within reuse_keep_frac
        of t, most likely first, with their calls."""
        s, phase, floor = self.spec, self._phase(), self._reuse_floor()
        scored = []
        for x in sim.ctx:
            if (x["seg"] == "out" and x.get("form", "full") in ("full", "truncated", "structured")
                    and not x.get("has_media")):
                q = self._reuse.p(x.get("id"), x.get("kind"), phase)
                if q >= floor:
                    scored.append((q, x))
        keep, used, budget = set(), 0, s.reuse_keep_frac * self.t
        calls = {x.get("call_id"): x["id"] for x in sim.ctx if x["seg"] == "call" and x.get("call_id")}
        for q, x in sorted(scored, key=lambda e: -e[0]):
            size = sim.seg_size(x)
            if used + size > budget:
                continue
            used += size
            keep.add(x["id"])
            if x.get("call_id") in calls:
                keep.add(calls[x["call_id"]])
        sim.M["v5_reuse_protected"] = len(keep)
        return frozenset(keep)

    # ------------------------------------------------------------------ trigger and decider
    def step(self, sim, r):
        self.reset(sim)
        self._session = getattr(sim, "store_namespace", None)
        s = self.spec
        self._use.request(x["id"] for x in sim.ctx if x["seg"] == "out"
                          and x.get("form", "full") in ("full", "truncated", "structured"))
        if self._reuse is not None:
            self._reuse.request(r, self._phase())
            self._keep = self._protected(sim)
        if s.guard:
            if self._since is not None:
                self._since += 1
            self._update_pressure(sim.request_chars() // 4)
            self._held = self._holding(sim)
        before = {x["id"]: x for x in sim.ctx if x["seg"] == "out"} if s.adaptive else {}
        try:
            self._step(sim, r)
        finally:
            if before:                            # outputs a summary just removed
                now = {x["id"] for x in sim.ctx}
                for i, x in before.items():
                    if i not in now:
                        self._forget(x)

    def _step(self, sim, r):
        s = self.spec
        knobs = s if not s.adaptive else dataclasses.replace(s, keep_recent=self._keep_recent())
        segment_ops = [o for o in self.ops if o.when == "segment_end"]
        if segment_ops and self._segment_closed(sim):
            done = [self._segment_pass(sim, o) for o in segment_ops]
            if any(done):
                self._compacted = True
                self._seg_done = getattr(sim, "segid", 0)
        topic_ops = [o for o in self.ops if o.when == "topic"]
        if topic_ops and self._topic_changed(sim):
            size = sim.request_chars() // 4
            if any(o.frac * self.t < size <= self.t for o in topic_ops) and self._chain(sim, knobs, force=True):
                self._compacted = True
                self._after = sim.request_chars() // 4
                self._since = 0
                sim.M["v5_topic"] += 1
                return
        if not any(o.when == "size" and o.transform == "summary" for o in self.ops):
            return                                # no size operator here, or the host compacts by itself
        size = sim.request_chars() // 4
        if s.trigger == "size" and size <= self.t:
            return
        if (s.min_growth and self._after is not None and size < self._after + s.min_growth * self.t
                and not (s.hard_t and size > s.hard_t)):
            return
        self.knobs = knobs
        try:
            self._step_compact(sim, r, size)
        finally:
            self.knobs = s
        if self._compacted:
            self._after = sim.request_chars() // 4
            self._since = 0

    def _step_compact(self, sim, r, size):
        if self.spec.decider == "cost" and not (self.spec.hard_t and size > self.spec.hard_t):
            gain = self._gain(sim)
            sim.M["v5_decisions"] += 1
            if gain is None or gain <= 0:
                sim.M["v5_deferred"] += 1
                return
        if self.spec.writer != "mechanical":
            self._compacted = self._model_compaction(sim)
            return
        CliffCompaction.step(self, sim, r) if self.spec.trigger == "size" else self._chain_always(sim)

    def _chain_always(self, sim):
        self._compacted = self._chain(sim, self.knobs, force=True)

    def _gain(self, sim):
        """Modelled net value of compacting now, in uncached-input-token units (positive = compact)."""
        items = sim.sent()
        res = self._compact(list(items), self.knobs)
        if res is None:
            return None
        s = self.spec
        before = sim.request_chars() // 4
        fixed = (sim.fixed_chars if sim.fixed_chars is not None else sim.prefix * 4) // 4
        after = fixed + sum(self._chars(sim, x) + 2 for x in res["new"]) // 4
        removed = before - after
        if removed <= 0:
            return None
        saving = removed * s.horizon * (s.cached_price + s.length_penalty)
        rebuild = (after - fixed) * (1 - s.cached_price)
        lost = sum(1 for x in res["summary"]["members"] if x["seg"] == "out" and len(x.get("text") or "") > s.output_keep_chars)
        recovery = lost * s.recovery_rate * (s.recovery_cost + after * s.cached_price)
        writer = 0.0 if s.writer == "mechanical" else removed + s.writer_tokens * s.output_price
        return saving - rebuild - recovery - writer

    # ------------------------------------------------------------------ scope and previous replacements
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

        def stays(x):
            return (x.get("protected") or (k.protect_user and x["seg"] == "msg" and x.get("role") == "user")
                    or (k.protect_spec and x.get("kind") == "spec") or x.get("id") in self._keep
                    or x.get("id") in self._held)
        gone = [x for x in old if not stays(x)]
        if not gone:
            return None
        parts = []
        if k.previous == "keep":
            for x in gone:
                if x.get("cliff") or is_summary(x):
                    body = (x.get("text") or "")[len(SUMMARY_HEADER):].strip()
                    if body:
                        parts.append(body)
        parts += [p for x in gone for p in render_item(x, k, getattr(self, "_session", None))]
        text = SUMMARY_HEADER + "\n\n" + "\n\n---\n\n".join(parts) if parts else SUMMARY_HEADER
        kept = [x for x in old if stays(x)] + [x for t in turns[len(turns) - keep:] for x in t]
        summary = dict(seg="summary", virtual=True, cliff=True, text=text, members=gone)
        new = [*working[:head], summary, *kept]
        if len(new) >= len(working):
            return None
        return dict(new=new, head=head, summary=summary, cut=len(working) - len(kept))

    # ------------------------------------------------------------------ model-written replacements
    def _model_compaction(self, sim):
        res = self._compact(list(sim.sent()), self.knobs)
        if res is None:
            return False
        members = [x for x in res["summary"]["members"] if x in sim.ctx
                   and not (self.spec.previous == "drop" and (x.get("cliff") or is_summary(x)))]
        if not members:
            return False
        state = sim.summarize_segment(members, size=self.spec.writer_tokens, guidance=STATE_GUIDANCE, billed=True,
                                      cliff=True) if self.spec.writer == "llm" else None
        if self.spec.writer == "llm":
            if state is None:
                sim.M["v5_writer_failed"] += 1
                return False
            state["text"] = SUMMARY_HEADER + "\n\n" + state["text"]
            state["chars"] = sim.message_chars(state["text"])
            state["size"] = sim.tokens(state["text"])
            return True
        # hybrid: a model-written state ahead of the mechanical lines
        written = sim._summary_text(members, "segment", self.spec.writer_tokens, STATE_GUIDANCE) \
            if hasattr(sim, "_summary_text") else None
        if written is None:
            sim.M["v5_writer_failed"] += 1
            return False
        mech = res["summary"]["text"][len(SUMMARY_HEADER):].strip()
        text = SUMMARY_HEADER + "\n\n[state]\n" + written + ("\n\n---\n\n" + mech if mech else "")
        new = sim.summarize_segment(members, text=text, billed=True, cliff=True)
        if new is not None:
            new["chars"] = sim.message_chars(text)
            new["size"] = sim.tokens(text)
        return new is not None

    # ------------------------------------------------------------------ recall
    def call_tool(self, name, args):
        from ctxpress import settings
        from ctxpress.methods.infinite_archive import call
        try:
            return json.dumps(call(name, args, settings.store_dir()), ensure_ascii=False), False
        except (OSError, ValueError, KeyError, TypeError) as error:
            return json.dumps(dict(error=str(error)), ensure_ascii=False), True


_LINE_MIN = 24
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]{5,}")


_NUMBERED = re.compile(r"^(?:[\w./-]+[:-])?\d+[:\t|-]\s?")    # "12:", "12\t", "path:12:" from nl, grep -n, rg -n


def _features(text):
    lines = {hash(ln) for ln in (_NUMBERED.sub("", l.strip()).strip() for l in text.splitlines()) if len(ln) >= _LINE_MIN}
    return lines, set(_IDENT.findall(text))


def _unescape(text):
    """Source text quoted inside code (Code Mode JavaScript, heredocs in JSON) keeps its escapes; undo the common ones."""
    return text.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\'", "'")


def _strings(v):
    """Every string inside call arguments, unescaped (arguments usually arrive as a JSON string)."""
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except ValueError:
            return [v]
        return _strings(parsed) if not isinstance(parsed, str) else [parsed]
    if isinstance(v, dict):
        return [t for x in v.values() for t in _strings(x)]
    if isinstance(v, list):
        return [t for x in v for t in _strings(x)]
    return []


AGE_BUCKETS = (1, 2, 3, 5, 8, 13, 21, 34, 55)
AGE_PRIOR, AGE_DECAY, AGE_N0 = 0.4, 0.8, 5.0     # prior P(reuse) at age 1, its decay per bucket, prior weight


def _sources(x):
    """Where an output came from: the same call text (a read of another range of a file is another source)."""
    cmd = _command(x.get("call_text"))
    return [("cmd", cmd)] if cmd else []


class Usage:
    """Generic seen / in-use signals (framework page, table F3), for every tool and host.

    seen     the model produced something after the output arrived
    reused   a later model item (call arguments, text) repeats a line of the output that occurs in few outputs,
             or shares at least `min_idents` identifiers that occur in few outputs (with path_use, also naming the
             path it came from)"""

    def __init__(self, min_idents=4, rare=3, max_paths=4, path_use=False):
        self.min_idents, self.rare, self.max_paths, self.path_use = min_idents, rare, max_paths, path_use
        self.broad = set()                # outputs drawn from more than max_paths paths
        self.model = 0                    # model items so far
        self.req = 0                      # requests so far
        self.born, self.used = {}, {}     # output id -> model count at arrival / request of last reuse
        self.lines, self.idents, self.paths = {}, {}, {}
        self.source, self.newer = {}, set()   # source key -> latest output id; ids replaced by a newer output
        self.pending = []                 # (request, id, bucket) waiting for the horizon to pass
        self.horizon = 20
        self.n = [0] * len(AGE_BUCKETS)   # resolved observations per age bucket
        self.k = [0] * len(AGE_BUCKETS)   # of which reused within the horizon

    def request(self, alive=()):
        self.req += 1
        ages = set(AGE_BUCKETS)
        for i in alive:
            a = self.req - self.used.get(i, self.req)
            if a in ages:
                self.pending.append((self.req, i, AGE_BUCKETS.index(a)))
        while self.pending and self.pending[0][0] + self.horizon <= self.req:
            r0, i, b = self.pending.pop(0)
            self.n[b] += 1
            self.k[b] += self.used.get(i, -1) > r0

    def p_reuse(self, x, horizon):
        """P(reused again within `horizon` requests) for an output last reused `a` requests ago."""
        self.horizon = horizon
        a = self.req - self.last(x)
        b = max([j for j, age in enumerate(AGE_BUCKETS) if age <= a] or [0])
        prior = AGE_PRIOR * AGE_DECAY ** b
        return (self.k[b] + prior * AGE_N0) / (self.n[b] + AGE_N0)

    def superseded(self, x):
        return x.get("id") in self.newer

    def add(self, x):
        if x["seg"] == "out" and x.get("id") is not None:
            i = x["id"]
            self.born[i], self.used[i] = self.model, self.req
            lines, idents = _features(_unescape(x.get("text") or ""))
            for h in lines:
                ids = self.lines.setdefault(h, set())
                if len(ids) <= self.rare:
                    ids.add(i)
            for w in idents:
                ids = self.idents.setdefault(w, set())
                if len(ids) <= self.rare:
                    ids.add(i)
            res = set(x.get("res") or ())       # its own source; a listing's paths move value, not reuse it
            if len(res) > self.max_paths:
                self.broad.add(i)
            for p in res:
                self.paths.setdefault(p, set()).add(i)
            for key in _sources(x):
                old = self.source.get(key)
                if old is not None and old != i:
                    self.newer.add(old)
                self.source[key] = i
            return
        if x["seg"] not in ("msg", "call", "reason") or (x["seg"] == "msg" and x.get("role") != "assistant"):
            return
        self.model += 1
        text = (x.get("text") or "") + "\n" + "\n".join(_strings(x.get("wire_arguments", x.get("args"))))
        lines, idents = _features(_unescape(text))
        hit = set()
        for h in lines:
            ids = self.lines.get(h, set())
            if len(ids) <= self.rare:
                hit |= ids
        counts = {}
        for w in idents:
            ids = self.idents.get(w)
            if ids and len(ids) <= self.rare:
                for i in ids:
                    counts[i] = counts.get(i, 0) + 1
        hit |= {i for i, n in counts.items() if n >= self.min_idents}
        res = x.get("res") or ()
        if self.path_use and len(res) <= self.max_paths:   # naming a path is weak evidence; off by default
            for p in res:
                hit |= {i for i in self.paths.get(p, ()) if i not in self.broad}
        for i in hit:
            self.used[i] = self.req

    def seen(self, x):
        return self.model > self.born.get(x.get("id"), -1)

    def last(self, x):
        return self.used.get(x.get("id"), -1)

    def active(self, x, window):
        return self.req - self.last(x) <= window


def _command(text):
    return " ".join((text or "").split())


def _tools():
    from ctxpress.methods.infinite_archive import TOOLS
    return TOOLS
