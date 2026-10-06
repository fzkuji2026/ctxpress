"""AgentDiet (Xiao et al., FSE 2026, arXiv 2509.23586): inference-time trajectory reduction by a reflection model."""
from __future__ import annotations
from ctxpress.methods.base import Method

# trae_agent/agents/traj_analyzer.py SYS_PROMPT (artifact, figshare 10.6084/m9.figshare.30073654).
AGENTDIET_SYSTEM = """You will analyze and compress a given step in a trajectory of an AI agent solving a software bug.

In the trajectory, each step is marked in <step id="..."></step>.
The agent will think in <think>, call external tools as marked in <call tool="..."></call>. Its result is marked in <result></result> within the <step> tag.

Your job is to compress the text within the given id to avoid harming efficiency, typically shortening it to 20%-50% of the original length.
Meanwhile, keep the compressed text useful such that you are able to continue the trajectory as close as the original path.

- You should ONLY remove redundant texts, which are either irrelevant to future steps or duplicated by other texts in the trajectory.
- Replace the text to remove to "..." and a short takeaway, e.g. "... (same as the content below)".
- You should keep the original structure unchanged, e.g., XML tags, Python indentation and line numbers.
- Again, keep useful details in the original content unchanged, e.g., XML tags, Python indentation and line numbers.

Typical examples:
- If the step opens a huge file but only one part is necessary for future steps, replace other parts to "... (unrelated function XXX, YYY)".
- If the step runs a verbose test script and everything goes fine, replace the verbose part to "... (expected output)".
- If the step uses str_replace_editor to modify a file and the content can be inferred by the content after it, replace the tool call argument to "... (see results below)".

You should only process the text within the <step> tag with the given id. STOP OUTPUT IMMEDIATELY AFTER </step>."""
ERASED = "(System reminder: compressed for better efficiency) "


class AgentDiet(Method):
    """AgentDiet: after each agent step s, a reflection model rewrites step s - a (a = 2) if it is longer than
    `threshold` tokens, seeing steps s - a - b .. s (b = 1; step -1 is the task) serialized as
    <step id><think/talk>, <call tool>, <result></step>; the rewrite is kept if it saves at least 400 tokens or
    20%, and the whole step (model text, calls, outputs) becomes one assistant message "(System reminder: compressed
    for better efficiency) ...". A step is one model turn and the outputs of its calls; user messages stay outside
    steps. Later prompts show a rewritten step in its original form, as the authors' MessageManager does.
    One reflection per request, as one per agent step in the original: when a session starts with earlier history
    (a resumed boundary), only step s - a of the first request and the steps after it are considered."""
    source = "arXiv 2509.23586"
    requires_summary = True

    def __init__(self, a=2, b=1, threshold=500, reflect_model=None, bypass_filter=None, min_saving=400, max_ratio=0.8):
        if a < 0 or b < 0 or threshold < 0:
            raise ValueError("a, b and threshold must be non-negative")
        self.a, self.b, self.threshold, self.reflect_model = a, b, threshold, reflect_model
        # The authors switch the wording for GPT-5 reflection models ('think' -> 'talk', 'agent' -> 'engineer').
        self.bypass = bool(reflect_model and "gpt-5-" in reflect_model) if bypass_filter is None else bool(bypass_filter)
        self.min_saving, self.max_ratio = min_saving, max_ratio
        self.name = f"AgentDiet（a = {a}，b = {b}，θ = {threshold}）"
        self.framework = dict(L1="无", L2=f"每步之后，由反思模型压缩第 s−{a} 步（超过 {threshold} token 时），整步换成压缩文本",
                              L3="无", cross="无", memory="无", decider="反思模型（固定窗口与阈值）")
        self.done = set()

    def reset(self, sim):
        self.done = set()

    @staticmethod
    def steps(sim):
        """Agent steps in context, oldest first: the items of each model turn (a rewritten step is its summary)."""
        out = {}
        for s in sim.ctx:
            if s.get("turn", 0) <= 0 or s.get("protected") or (s["seg"] == "msg" and s.get("role") != "assistant"):
                continue
            out.setdefault(s["turn"], []).append(s)
        return [out[k] for k in sorted(out)]

    def serialize(self, sim, step, idx):
        """MessageManager.extract_step_into_traj: model text, then its calls, then each result."""
        if step is None:
            user = [s for s in sim.sent() if s["seg"] == "msg" and s.get("role") == "user"]
            return user[0].get("text", "") if user else ""
        if len(step) == 1 and step[0].get("diet_original") is not None:
            # as the authors' code: the stored original (itself a <step> block) inside another <step> wrapper
            return f'<step id="{idx}">\n{step[0]["diet_original"]}\n</step>'
        tag = "talk" if self.bypass else "think"
        out = [f'<step id="{idx}">']
        think = "".join(s.get("text", "") for s in step if s["seg"] in ("reason", "msg"))
        if think.strip():
            out.append(f"<{tag}>{think}</{tag}>")
        for s in step:
            if s["seg"] == "call":
                out.append(f'<call tool="{s.get("name") or "tool"}">{(s.get("args") or s.get("text", "")).strip()}</call>')
        for s in step:
            if s["seg"] == "out" and s.get("text", "").strip():
                out.append(f"<result>{s.get('text', '')}</result>")
        return "\n".join(out + ["</step>"])

    def tokens(self, sim, text):
        return sim.tokens(text) if hasattr(sim, "tokens") else len(text) // 4 + 4

    def step(self, sim, r):
        steps = self.steps(sim)
        if len(steps) < self.a + self.b:
            return
        idx = len(steps) - 1 - self.a
        if idx < 0 or idx in self.done:
            return
        self.done.add(idx)
        target = self.serialize(sim, steps[idx], idx)
        old = self.tokens(sim, target)
        if old < self.threshold:
            return
        context = "\n".join(self.serialize(sim, steps[i] if i >= 0 else None, i)
                            for i in range(idx - self.b, min(idx + self.a, len(steps) - 1) + 1))
        system = AGENTDIET_SYSTEM.replace("think", "talk").replace("agent", "engineer") if self.bypass else AGENTDIET_SYSTEM
        user = (f"{context}\n\nNow, compress the step {idx}."
                # The original prefills the answer and stops at </step>; Responses requests have neither.
                f'\n\nAnswer exactly as: Sure. Here is the compressed content of step {idx}: <step id="{idx}">...</step>')
        answer = sim.model_text(system, user, purpose="reflect", model=self.reflect_model)
        if answer is None:
            return
        content = answer.partition("</step>")[0]
        if "<step" in content[:200]:
            content = content.partition("<step")[2]
            if ">" in content[:20]:
                content = content.partition(">")[2]
        new = self.tokens(sim, content)
        if old - new >= self.min_saving or new < self.max_ratio * old:
            sim.summarize_segment(steps[idx], text=ERASED + content.strip(), model_written=True, role="assistant",
                                  diet_original=target)
