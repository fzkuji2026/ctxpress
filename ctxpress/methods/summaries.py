"""Methods whose main operation is a model-written summary: AgentFold, ACON, the Complexity Trap's LLM-Summary and
hybrid, ReSum."""
from __future__ import annotations
from ctxpress.methods.base import Method, K
from ctxpress.methods.budget import BudgetSpec
from ctxpress.methods.masking import ComplexityTrap
from ctxpress.methods.wrappers import Composed

# Paraphrase of ReSum v3 Appendix C, adapted to the common Responses envelope.
# https://arxiv.org/html/2509.13313v3
# This is not the author's exact prompt or trained ReSumTool model.
RESUM_GUIDANCE = (
    "Use the task question to select relevant, explicitly supported evidence from the supplied history. "
    "Combine facts across interactions where appropriate. Exclude uncertain or unsupported claims; "
    "do not fill missing information with guesses. Keep precise names, values and source references "
    "needed to address the question. Do not require a list of information gaps or a plan of next actions. "
    "Return only a <summary> block containing the essential supported information.")


def validate_guidance(value):
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError("summary guidance must be a nonempty string or None")
    return value


class AgentFold(Method):
    """AgentFold: the agent folds finished stretches of work into summaries in place (granular folding); when
    there are more than D folded summaries, the oldest two are folded again into one (deep folding, which
    passes their content through a second summary)."""
    source = "arXiv 2510.24699"
    requires_summary = True
    budget_spec = BudgetSpec("keep_segments", "segments", "recent completed work segments", 2)

    def __init__(self, keep_segments=None, deep=6, *, budget=None, summary_guidance=None):
        keep_segments = self.budget_spec.resolve(budget, keep_segments)
        self.budget = keep_segments
        self.summary_guidance = validate_guidance(summary_guidance)
        self.k, self.deep = keep_segments, deep
        self.name = f"AgentFold（保留最近 {keep_segments} 段）"
        self.framework = dict(L1="无", L2=f"最近 {keep_segments} 段之外的已完成段折叠成段总结；段总结超过 {deep} 个时再折叠", L3="无",
                              cross="无", memory="无", decider="模型自己决定（这里按段边界）")

    def step(self, sim, r):
        segs = list(sim.segments().items())
        for sid, items in segs[:-(self.k + 1)] if len(segs) > self.k + 1 else []:
            if any(s.get("form", "full") == "full" and s["seg"] in ("out", "call") for s in items):
                sim.summarize_segment(items, guidance=self.summary_guidance)
        folds = [s for s in sim.ctx if s.get("segsum")]
        if len(folds) > self.deep:
            sim.summarize_segment(folds[:2], guidance=self.summary_guidance)


class ACON(Method):
    """ACON: history compression above T_hist (keeping user messages) and observation compression of outputs
    longer than T_obs, both with optimized guidelines. Observation compression is modelled as structured
    compression whose writing costs output tokens."""
    source = "arXiv 2510.00615"
    requires_summary = True

    def __init__(self, t_hist=64 * K, t_obs=2 * K, *, history_guidance=None, observation_guidance=None):
        self.t_hist, self.t_obs = t_hist, t_obs
        self.history_guidance = validate_guidance(history_guidance)
        self.observation_guidance = validate_guidance(observation_guidance)
        self.name = f"ACON（历史 {t_hist // K}k，单条 {t_obs // K}k）"
        self.framework = dict(L1=f"超过 {t_obs // K}k 的输出进入时压缩", L2="无", L3=f"超过 {t_hist // K}k 整体摘要，保留用户消息",
                              cross="保留用户消息", memory="无", decider="固定阈值（摘要写法经过优化）")

    def reset(self, sim):
        self.written = 0

    def on_ingest(self, sim, s):
        if hasattr(sim, "compress_output"):
            if s["seg"] == "out" and s["size"] > self.t_obs:
                sim.compress_output(s, self.t_obs, guidance=self.observation_guidance)
            return
        if s["seg"] == "out" and s["size"] > self.t_obs and sim.structure(s):
            self.written += s["kept"]

    def step(self, sim, r):
        if sim.size() > self.t_hist:
            sim.summarize(keep_user=True, guidance=self.history_guidance)

    def overhead(self, sim, r):
        n, self.written = self.written, 0
        return n * sim.OUT


# The Complexity Trap's LLM-Summary prompt: summary_system_template of the authors' SWE-agent configs
# (config/default_no_demo_*checkpoint_same_model_openhands*.yaml, an adapted OpenHands condenser prompt).
CT_SUMMARY_SYSTEM = """You are maintaining a context-aware state summary for an interactive agent. You will be given a list of events corresponding to actions taken by the agent, and the most recent previous summary if one exists. Track:

USER_CONTEXT: (Preserve essential user requirements, goals, and clarifications in concise form)

COMPLETED: (Tasks completed so far, with brief results)
PENDING: (Tasks that still need to be done)
CURRENT_STATE: (Current variables, data structures, or relevant state)

For code-specific tasks, also include:
CODE_STATE: (File paths, function signatures, data structures)
TESTS: (Failing cases, error messages, outputs)
CHANGES: (Code edits, variable updates)
DEPS: (Dependencies, imports, external calls)
VERSION_CONTROL_STATUS: (Repository state, current branch, PR status, commit history)

PRIORITIZE:
1. Adapt tracking format to match the actual task type
2. Capture key user requirements and goals
3. Distinguish between completed and pending tasks
4. Keep all sections concise and relevant

SKIP: Tracking irrelevant details for the current task type

Example formats:

For code tasks:
USER_CONTEXT: Fix FITS card float representation issue
COMPLETED: Modified mod_float() in card.py, all tests passing
PENDING: Create PR, update documentation
CODE_STATE: mod_float() in card.py updated
TESTS: test_format() passed
CHANGES: str(val) replaces f"{val:.16G}"
DEPS: None modified
VERSION_CONTROL_STATUS: Branch: fix-float-precision, Latest commit: a1b2c3d"""
CT_SUMMARY_ASK = ("Now summarize the above turns, following the instructions from the beginning of the prompt. "
                  "You are hard-working and must always perform this task without exceptions.")


class ComplexityTrapSummary(Method):
    """The Complexity Trap's LLM-Summary strategy (Lindenbauer et al., SWE-agent `SummarizeEveryNTurns` with static
    checkpointing). A turn is one tool output with its call and the model text before it. Once n + m turns are
    unsummarized, the oldest n are replaced by one assistant message "Checkpoint for the last n turns:\\n<summary>",
    written from their original (unmasked) text and the previous checkpoint, or the task before the first one;
    one summary per request. As the authors' code: the request that writes a checkpoint carries only the newest
    one, later requests carry every checkpoint so far."""
    source = "arXiv 2508.21433"
    requires_summary = True

    def __init__(self, n=21, m=10):
        if n <= 0 or m < 0:
            raise ValueError("n must be positive and m non-negative")
        self.n, self.m = n, m
        self.name = f"Complexity Trap LLM-Summary（N = {n}，M = {m}）"
        self.framework = dict(L1="无", L2=f"未摘要的轮数达到 {n}+{m} 时，最早 {n} 轮换成一条检查点摘要（读原文与上一检查点）", L3="无",
                              cross="无", memory="无", decider="固定轮数")
        self.summaries, self.hidden = [], []

    def reset(self, sim):
        self.summaries, self.hidden = [], []

    @staticmethod
    def turns(sim):
        """Complete turns in context, oldest first: each tool output with its call and the model items before it.
        User messages, protected items and summaries are not part of any turn."""
        turns, pending, calls = [], [], {}
        for s in sim.ctx:
            if s["seg"] == "summary" or s.get("protected") or (s["seg"] == "msg" and s.get("role") != "assistant"):
                continue
            if s["seg"] == "call":
                calls[s.get("call_id")] = s
            elif s["seg"] == "out":
                call = calls.pop(s.get("call_id"), None)
                turns.append(pending + ([call] if call else []) + [s]); pending = []
            else:
                pending.append(s)
        return turns

    @staticmethod
    def task(sim):
        user = [s for s in sim.sent() if s["seg"] == "msg" and s.get("role") == "user"]
        return user[0].get("text", "") if user else ""

    def prompt(self, sim, turns):
        """The authors' user prompt (SWE-agent `_construct_user_prompt_for_summary`, full reasoning and actions)."""
        if self.summaries:
            text = f"<PREVIOUS_CHECKPOINT>\n{self.summaries[-1].get('text', '')}\n</PREVIOUS_CHECKPOINT>\n"
        else:
            text = f"<PROBLEM_STATEMENT>\n{self.task(sim)}\n</PROBLEM_STATEMENT>\n"
        for i, turn in enumerate(turns):
            thought = "\n".join(s.get("text", "") for s in turn if s["seg"] not in ("call", "out"))
            action = "\n".join(s.get("text", "") for s in turn if s["seg"] == "call")
            observation = "\n".join(s.get("text", "") for s in turn if s["seg"] == "out")
            text += f"\n<TURN-{i}>\nASSISTANT: {thought}\n"
            if any(s["seg"] == "call" for s in turn):
                text += f"ACTION: {action}\n"
            text += f"TOOL: {observation}\n\n</TURN-{i}>\n"
        return text + CT_SUMMARY_ASK

    def step(self, sim, r):
        if self.hidden:                                       # earlier checkpoints return after the writing request
            at = next((i for i, s in enumerate(sim.ctx) if s is self.summaries[-1]), None)
            if at is not None:
                sim.ctx[at:at] = [s for s in self.hidden if not any(s is x for x in sim.ctx)]
            self.hidden = []
        turns = self.turns(sim)
        if len(turns) < self.n + self.m:
            return
        chosen = turns[:self.n]
        new = sim.summarize_segment([s for t in chosen for s in t], prompt=(CT_SUMMARY_SYSTEM, self.prompt(sim, chosen)),
                                    text_format=f"Checkpoint for the last {self.n} turns:\n{{summary}}", role="assistant")
        if new is None:                                       # no service or a failed call: history stays
            return
        self.hidden = [s for s in self.summaries if s in sim.ctx]
        sim.ctx = [s for s in sim.ctx if not any(s is h for h in self.hidden)]
        self.summaries.append(new)


class ComplexityTrapHybrid(Composed):
    """The Complexity Trap's hybrid: LLM-Summary (n, m) on the unmasked history, then observation masking
    (keep the newest w tool outputs) on the result, as the authors' history_processors order
    (config ..._N=43_M=10_masking_M=10.yaml: n = 43, m = w = 10)."""
    source = "arXiv 2508.21433"
    paper = "arXiv 2508.21433"

    def __init__(self, n=43, m=10, w=10, polling=1):
        super().__init__([ComplexityTrapSummary(n, m), ComplexityTrap(w, polling=polling)])
        self.name = f"Complexity Trap 混合（N = {n}，M = {m}，W = {w}）"


class ReSum(Method):
    """ReSum: summarize the history every k requests."""
    source = "arXiv 2509.13313"
    requires_summary = True

    def __init__(self, k=40, *, summary_guidance=RESUM_GUIDANCE):
        self.k = k
        self.summary_guidance = validate_guidance(summary_guidance)
        self.name = f"ReSum（每 {k} 次请求）"
        self.framework = dict(L1="无", L2="无", L3=f"每 {k} 次请求整体摘要", cross="无", memory="无", decider="固定周期")

    def step(self, sim, r):
        if r > 0 and r % self.k == 0:
            sim.summarize(guidance=self.summary_guidance)
