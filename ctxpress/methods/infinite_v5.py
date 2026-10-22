"""infinite-context V5: one context-update rule R_t with every factor an explicit switch.

C_t = R_t(C_{t-1} + new items). The switches, grouped as in experiment-method.zh.html §8-9:

  trigger      when R_t acts: request size above `t` input tokens (the request as the host sends it, estimated by
               ctxpress.core.sizes from the provider's bill), or every request (`always`).
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
import collections, dataclasses, json, re
from ctxpress.methods.cliff import (CliffCompaction, Knobs, SUMMARY_HEADER, _TASK_NOTIFICATION_RE, group_turns,
                                    is_summary, truncate)
from ctxpress.core import tokens
from ctxpress.core.engine import is_model
from ctxpress.methods.reuse_memory import ReuseMemory
from ctxpress.core.cache_model import Cache, RHO

K = 1000
STATE_GUIDANCE = ("Write the working state a coding agent needs to continue after these steps are removed from its "
                  "context: goals, constraints and user requirements, decisions, what was verified, failures and their "
                  "causes, unresolved issues, and the next steps. Keep exact identifiers, paths, commands and error "
                  "messages. Do not invent progress. Plain text with short labelled sections.")
STATE_SYSTEM = ("You keep the working state of a coding agent whose older steps are being removed from its context. "
                "The input is JSON: previous_state (the state written at the last removal, may be empty) and steps "
                "(the removed steps, oldest first; long tool outputs are cut, their full text stays archived under "
                "#ID). Write the updated state the agent needs to continue, in these sections: TASK (the overall "
                "task and how work arrives, e.g. a task queue file); DONE (one line per finished task or milestone: "
                "its id, what was implemented, the main files, the tests that passed); CURRENT (the task or milestone "
                "in progress: its id, the requirements still open with exact names, decisions made and why, files "
                "changed, what was verified and how, logs and scripts under /tmp, processes still running); PROBLEMS (failures and their causes, unresolved issues); NEXT "
                "(the next steps). Keep exact identifiers, paths, commands, error messages, and the #ID of outputs "
                "worth reading again. Carry forward what still holds from previous_state, shortening DONE lines of "
                "older work; do not invent progress. Plain text, at most LIMIT. Also list in keep the #ID "
                "of the few outputs among the steps that cannot be recovered from the environment and are still "
                "needed verbatim: results of test or build runs whose failures are not fixed yet, error logs, output "
                "of commands that cannot simply be run again. Never list file contents, requirement documents, "
                "listings, searches or diffs: they can be read again from disk. Outputs marked stale were superseded. "
                "Return a JSON object {\"state\": \"<the state>\", \"keep\": [<#ID numbers>]}.")
STEPS_MARK = "\n\n[earlier steps]\n"     # between a written state and the mechanical lines after it
WRITER_RETRY = 20                         # requests before a writer that failed twice in a row is tried again
RECALL_NOTE = ("Removed or shortened tool outputs are archived. Their references look like [archived #ID] or "
               "'stored at #ID'; read one with infinite_context_read (id=ID) or find text with "
               "infinite_context_search.")
WRITERS = ("mechanical", "llm", "hybrid")
PREVIOUS = ("drop", "keep")
DECIDERS = ("always", "cost")
TRIGGERS = ("size", "always")
DIALECTS = ("wire", "full")
HOLDS = ("window", "value")
CHOOSERS = ("fixed", "cost")


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
    reasoning_header: bool = False        # proxy: tell Codex the reported input tokens include reasoning
                                          # (x-reasoning-included); without it Codex adds an estimate of all older
                                          # encrypted reasoning in its local history after a new user message and
                                          # compacts early (10-22: 70.7k provider tokens; Vanilla 91-109k, 3 of 17)
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
    reread_use: bool = False              # a read of a source an output came from (the same command, or a file it
                                          # was read from, not edited since) counts as using that output again, and
                                          # outputs a summary or placeholder removed stay in the reuse statistics:
                                          # P(reuse) then includes the re-reads that removing content causes
    supersede: bool = False               # outputs replaced by a newer one from the same source (same path read
                                          # again, same command run again) are never held and go at the next batch
    spec_keep_frac: float = 0.0           # resident requirements: the newest spec outputs (SRS, design docs), together
                                          # at most this share of t, stay verbatim through every operator (0 = off)
    release_at_topic: bool = False        # cost: at a commit or a new user message, outputs last used before it are no
                                          # longer held (what is used again is held again), and requirements
                                          # documents a commit or tag names are no longer resident
    commit_topic: bool = False            # cost: a commit or tag is a topic change (it records finished work) and a
                                          # first read of a requirements document is not (agents read them ahead)
    choose: str = "fixed"                 # fixed: the operators the switches list; cost: at every request each
                                          # available operator is a candidate and the cost model picks one (§9.7)
    writers: tuple = ("mechanical",)      # cost: summary writers that are candidates (mechanical, hybrid)
    writer_model: str = ""                # hybrid: who writes the task state; "" = the host's summary service,
                                          # "deepseek-flash" = DeepSeek on its own route (ctxpress/live/deepseek.py)
    writer_input_price: float = 1.0       # cost: the writer's price per input token, in uncached-input units
    writer_output_price: float = 8.0      # cost: the writer's price per output token, in uncached-input units
    writer_input_tokens: int = 40_000     # hybrid with writer_model: most tokens of history the writer reads
    writer_keep_frac: float = 0.0         # hybrid with writer_model: outputs the writer judges not recoverable from
                                          # the environment and still needed (unfixed test failures, error logs)
                                          # stay verbatim, together at most this share of t; file contents, docs,
                                          # listings and diffs can be read again and go (0 = off)
    stale_after_edit: bool = False        # an edit of a file supersedes the earlier reads of it. Off: in recorded
                                          # SWE-Milestone sessions agents edit a file a median 2-3 calls after reading
                                          # it (scripts and sed -i, almost never apply_patch) and keep working from the
                                          # read, so this would release what is in use
    same_output: bool = False             # an output identical to an earlier one supersedes it. Off: no two outputs
                                          # were identical in recorded sessions (timing and session fields differ)
    state_recovery_factor: float = 0.5    # cost, hybrid: share of recoveries still needed with a written task state
                                          # (assumed; to be set by trials)
    cache_rho: tuple = RHO                # cost: provider cache retention after an edit (core/cache_model.py)
    summary_at: str = "boundary"          # cost: when the rent account may summarize (above topic_min_frac of t):
                                          # any = rent >= F; topic = only after a topic change; boundary = at a
                                          # topic change once rent >= boundary_early F, else at boundary_late F
    boundary_early: float = 0.5           # boundary: rent/F from which a topic change is taken (L = 0.71 L*)
    boundary_late: float = 2.0            # boundary: rent/F at which it summarizes without one (L = 1.41 L*)
    boundary_window: int = 3              # boundary: requests after a topic change that still count as at it
    at_hard: str = "summary"              # cost, above t: our mechanical summary (the deadline), or leave it to the
                                          # host ("host"). t is the request's input tokens; Codex compacts near
                                          # 0.9 x its 128k window = 115.2k
    summary_min_frac: float = 0.15        # cost: a summary must remove at least this share of t
    candidates: tuple = ("placeholder", "summary")   # cost: candidate operators (ablation removes one)

    def check(self):
        for name, allowed in (("trigger", TRIGGERS), ("writer", WRITERS), ("previous", PREVIOUS), ("decider", DECIDERS),
                              ("tool_dialect", DIALECTS)):
            if getattr(self, name) not in allowed:
                raise ValueError(f"{name} must be one of {allowed}")
        if self.choose not in CHOOSERS or not self.writers or set(self.writers) - {"mechanical", "hybrid"}:
            raise ValueError(f"choose must be one of {CHOOSERS}; writers from mechanical, hybrid")
        if set(self.candidates) - {"placeholder", "summary"}:
            raise ValueError("candidates from placeholder, summary")
        if self.summary_at not in ("any", "topic", "boundary"):
            raise ValueError("summary_at must be any, topic or boundary")
        if self.at_hard not in ("summary", "host") or not 0 <= self.summary_min_frac < 1:
            raise ValueError("at_hard must be summary or host; 0 <= summary_min_frac < 1")
        if not (0 < self.boundary_early <= 1 <= self.boundary_late) or self.boundary_window < 0:
            raise ValueError("0 < boundary_early <= 1 <= boundary_late; boundary_window >= 0")
        if not (0 <= self.state_recovery_factor <= 1) or len(self.cache_rho) < 1:
            raise ValueError("state_recovery_factor in [0, 1]; cache_rho nonempty")
        if (self.writer_model not in ("", "deepseek-flash") or self.writer_input_price < 0
                or self.writer_output_price < 0 or self.writer_input_tokens < 250 or not 0 <= self.writer_keep_frac <= 1):
            raise ValueError("writer_model is '' or deepseek-flash; writer prices >= 0; writer_input_tokens >= 250; "
                             "writer_keep_frac in [0, 1]")
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
    """Replacement lines for one item leaving the context. With Cliff's settings this is Cliff's summarize_item:
    its caps are the authors' content rules in characters (an output of at most output_keep_chars characters is
    kept verbatim); an archived output's reference states its tokens."""
    if x.get("cliff") or x["seg"] == "summary":
        return []
    cut, keep_max = truncate, k.output_keep_chars
    h = x.get("htype")
    if x["seg"] == "msg":
        role, text = x.get("role", ""), (x.get("text") or "").strip()
        if not text:
            return []
        if role == "assistant":
            return [f"assistant: {cut(text, k.thought_max_chars)}"]
        if text.startswith(SUMMARY_HEADER):
            return []
        if role == "user":
            text = _TASK_NOTIFICATION_RE.sub("", text).strip()
            return [f"user: {cut(text, k.human_max_chars)}"] if text else []
        return [f"{role}: {cut(text, k.human_max_chars)}"]
    if h == "reasoning" or (h is None and x["seg"] == "reason"):
        text = (x.get("text") or "").strip()
        return [f"thinking: {cut(text, k.thinking_max_chars)}"] if k.keep_thinking and text else []
    full = k.tool_dialect == "full"
    if h == "function_call_output" or (h is None and x["seg"] == "out") or (full and x["seg"] == "out"):
        out = (x.get("text") or "").strip()
        if out and len(out) <= keep_max:
            return [f"result: {out}"]
        if out and k.recall and x.get("id") is not None:
            where = f" session={session}" if session else ""
            return [f"result: [archived #{x['id']}{where}, {_count(out)} tokens]"]
        return []
    if (h or "").endswith("_call") or (h is None and x["seg"] == "call"):
        args = x.get("wire_arguments", x.get("args", x.get("text", "") if h is None else ""))
        if full and not args:
            args = x.get("args") or x.get("text") or ""
        if not isinstance(args, str):
            args = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return [f"[{x.get('name') or h or 'call'}] {cut(args, k.cmd_max_chars)}"]
    return []


class InfiniteContextV5(CliffCompaction):
    """One R_t, every factor a switch; the defaults are CliffCompaction."""
    source = "本项目：infinite-context V5（统一模型，CliffCompaction 为默认特例）"

    def __init__(self, **switches):
        unknown = set(switches) - {f.name for f in dataclasses.fields(Spec)} - {"t"}
        if unknown:
            raise ValueError("unknown switches: " + ", ".join(sorted(unknown)))
        t = switches.pop("t", 100 * K)
        if switches.get("choose") == "cost":       # hard constraints of §9.7, not switches: nothing unseen or in use
            # changes; specs stay. Superseded reads released and at most 20% of t held: what is held cannot be
            # summarized, and the hold carried 32 outputs per request at 30% (发现 24). The value hold holds as many
            # tokens as the window on recorded Codex sessions, so the window stays.
            # Prices of gpt-6.1-sol per uncached input token: cached $0.1 / $2.0 = 0.05, output $10 / $2 = 5.
            # Summaries only: at a cached price of 0.05 an edit inside the prompt (a placeholder) rebuilds all that
            # follows it at 0.95 and saves 0.05 per removed token per request, so it pays back only after
            # ~19 x (tokens after it / tokens removed) requests, longer than a cycle; it also removes outputs while
            # their topic may still be open.
            # Specs stay as a bounded resident set (newest first, superseded released, at most spec_keep_frac of
            # t): kept unbounded they reached 49-74k by the end of a session. Later user messages (task queue
            # updates) stay, as Codex native compaction keeps them. Re-reads come within 10 requests 69% of the
            # time and within 15 about 75%, so an output stays in use for 15 requests.
            # From the 10-21 run (6 summaries of ours, 14 by Codex in 961 requests): Codex native compaction leaves
            # 26-29k (chars/4) and loses no score, ours left 41-58k, most of it held outputs and specs at 20% of t
            # each, so each gets 10%; with that much left the rent point at 2 F lay above t, so the rent summarizes
            # at F without a topic change. The task state is written by DeepSeek (deepseek-flash, $0.3 / $1.2 per
            # million = 0.15 / 0.6 of an uncached input token), about the size of Codex's compaction item (3.2-4.2k
            # output tokens), ahead of the mechanical lines and their archive numbers.
            # From 5 recorded Vanilla SWE-Milestone sessions (73 milestones): the commit and tag of a milestone are
            # its only exact boundary (73/73); 43/73 requirements documents were read before the previous milestone
            # was tagged (up to 8 open at once), so a first read is no boundary and open documents stay resident;
            # a tagged milestone's document was read again once in 73. At the tag 66% of the milestone's outputs
            # (39% of chars) are never referenced later, and later references re-read the file first 72% of the
            # time, so the guard releases them (uses within 2 requests before the tag still hold). Specs get 15%:
            # sklearn documents reach 8.9k and Codex's compaction drops them, after which they were re-read within
            # 1-4 requests in 24/24 cases. t counts input tokens (ctxpress.core.sizes): the 10-22 run counted
            # chars/4, 38% above the bill (median), so its 42 summaries came with 52-83k billed tokens on the
            # request before (median 72k), every 18-23 requests where the rent gives 28-35; Codex itself compacts
            # at 115.2k, above t = 102400.
            switches = {"reasoning_header": True, "guard": True, "protect_spec": True, "recall": True, "supersede": True,
                        "protect_user": True, "guard_active": 15, "guard_keep_frac": 0.1, "spec_keep_frac": 0.15,
                        "cmd_max_chars": 300, "cached_price": 0.05, "output_price": 5.0, "reread_use": True,
                        "candidates": ("summary",), "boundary_late": 1.0, "release_at_topic": True, "commit_topic": True,
                        "writers": ("hybrid",), "writer_model": "deepseek-flash", "writer_tokens": 3000,
                        "writer_input_price": 0.15, "writer_output_price": 0.6, "writer_keep_frac": 0.05, **switches}
        for name in ("writers", "cache_rho", "candidates"):
            if isinstance(switches.get(name), list):
                switches[name] = tuple(switches[name])
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
        stores = spec.choose == "cost" or any(o.transform in ("shorten", "structure", "truncate", "placeholder")
                                              for o in ops)
        archived = spec.recall or stores
        self.memory = "id" if stores else None
        writes = spec.writer == "hybrid" or (spec.choose == "cost" and "hybrid" in spec.writers)
        self.writer_model = spec.writer_model if writes else ""   # launchers open DeepSeek's route for it
        # the proxy gives a request a summary service only if the method asks for one; a writer model's calls are
        # recorded through it (LiveContext._prompt_text)
        self.requires_summary = writes or spec.writer == "llm"
        self.reasoning_included_header = spec.reasoning_header
        self.agent_tools = _tools() if archived else ()
        self.instructions = RECALL_NOTE if archived else ""
        by = lambda *whens: "；".join(o.describe(t) for o in ops if o.when in whens) or "无"
        self.framework = dict(L1=by("entry"), L2=by("segment_end"), L3=by("topic", "size"),
                              cross=("可按编号读回原文" if archived else "无"), memory=("归档" if archived else "无"),
                              decider=("每次请求按成本模型选算子（含缓存命中）" if spec.choose == "cost" else spec.decider)
                              + ("＋运行时信号（阶段、找回）" if spec.adaptive else "")
                              + ("＋长短期再用记忆" if spec.reuse_profile is not None else "")
                              + ("＋先看后动、正在用的不动" if spec.guard else ""))
        self.ops = build_ops(spec)
        if spec.ops is not None:
            self.name = self.name[:-1] + "；ops=" + "+".join(o.label() for o in self.ops) + "）"
        self._recent = []                 # last calls and outputs (phase)
        self._removed = {}                # read command -> its paths, for outputs shortened or removed
        self._sticky = set()              # paths re-read after a removal: L2 leaves them alone
        self._gone = set()                # reread_use: ids of outputs a summary or placeholder removed
        self._r, self.last_decision = 0, None   # the request being decided; what the last summary expected
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
        self._cache = None                # cost: the provider cache of this session (requests sent so far)
        self._sizes = []                  # cost: request sizes sent, for growth per request
        self._topic_pending = False       # cost: a topic change since the last summary
        self._cycle = 0                   # cost: index in _sizes of the first request after the last summary
        self._wait_from = None            # cost: request at which a placeholder batch started waiting
        self._ph_seg = -1                 # cost: segment id at the last placeholder batch
        self._topic_req = None            # cost: index in _sizes of the last topic change
        self._rebases = 0                 # cost: host history rebases seen (Codex native compaction)
        self._spec_calls = set()          # call ids that read a requirements document
        self._spec_paths = set()          # requirements documents read so far
        self._new_spec = False            # a requirements document never read before was read (topic)
        self._writer_fails = 0            # consecutive failed writer calls: after 2 the cost model plans mechanical
        self._writer_retry = 0            # ... until this Usage request count (WRITER_RETRY later), then tries again
        self._topic_use = -1              # cost: Usage request count from which use counts after the last release
        self._spec_of = {}                # call id -> requirements documents it reads (task queues left out)
        self._queue_calls = set()         # call ids that read a task queue
        self._spec_skip = frozenset()     # cost: spec outputs neither resident nor held (finished, older queues)
        self._texts = {}                  # same_output: text hash -> latest output id with that text
        self._commit = False              # a commit or tag was called since the last request
        self._done = {}                   # name commits and tags recorded (tag names, words of commit messages) ->
                                          # number of commit calls when it was last recorded
        self._commits = 0                 # commit or tag calls so far
        self._read_at = {}                # call id of a requirements read -> commit calls before it

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
            specs = _spec_paths(item.get("text"))
            if specs:
                self._spec_calls.add(item.get("call_id"))
                docs = {p for p in specs if not _QUEUE_PATH.search(p)}
                if docs:
                    self._spec_of[item.get("call_id")] = docs
                    self._read_at[item.get("call_id")] = self._commits
                if docs != specs:
                    self._queue_calls.add(item.get("call_id"))
                if specs - self._spec_paths and self._spec_paths:
                    self._new_spec = True
                self._spec_paths |= specs
            names = _committed(item.get("text"))
            if names is not None:
                self._commit = True
                self._commits += 1
                self._done.update(dict.fromkeys(names, self._commits))
            res = {p for p in res if not _SPEC_PATH.search(p)}   # polled queue / SRS reads are not the work
            if res:
                self._paths = (self._paths + [res])[-16:]
            if s.reread_use:
                if item.get("kind") == "edit":
                    self._use.edit(item.get("res") or ())
                else:
                    paths = item.get("res") or () if item.get("kind") in ("read", "spec") else ()
                    again = self._use.read_again(_command(item.get("text")), paths)
                    if again:
                        sim.M["op_reread"] += 1
                        if again & self._gone:
                            sim.M["op_reread_removed"] += 1
            if item.get("kind") == "edit":       # reading an edited file again is normal, not a loss
                self._removed = {c: p for c, p in self._removed.items() if not (p & res)}
                if s.stale_after_edit:
                    for p in res:
                        self._use.newer |= {i for i in self._use.paths.get(p, ()) if i not in self._use.broad}
            elif s.adaptive and _command(item.get("text")) in self._removed:
                self._sticky |= self._removed.pop(_command(item.get("text")))
                self._harm = min(s.harm_max, self._harm + 1)
                self._harm_seg = getattr(sim, "segid", None)
                sim.M["v5_refetch"] += 1
            return
        if item["seg"] != "out" or item.get("protected") or item.get("has_media"):
            return
        if item.get("call_id") in self._spec_calls:    # an SRS read in a mixed command is still the SRS
            item["kind"] = "spec"
        if s.same_output and self._length(item.get("text") or "") >= 200:
            key = hash(item["text"])
            if key in self._texts:
                self._use.newer.add(self._texts[key])
            self._texts[key] = item["id"]
        if s.choose == "cost":
            self._entry_cost(sim, item)
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

    def _closed_outputs(self, sim, op=None, held=True):
        """Long outputs of closed segments that L2 may transform: not the last segment's tests, nothing protected,
        held, kept by the reuse memory or re-read after a removal; protected specs stay."""
        s, cur = self.spec, getattr(sim, "segid", 0)
        last_tests = {x["id"] for x in sim.ctx if x["seg"] == "out" and x.get("test") and x.get("segid") == cur - 1}
        old = lambda x: x.get("segid", cur) < cur or (s.supersede and self._use.superseded(x))
        return [x for x in sim.ctx if x["seg"] == "out" and old(x) and (op is None or op.touches(x))
                and x.get("form", "full") in ("full", "truncated", "structured")
                and not x.get("protected") and not x.get("has_media") and x["id"] not in last_tests
                and self._length(x.get("text") or "") > s.output_keep_chars
                and not (s.choose == "cost" and s.protect_spec and x.get("kind") == "spec")
                and not (set(x.get("res") or ()) & self._sticky) and x["id"] not in self._keep
                and (not held or x["id"] not in self._held)]

    def _segment_pass(self, sim, op):
        """Once per closed segment, the long outputs of all closed segments are transformed in one batch."""
        s = self.spec
        cands = self._closed_outputs(sim, op)
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
        """Why the topic changed since the last request: "user", "commit", "spec", "shift", or "" (no change)."""
        new_user, self._new_user = self._new_user, False
        committed, self._commit = self._commit and self.spec.commit_topic, False
        old, recent = self._paths[:-6], self._paths[-6:]
        a = set().union(*old) if old else set()
        b = set().union(*recent) if recent else set()
        cooled = self._calls - self._topic_at >= self.spec.topic_gap
        shifted = self.spec.topic_shift and cooled and len(a) >= 3 and len(b) >= 3 and not (a & b)
        new_spec, self._new_spec = self._new_spec and not self.spec.commit_topic, False
        why = "user" if new_user else "commit" if committed else "spec" if new_spec else "shift" if shifted else ""
        if why:
            self._paths = self._paths[-6:]        # the new topic starts here
            self._topic_at = self._calls
        return why

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
        resident = self._resident(sim)
        apart = resident | self._spec_skip if s.choose == "cost" else frozenset()   # specs have their own budget
        live = [x for x in sim.ctx if x["seg"] == "out" and x.get("form", "full") in ("full", "truncated", "structured")
                and not x.get("has_media") and x["id"] not in apart]
        if s.hold == "value":
            size_now = self._size(sim)
            held = [x for x in live if not self._use.seen(x) or self._use.active(x, s.hold_floor)
                    or (not (s.supersede and self._use.superseded(x)) and self._worth(sim, x, size_now))]
        else:
            held = [x for x in live if not (s.supersede and self._use.superseded(x))
                    and (not self._use.seen(x) or (self._use.active(x, s.guard_active) and not self._released(x)))]
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
        return frozenset(keep | resident)

    def _resident(self, sim):
        """The newest spec outputs within spec_keep_frac of t, with their calls (framework page: 常驻)."""
        s = self.spec
        if not s.spec_keep_frac:
            return frozenset()
        specs = [x for x in sim.ctx if x["seg"] == "out" and x.get("kind") == "spec"
                 and x.get("form", "full") == "full" and not (s.supersede and self._use.superseded(x))]
        skip = {x["id"] for x in specs if self._finished(x)}
        if s.release_at_topic:                    # task queue listings (commit chains print one too): the newest
            queues = [x for x in specs if x.get("call_id") in self._queue_calls and not self._spec_of.get(x.get("call_id"))]
            skip |= {x["id"] for x in queues[:-1]}
        self._spec_skip = frozenset(skip)
        specs = [x for x in specs if x["id"] not in skip]
        keep, used, budget = set(), 0, s.spec_keep_frac * self.t
        calls = {x.get("call_id"): x["id"] for x in sim.ctx if x["seg"] == "call" and x.get("call_id")}
        order = sorted(reversed(specs), key=lambda x: -self._use.last(x)) if s.choose == "cost" else reversed(specs)
        for x in order:                           # cost: in use first, then newest; otherwise newest first
            size = sim.seg_size(x)
            if used + size > budget:
                continue                          # an older, smaller document may still fit
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
        self._r = r
        self._session = getattr(sim, "store_namespace", None)
        s = self.spec
        alive = {x["id"] for x in sim.ctx if x["seg"] == "out" and x.get("form", "full") in ("full", "truncated", "structured")}
        if s.reread_use:                      # removed, but still content the agent may need (until superseded)
            self._gone -= self._use.newer
            alive |= self._gone
        self._use.request(sorted(alive))
        if self._reuse is not None:
            self._reuse.request(r, self._phase())
            self._keep = self._protected(sim)
        if s.choose == "cost":
            self._observe(sim)
        if s.guard:
            if self._since is not None:
                self._since += 1
            self._update_pressure(self._size(sim))
            self._held = self._holding(sim)
        before = {x["id"]: x for x in sim.ctx if x["seg"] == "out"} if s.adaptive else {}
        try:
            if s.choose == "cost":
                self._choose(sim, r)
            else:
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
            size = self._size(sim)
            if any(o.frac * self.t < size <= self.t for o in topic_ops) and self._chain(sim, knobs, force=True):
                self._compacted = True
                self._after = self._size(sim)
                self._since = 0
                sim.M["v5_topic"] += 1
                return
        if not any(o.when == "size" and o.transform == "summary" for o in self.ops):
            return                                # no size operator here, or the host compacts by itself
        size = self._size(sim)
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
            self._after = self._size(sim)
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
        before = self._size(sim)
        fixed = sim.request_tokens([])
        after = sim.request_tokens(res["new"])
        removed = before - after
        if removed <= 0:
            return None
        saving = removed * s.horizon * (s.cached_price + s.length_penalty)
        rebuild = (after - fixed) * (1 - s.cached_price)
        lost = sum(1 for x in res["summary"]["members"] if x["seg"] == "out" and self._length(x.get("text") or "") > s.output_keep_chars)
        recovery = lost * s.recovery_rate * (s.recovery_cost + after * s.cached_price)
        writer = 0.0 if s.writer == "mechanical" else removed + s.writer_tokens * s.output_price
        return saving - rebuild - recovery - writer

    # ------------------------------------------------------------------ choose = cost: the cost model picks (§9.7)
    # Candidates at a request: (b) placeholders for the long outputs of closed segments, once per closed segment
    # (off by default), (c) an early summary (above topic_min_frac of t; `summary_at` says when the rent account
    # may take it), each written mechanically or, if listed in `writers`, as hybrid; (e) nothing. Above the hard
    # limit (`t` unless hard_t is set) nothing is chosen: the host compacts (Codex native compaction). (a),
    # shortening a new output, is decided as it enters (_entry_cost). Prices are in uncached-input-token units.
    # A placeholder batch: what this request costs more or less (the provider cache, core/cache_model.py), the
    # tokens saved on the following requests until the content would go anyway, the expected recoveries. A
    # summary: the rent paid since the last summary against the new cycle's uncached first request, the
    # recoveries and the writer (_summary_net). The best positive candidate runs; none = nothing changes.
    def _keys(self, sim, items):
        keys, sizes = [], []
        for x in items:
            keys.append(("v", hash(x.get("text"))) if x.get("virtual") else (x["id"], x.get("form", "full"), x.get("kept")))
            sizes.append(self._item_tokens(sim, x))
        return keys, sizes

    def _cache_for(self, sim):
        fixed = sim.request_tokens([])
        if self._cache is None:
            self._cache = Cache(fixed=fixed, rho=self.spec.cache_rho)
        self._cache.fixed = fixed
        return self._cache

    def _hard(self):
        return self.spec.hard_t or self.t

    def _size(self, sim):
        """Input tokens of the request about to be sent (sim.request_tokens: live, estimated from the bill)."""
        return sim.request_tokens()

    @staticmethod
    def _length(text):
        """Length of a text for the authors' content caps (output_keep_chars): characters."""
        return len(text)

    def _stay(self, size_now):
        """Requests a saving lasts: until the context would reach the hard limit anyway (then everything old goes),
        at the growth per request seen so far, or (cost, with summaries) until the next summary; at most
        `horizon`."""
        s = self.spec
        d = [b - a for a, b in zip(self._sizes, self._sizes[1:]) if b > a][-10:]
        growth = max(1.0, sum(d) / len(d)) if d else 1000.0
        stay = min(float(s.horizon), (self._hard() - size_now) / growth)
        if s.choose == "cost" and "summary" in s.candidates and self._sizes:
            # the next summary comes when the rent pays for the new cycle's first request: in the renewal model
            # a cycle lasts sqrt(2 F / (p g)) requests, F the first request of a cycle (uncached body), p the rent
            left = self._sizes[self._cycle] - (self._cache.fixed if self._cache else 0)
            first = (1 - s.cached_price) * max(left, 1)
            cycle = (2 * first / ((s.cached_price + s.length_penalty) * growth)) ** 0.5
            stay = min(stay, cycle - (len(self._sizes) - self._cycle))
        return max(1.0, stay)

    def _recovery(self, sim, lost, stay, after):
        """Expected cost of getting removed outputs back: P(needed within the stay) x (the output again, one more
        request on the new context, the model's extra work)."""
        s, n = self.spec, max(1, int(stay))
        return sum(self._use.p_reuse(x, n) * (sim.seg_size(x) + s.refetch_work + after * s.cached_price) for x in lost)

    def _net(self, sim, now, new, lost, writer_cost=0.0, recovery_scale=1.0):
        s, cache = self.spec, self._cache_for(sim)
        keys, sizes = now
        nkeys, nsizes = new
        removed = sum(sizes) - sum(nsizes)
        if removed <= 0:
            return None
        size_now = cache.fixed + sum(sizes)
        stay = self._stay(size_now)
        this = -cache.rebuild(keys, sizes, nkeys, nsizes, s.cached_price) + s.length_penalty * removed
        later = removed * (stay - 1) * (s.cached_price + s.length_penalty)
        risk = recovery_scale * self._recovery(sim, lost, stay, cache.fixed + sum(nsizes))
        return this + later - risk - writer_cost

    def _summary_net(self, sim, now, new, lost, writer_cost=0.0, recovery_scale=1.0):
        """A summary starts a new cycle; it pays once the rent of keeping the context paid since the last summary
        exceeds what the new cycle's first request loses (ski rental). Rent of a request is what it paid for the
        tokens above the size the summary leaves (at most what it removes). In the renewal model of a context
        growing g per request from a, this stops at S - a = sqrt(2 F g / p), the size of least cost per request;
        counting above the size left, not below the size now, stays right when placeholders shrink the context."""
        s, cache = self.spec, self._cache_for(sim)
        keys, sizes = now
        nkeys, nsizes = new
        removed = sum(sizes) - sum(nsizes)
        if removed <= 0 or removed < s.summary_min_frac * self.t:
            return None
        p = s.cached_price + s.length_penalty
        size_now = cache.fixed + sum(sizes)
        left = cache.fixed + sum(nsizes)
        rent = sum(p * min(removed, z - left) for z in self._sizes[self._cycle:] if z > left)
        first = cache.cost(nkeys, nsizes, s.cached_price) - s.cached_price * (cache.fixed + sum(nsizes))
        risk = recovery_scale * self._recovery(sim, lost, self._stay(size_now), cache.fixed + sum(nsizes))
        return rent - self._boundary_factor() * first - risk - writer_cost

    def _boundary_factor(self):
        """How much rent, in units of F, a summary needs now. The cost per request of a cycle of length L is
        F/L + p g L / 2; relative to its least value at L* it is (x + 1/x) / 2 with x = L/L*, and rent/F = x^2.
        Taking a topic change anywhere in rent in [0.5 F, 2 F] (x in [0.71, 1.41]) costs at most 6% of that part,
        and a summary at a topic change drops detail the new topic rarely needs."""
        s = self.spec
        if s.summary_at != "boundary":
            return 1.0
        at = self._topic_req is not None and len(self._sizes) - self._topic_req <= s.boundary_window
        return s.boundary_early if at else s.boundary_late

    def _placeholder_gain(self, sim, now, items):
        ids, ph = {x["id"] for x in items}, sim.placeholder_size("memid")
        keys, sizes = now
        nkeys, nsizes = [], []
        for x, k, z in zip(sim.sent(), keys, sizes):
            if not x.get("virtual") and x.get("id") in ids:
                nkeys.append((x["id"], "memid", ph)); nsizes.append(ph + 1)
            else:
                nkeys.append(k); nsizes.append(z)
        return self._net(sim, now, (nkeys, nsizes), items)

    def _plan_placeholder(self, sim, now):
        """(b), with the option of waiting: outputs of closed segments held only because the model used them
        recently become free within guard_active requests; one batch then costs one cache rebuild instead of
        several. Waiting w requests gives up w requests of the saving now. A batch is weighed only once a new
        segment closed since the last one: outputs close a segment at a time, and a batch per request edits the
        prompt on consecutive requests, where the provider keeps least (core/cache_model.py)."""
        s = self.spec
        if getattr(sim, "segid", 0) <= self._ph_seg:
            return None
        cands = self._closed_outputs(sim)
        if not cands:
            return None
        gain = self._placeholder_gain(sim, now, cands)
        if gain is None:
            return None
        ids = {x["id"] for x in cands}
        soon = [x for x in self._closed_outputs(sim, held=False) if x["id"] not in ids]
        req = self._use.req
        if soon and (self._wait_from is None or req - self._wait_from < s.guard_active):   # one wait, not a stall
            wait = max(1, max(s.guard_active - (req - self._use.last(x)) for x in soon))
            later = self._placeholder_gain(sim, now, cands + soon)
            removed = sum(sim.seg_size(x) for x in cands)
            if later is not None and later - wait * removed * s.cached_price > gain:
                if self._wait_from is None:
                    self._wait_from = req
                sim.M["v5_choose_wait"] += 1
                return None
        return dict(action="placeholder", gain=gain, items=cands)

    def _plan_summaries(self, sim, now, knobs):
        """The summary now, written mechanically and (if listed and the host can write) as hybrid."""
        s = self.spec
        res = self._compact(list(sim.sent()), knobs)
        if res is None:
            return []
        lost = [x for x in res["summary"]["members"]
                if x["seg"] == "out" and self._length(x.get("text") or "") > s.output_keep_chars]
        nkeys, nsizes = self._keys(sim, res["new"])
        plans = []
        hybrid = "hybrid" in s.writers and self._can_write(sim)
        gain = self._summary_net(sim, now, (nkeys, nsizes), lost)
        if gain is not None and ("mechanical" in s.writers or not hybrid):
            plans.append(dict(action="summary", writer="mechanical", gain=gain))
        if hybrid:
            at = next(i for i, x in enumerate(res["new"]) if x is res["summary"])
            hsizes = list(nsizes)
            hsizes[at] += s.writer_tokens
            members = sum(sim.seg_size(x) for x in res["summary"]["members"])
            if s.writer_model:
                members = min(members, s.writer_input_tokens)
            gain = self._summary_net(sim, now, (nkeys, hsizes), lost,
                                     writer_cost=members * s.writer_input_price + s.writer_tokens * s.writer_output_price,
                                     recovery_scale=s.state_recovery_factor)
            if gain is not None:
                plans.append(dict(action="summary", writer="hybrid", gain=gain))
        return plans

    def _observe(self, sim):
        """Before the hold: a host compaction starts a new cycle (the user messages it re-sent are not a new
        topic); a commit or a new user message marks where use counts again (release_at_topic)."""
        rebases = sim.M.get("history_rebases", 0)
        size = self._size(sim)
        host = rebases != self._rebases or (self._sizes and size < 0.6 * self._sizes[-1])
        if host:
            self._rebases = rebases
            self._cycle, self._topic_pending, self._topic_req, self._new_user = len(self._sizes), False, None, False
            self._commit = False
        why = self._topic_changed(sim)
        if why:
            self._topic_pending, self._topic_req = True, len(self._sizes)
            sim.M["v5_topic_" + why] += 1
        if why in ("user", "commit"):
            # this request's outputs (counted one request back: they arrived before the count advanced) and the two
            # requests before it, e.g. the test run that cleared the commit, still count as used after it
            self._topic_use = self._use.req - 3

    def _released(self, x):
        """Last used before the last commit or new user message (release_at_topic)."""
        return self.spec.release_at_topic and self._use.last(x) < self._topic_use

    def _finished(self, x):
        """A requirements document read whose documents a commit or tag named (release_at_topic)."""
        docs = self._spec_of.get(x.get("call_id"))
        if not (self.spec.release_at_topic and docs):
            return False
        after = {n for n, k in self._done.items() if k > self._read_at.get(x.get("call_id"), 0)}   # a later commit
        return all(_named(_doc_id(p), after) for p in docs)       # a read after its commit (rework) stays open

    def _choose(self, sim, r):
        s = self.spec
        knobs = s if not s.adaptive else dataclasses.replace(s, keep_recent=self._keep_recent())
        cache = self._cache_for(sim)
        size = self._size(sim)
        self._summarized = False
        try:
            now = self._keys(sim, sim.sent())
            plans = []
            p = self._plan_placeholder(sim, now) if "placeholder" in s.candidates else None
            if p is not None:
                plans.append(p)
            limit = self._hard()
            forced = size > limit
            if not forced and ("summary" in s.candidates and (self._topic_pending or s.summary_at != "topic")
                          and size > s.topic_min_frac * limit):
                plans += self._plan_summaries(sim, now, knobs)
            sim.M["v5_choices"] += 1
            if forced:
                self._deadline(sim, knobs)
                return
            best = max(plans, key=lambda p: p["gain"], default=None)
            if best is None or best["gain"] <= 0:
                sim.M["v5_choose_nothing"] += 1
                return
            if best["action"] == "placeholder":
                sim.to_placeholder(best["items"], "memid")
                self._audit(sim, best["items"])
                for x in best["items"]:
                    self._forget(x)
                if s.reread_use:
                    self._gone |= {x["id"] for x in best["items"]}
                self._compacted, self._wait_from, self._ph_seg = True, None, getattr(sim, "segid", 0)
                sim.M["v5_segment"] += 1
                sim.M["v5_choose_placeholder"] += 1
                return
            done = self._summarize(sim, knobs, best["writer"])
            if done:
                self._compacted, self._topic_pending, self._summarized = True, False, True
                sim.M["v5_topic"] += 1
                sim.M["v5_choose_summary_" + done] += 1
        finally:
            keys, sizes = self._keys(sim, sim.sent())
            cache.record(keys, sizes)
            self._sizes.append(cache.fixed + sum(sizes))
            if self._summarized:
                self._cycle = len(self._sizes) - 1
            if self._compacted:
                self._after, self._since = self._sizes[-1], 0

    def _summarize(self, sim, knobs, writer):
        """One summary with this writer; a hybrid whose writer fails falls back to the mechanical lines. Returns the
        writer that wrote it, or None when nothing changed."""
        self.knobs = knobs
        present = {x["id"] for x in sim.ctx if x["seg"] == "out"}
        lost = self._lost(sim, knobs)
        try:
            if writer == "hybrid":
                if self._model_compaction(sim, "hybrid"):
                    return "hybrid"
                sim.M["v5_writer_fallback"] += 1
            state = "\n\n".join(p for p in (_state_of(x.get("text") or "", written_only=True)
                                             for x in sim.ctx if x["seg"] == "summary") if p)
            if not self._chain(sim, knobs, force=True):
                return None
            if state:                             # the state written so far survives a mechanical summary
                new = next(x for x in reversed(sim.ctx) if x["seg"] == "summary")
                body = (new.get("text") or "")[len(SUMMARY_HEADER):].strip()
                new["text"] = SUMMARY_HEADER + "\n\n[state]\n" + state + (STEPS_MARK + body if body else "")
                new["size"] = sim.message_tokens(new["text"])
            return "mechanical"
        finally:
            self.knobs = self.spec
            gone = present - {x["id"] for x in sim.ctx if x["seg"] == "out"}
            if gone:
                if self.spec.reread_use:
                    self._gone |= gone
                self._log_summary(sim, writer, lost)

    def _lost(self, sim, knobs):
        """The long outputs a summary now would remove, and the requests its saving lasts (for the decision log)."""
        res = self._compact(list(sim.sent()), knobs)
        size = self._size(sim)
        if res is None:
            return [], 1.0, size
        lost = [x for x in res["summary"]["members"]
                if x["seg"] == "out" and self._length(x.get("text") or "") > self.spec.output_keep_chars]
        return lost, self._stay(size), size

    def _log_summary(self, sim, writer, lost):
        """What the cost model expected of this summary, for comparison with the re-reads that follow
        (records: method_decision; op_reread_removed counts the re-reads of removed outputs)."""
        items, stay, size = lost
        n = max(1, int(stay))
        self.last_decision = dict(
            request=self._r, action="summary", writer=writer, tokens_before=round(size),
            lost_outputs=len(items), stay=round(stay, 1),
            expected_rereads=round(sum(self._use.p_reuse(x, n) for x in items), 3),
            expected_reread_tokens=round(sum(self._use.p_reuse(x, n) * sim.seg_size(x) for x in items)),
            reuse_observations=sum(self._use.n), reuse_hits=sum(self._use.k))

    def _deadline(self, sim, knobs):
        """Above t our own summary runs (the deadline every method's trigger has), written as the spec says; the
        host (Codex native compaction) is left only what a summary cannot remove: less than summary_min_frac of
        t, or a summary on the last request that left the context above t."""
        s = self.spec
        stuck = self._sizes and self._cycle == len(self._sizes) - 1 and self._sizes[-1] > self._hard()
        res = None if (s.at_hard == "host" or stuck) else self._compact(list(sim.sent()), knobs)
        if res is not None:
            _, before = self._keys(sim, sim.sent())
            _, after = self._keys(sim, res["new"])
            if sum(before) - sum(after) >= s.summary_min_frac * self.t:
                writer = "hybrid" if "hybrid" in s.writers and self._can_write(sim) else "mechanical"
                if self._summarize(sim, knobs, writer):
                    self._compacted, self._topic_pending, self._summarized = True, False, True
                    sim.M["v5_choose_deadline"] += 1
                    return
        sim.M["v5_choose_host"] += 1

    def _entry_cost(self, sim, item):
        """(a) A new long output: shortened as it enters only if that pays (no cache cost, it was never sent; the
        risk is needing it back). Rule R1 holds: only outputs above guard_oversize_frac of t."""
        s = self.spec
        if (sim.seg_size(item) <= s.guard_oversize_frac * self.t or (s.protect_spec and item.get("kind") == "spec")
                or set(item.get("res") or ()) & self._sticky):
            return
        saved_item, saved_m = dict(item), collections.Counter(sim.M)
        before = sim.seg_size(item)
        budget = s.entry_frac * self.t
        if content_kind(item) in ("test", "search"):
            sim.structure(item)
        if sim.seg_size(item) > budget:
            sim.truncate(item, budget)
        removed = before - sim.seg_size(item)
        size_now = self._size(sim)
        stay = self._stay(size_now)
        gain = removed * (1 + (stay - 1) * (s.cached_price + s.length_penalty)) \
            - self._use.p_reuse(item, max(1, int(stay))) * (before + s.refetch_work + size_now * s.cached_price)
        if removed <= 0 or gain <= 0:
            item.clear(); item.update(saved_item)
            sim.M.clear(); sim.M.update(saved_m)
            return
        self._forget(item)
        sim.M["v5_entry"] += 1
        self._audit(sim, [item])

    # ------------------------------------------------------------------ scope and previous replacements
    def _compact(self, working, k):
        first = next((i for i, x in enumerate(working) if is_model(x)), None)
        if first is None:
            return None
        head = j = first
        while j > 0 and (is_summary(working[j - 1]) or (k.protect_user and working[j - 1]["seg"] == "msg"
                                                          and working[j - 1].get("role") == "user")):
            j -= 1                                # earlier summaries, past user messages kept after them: a new
            if is_summary(working[j]):            # summary replaces them instead of piling up after the messages
                head = j
        turns = group_turns(working[head:])
        keep = max(0, k.keep_recent)
        if len(turns) <= keep:
            return None
        old = [x for t in turns[:len(turns) - keep] for x in t]
        media = {x.get("call_id") for x in old if x.get("has_media") and x.get("call_id")}

        def stays(x):                             # the host contract keeps media and the call it answers
            return (x.get("protected") or (k.protect_user and x["seg"] == "msg" and x.get("role") == "user")
                    or (k.protect_spec and x.get("kind") == "spec" and (self.spec.choose != "cost"
                                                                         or x.get("id") in self._held))
                    or x.get("id") in self._keep
                    or x.get("id") in self._held or x.get("has_media") or x.get("call_id") in media - {None})
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
    def _model_compaction(self, sim, writer=None):
        writer = writer or self.spec.writer
        res = self._compact(list(sim.sent()), self.knobs)
        if res is None:
            return False
        members = [x for x in res["summary"]["members"] if x in sim.ctx
                   and not (self.spec.previous == "drop" and (x.get("cliff") or is_summary(x)))]
        if not members:
            return False
        state = sim.summarize_segment(members, size=self.spec.writer_tokens, guidance=STATE_GUIDANCE, billed=True,
                                      cliff=True) if writer == "llm" else None
        if writer == "llm":
            if state is None:
                sim.M["v5_writer_failed"] += 1
                return False
            state["text"] = SUMMARY_HEADER + "\n\n" + state["text"]
            state["size"] = sim.message_tokens(state["text"])
            return True
        # hybrid: a model-written state ahead of the mechanical lines. With a writer model the earlier summaries
        # are replaced too (their state is carried forward by the writer), as the mechanical chain replaces them.
        if self.spec.writer_model:
            members = [x for x in res["summary"]["members"] if x in sim.ctx]
        written, keep = self._write_state(sim, members)
        if written is None:
            self._writer_fails += 1
            sim.M["v5_writer_failed"] += 1
            if self._writer_fails >= 2:
                self._writer_retry = self._use.req + WRITER_RETRY
                sim.M["v5_writer_paused"] += 1
            return False
        self._writer_fails = 0
        kept = self._writer_kept(sim, members, keep)
        if kept:                                  # not recoverable from the environment: stays verbatim
            members = [x for x in members if x["id"] not in kept]
            sim.M["v5_writer_kept"] += len(kept)
        mech = res["summary"]["text"][len(SUMMARY_HEADER):].strip()
        text = SUMMARY_HEADER + "\n\n[state]\n" + written + (STEPS_MARK + mech if mech else "")
        billed = not self.spec.writer_model       # a writer model's call is billed on its own route
        new = sim.summarize_segment(members, text=text, billed=billed, model_written=not billed, cliff=True)
        if new is not None:
            new["size"] = sim.message_tokens(text)
            sim.M["v5_writer_" + (self.spec.writer_model or "host")] += 1
        return new is not None

    def _can_write(self, sim):
        """A writer can answer: a summary service is attached (the proxy attaches one per request when the method
        asks, requires_summary), and the last two writer calls did not both fail within WRITER_RETRY requests."""
        if getattr(sim, "summarizer", None) is None or (self._writer_fails >= 2 and self._use.req < self._writer_retry):
            return False
        return hasattr(sim, "model_text") if self.spec.writer_model else hasattr(sim, "_summary_text")

    def _writer_kept(self, sim, members, keep):
        """Ids of the outputs (with their calls) the writer asked to keep, newest first within writer_keep_frac."""
        s = self.spec
        if not (s.writer_keep_frac and keep):
            return set()
        wanted = set()
        for k in keep:
            try:
                wanted.add(int(k))
            except (TypeError, ValueError):
                continue
        outs = [x for x in members if x["seg"] == "out" and x.get("id") in wanted]
        calls = {x.get("call_id"): x["id"] for x in members if x["seg"] == "call" and x.get("call_id")}
        kept, used = set(), 0
        for x in reversed(outs):
            size = sim.seg_size(x)
            if used + size > s.writer_keep_frac * self.t:
                continue
            used += size
            kept.add(x["id"])
            if x.get("call_id") in calls:
                kept.add(calls[x["call_id"]])
        return kept

    def _write_state(self, sim, members):
        """(task state, #IDs to keep) of a hybrid summary; state None when the writer fails: the host's summary
        service, or `writer_model` given the earlier state and the removed steps (_writer_input)."""
        s = self.spec
        if not s.writer_model:
            return (sim._summary_text(members, "segment", s.writer_tokens, STATE_GUIDANCE)
                    if hasattr(sim, "_summary_text") else None), []
        earlier = [x for x in members if x.get("cliff") or is_summary(x)]
        steps = [x for x in members if not (x.get("cliff") or is_summary(x))]
        previous = "\n\n".join(p for p in (_state_of(x.get("text") or "") for x in earlier) if p)
        body = json.dumps(dict(previous_state=previous, steps=self._writer_input(sim, steps)), ensure_ascii=False)
        limit = f"{s.writer_tokens} tokens"
        answer = sim.model_text(STATE_SYSTEM.replace("LIMIT", limit), body,
                                purpose="infinite_context_compression", model=s.writer_model)
        try:
            reply = json.loads(answer, strict=False)   # DeepSeek's JSON mode leaves raw line breaks in strings
        except (TypeError, ValueError):
            return None, []
        if not isinstance(reply, dict):
            return None, []
        keep = reply.pop("keep", None) if "state" in reply else None
        state = _flat(reply.get("state", reply))
        if not state.strip():
            return None, []
        state = _head(state.strip(), s.writer_tokens * 3 // 2)   # the writer may overrun its target by half
        return state, keep if isinstance(keep, list) else []

    def _writer_input(self, sim, steps):
        """The removed steps as the writer reads them, oldest first: messages, one-line calls, outputs cut to their
        head and tail with the archive number (tokens: a message's first 750, a call's first 100, an output's first
        250 and last 125). Beyond writer_input_tokens the oldest fifth and the newest steps stay and the middle is left
        out, said so in its place."""
        s, lines = self.spec, []
        size, cap = _count, s.writer_input_tokens
        for x in steps:
            text = (x.get("text") or "").strip()
            if x["seg"] == "msg" and text:
                lines.append(f"{x.get('role') or 'message'}: {_head(text, 750)}")
            elif x["seg"] == "call":
                lines.append(f"[{x.get('name') or 'call'}] {_head(text, 100)}")
            elif x["seg"] == "out" and text:
                cut = _head_tail(text, 250, 125)
                stale = " (stale: superseded by a later read or edit)" if self._use.superseded(x) else ""
                lines.append(f"output #{x.get('id')}{stale}: {cut}")
        sizes = [size(l) for l in lines]
        if sum(sizes) <= cap:
            return lines
        head, used = [], 0
        for l, n in zip(lines, sizes):
            if used + n > cap // 5:
                break
            head.append(l)
            used += n
        tail = []
        for l, n in zip(reversed(lines[len(head):]), reversed(sizes[len(head):])):
            if used + n > cap:
                break
            tail.append(l)
            used += n
        left_out = len(lines) - len(head) - len(tail)
        return head + [f"[... {left_out} steps left out ...]"] + tail[::-1]

    # ------------------------------------------------------------------ recall
    def call_tool(self, name, args):
        from ctxpress import settings
        from ctxpress.methods.infinite_archive import call
        try:
            return json.dumps(call(name, args, settings.store_dir()), ensure_ascii=False), False
        except (OSError, ValueError, KeyError, TypeError) as error:
            return json.dumps(dict(error=str(error)), ensure_ascii=False), True


_FAILURE = re.compile(r"FAIL|Error|error[:\[]|Traceback|panic|assert|Exception|^E\s", re.M)


def _failures(text, limit=600):
    """Failure lines from the cut middle of an output (a test log's reason often sits there)."""
    found = [l.strip() for l in text.splitlines() if _FAILURE.search(l)]
    if not found:
        return ""
    return "[failure lines from the cut part]\n" + truncate("\n".join(found), limit) + "\n"


_count = tokens.count


def _head(text, n):
    return tokens.head(text, n)


def _head_tail(text, head, tail):
    """An output cut to its first `head` and last `tail` tokens, the failure lines between them kept."""
    enc = tokens.encoding()
    ids = enc.encode(text, disallowed_special=())
    if len(ids) <= head + tail:
        return text
    first, middle, last = enc.decode(ids[:head]), enc.decode(ids[head:-tail]), enc.decode(ids[-tail:])
    return first + f"\n... [{len(ids) - head - tail} tokens cut] ...\n" + _failures(middle) + last


def _flat(v):
    """A writer's JSON state as text: strings as they are, sections as 'NAME:' blocks, lists as lines."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return "\n\n".join(f"{k}:\n{_flat(x)}".rstrip() for k, x in v.items())
    if isinstance(v, list):
        return "\n".join("- " + _flat(x) for x in v)
    return "" if v is None else str(v)


def _state_of(text, written_only=False):
    """What an earlier summary carries into the next state: its written state, else (unless written_only) the
    newest 8000 characters of its mechanical lines."""
    body = text[len(SUMMARY_HEADER):].strip() if text.startswith(SUMMARY_HEADER) else text.strip()
    if body.startswith("[state]\n"):
        return body[len("[state]\n"):].split(STEPS_MARK, 1)[0].strip()
    return "" if written_only else body[-8000:]


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
        self.arrived, self.res = {}, {}   # output id -> request of arrival / the paths it was read from
        self.edited = {}                  # path -> request of its last edit
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
        a = self.req - self.last(x)
        b = max([j for j, age in enumerate(AGE_BUCKETS) if age <= a] or [0])
        prior = AGE_PRIOR * AGE_DECAY ** b
        p = (self.k[b] + prior * AGE_N0) / (self.n[b] + AGE_N0)   # within the fixed resolution horizon
        if horizon == self.horizon:
            return p
        return 1 - (1 - min(p, 0.999)) ** (horizon / self.horizon)   # constant hazard over the horizon asked

    def superseded(self, x):
        return x.get("id") in self.newer

    def add(self, x):
        if x["seg"] == "out" and x.get("id") is not None:
            i = x["id"]
            self.born[i], self.used[i] = self.model, self.req
            self.arrived[i], self.res[i] = self.req, set(x.get("res") or ())
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

    def edit(self, paths):
        for p in paths:
            self.edited[p] = self.req

    def read_again(self, cmd, paths):
        """A call reading a source earlier outputs came from: the same command, or (`paths`: a read's files) a file
        they were read from, not edited since they arrived. Those outputs were needed again; returns their ids."""
        ids = set()
        old = self.source.get(("cmd", cmd)) if cmd else None
        if old is not None:
            ids.add(old)
        if len(paths) <= self.max_paths:
            for p in paths:
                ids |= {i for i in self.paths.get(p, ()) if i not in self.broad}
        ids = {i for i in ids if not any(self.edited.get(p, -1) >= self.arrived.get(i, 0) for p in self.res.get(i, ()))}
        for i in ids:
            self.used[i] = self.req
        return ids

    def last(self, x):
        return self.used.get(x.get("id"), -1)

    def active(self, x, window):
        return self.req - self.last(x) <= window


_SPEC_PATH = re.compile(r"(^|/)srs/|SRS\.md$|(^|/)TASK_QUEUE\.md$|(^|/)requirements?\.md$", re.I)
_QUEUE_PATH = re.compile(r"(^|/)TASK_QUEUE\.md$", re.I)
_GIT = r"\bgit(?:[ \t]+-[cC][ \t]+\S+)*[ \t]+"   # git, its -C <dir> / -c <key=value> options
_GIT_TAG = re.compile(_GIT + r"tag[ \t]+((?:-\S+[ \t]+)*)['\"]?([A-Za-z0-9_][^\s'\"&;|)<>]*)")
_GIT_COMMIT = re.compile(_GIT + r"commit\b")
_COMMIT_MSG = re.compile(_GIT + r"commit\b[^&;|\n]*?\s-[a-z]*m\s*(['\"])(.+?)\1")
_TAG_FLAGS = {"-a", "-s", "-f", "--annotate", "--sign", "--force"}


def _committed(text):
    """Names a call's commits and tags record (tag names, words of commit messages), or None: no commit or tag
    (listing or deleting tags is none)."""
    from ctxpress.core import trace
    s = trace.unescape(text or "")
    names = {m.group(2) for m in _GIT_TAG.finditer(s)    # a bare number is a redirect: `git tag 2>/dev/null` lists
             if set(m.group(1).split()) <= _TAG_FLAGS and not m.group(2).isdigit()}
    names |= {w.strip(".,:;!()") for m in _COMMIT_MSG.finditer(s) for w in m.group(2).split()}
    return names if names or _GIT_COMMIT.search(s) else None


def _doc_id(path):
    """What names a requirements document: srs/M12.2_SRS.md -> M12.2, srs/M3/SRS.md -> M3, docs/req.md -> req."""
    parts = path.rstrip("/").split("/")
    name = parts[-1]
    if re.fullmatch(r"(SRS|requirements?)\.md", name, re.I) and len(parts) > 1:
        return parts[-2]
    return re.sub(r"[_-]?(SRS)?\.md$", "", name, flags=re.I)


def _named(doc, names):
    """A commit or tag names the document: a name equal to it or ending in -, _ or / and it."""
    return len(doc) >= 2 and any(n == doc or n.endswith(("-" + doc, "_" + doc, "/" + doc)) for n in names)


def _spec_paths(text):
    """Requirements documents a call reads: a read command naming a strong requirements path (srs/, *SRS.md,
    TASK_QUEUE.md), even when the same command also reads code (trace.classify calls that 'read')."""
    from ctxpress.core import trace
    s = trace.unescape(text or "")
    if not trace.READ.search(s):
        return set()
    return {p for p in (trace.norm(m.group(1)) for m in trace.PATH.finditer(s)) if _SPEC_PATH.search(p)}


def _command(text):
    return " ".join((text or "").split())


def _tools():
    from ctxpress.methods.infinite_archive import TOOLS
    return TOOLS
