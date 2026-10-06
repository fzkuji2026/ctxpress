"""SWE-Pruner (Wang et al., arXiv 2601.16746) on the ctxpress framework.

As in the authors' agent (mini-swe-agent with pruning: agents/default.py `_apply_pruner`, utils/pruner.py,
templates/pruner.yaml): the agent attaches a context focus question to a command whose output will be long; the
output is sent with the question to the pruner (a 0.6B model, served by the authors' `swe-pruner` server), which
keeps the relevant lines. The output then reads
    "Filtered some unrelevant parts judged by your context_focus_question, good try! Filtered Output:\\n" + pruned
or, when every line is kept (or the output is at most `min_chars` characters),
    "All outputs are judged as relevent! Output:\\n" + output
and "[Pruner Error]: ..." with the original output when the pruner fails. Outputs without a question are untouched.

Codex has no field for the question, so the method asks for it in its instructions (sent as a developer message):
the agent appends a shell comment `# context_focus_question: <question>` to the command. The original's
<context_focus_question> tag is accepted too.

Without a pruner (the replay simulator, or `url=None`) the relevant lines cannot be judged; `fallback` chooses
"definitions" (keep the file's definition lines: a lower bound) or "oracle" (also every line a later edit needs:
an upper bound). The small model's reading cost is charged as overhead.
"""
from __future__ import annotations
import json, re, urllib.request
from ctxpress.methods.base import Method
from ctxpress.core.trace import content_type, shell_command

FILTERED = "Filtered some unrelevant parts judged by your context_focus_question, good try! Filtered Output:\n"
ALL_KEPT = "All outputs are judged as relevent! Output:\n"
INSTRUCTIONS = (
    "Context pruning is enabled for command outputs. When a command will print a lot (for example reading a whole "
    "file), end the command with a shell comment stating what you are looking for:\n"
    "    nl -ba path/to/file.py  # context_focus_question: where is the timeout handled?\n"
    "Only the lines relevant to that question are shown to you. Leave the comment out when you need the full output; "
    "if the filtered output misses something, read the lines you need again without the comment.")
_TAG = re.compile(r"<context_focus_question>\s*(.*?)\s*</context_focus_question>", re.S | re.I)
_COMMENT = re.compile(r"#\s*context_focus_question:\s*(.+?)\s*$", re.M)


def focus_question(call_text):
    """The question the agent attached to a call, or None."""
    m = _TAG.search(call_text or "")
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = _COMMENT.search(shell_command(call_text or "").replace("\\n", "\n"))
    return m.group(1).strip().strip('"\'') if m else None


class HTTPPruner:
    """Client of the authors' server (`swe-pruner --model-path ./model`): POST {query, code, threshold, ...}."""

    def __init__(self, url, timeout=60.0):
        self.url, self.timeout = url, timeout

    def __call__(self, query, code, threshold, chunk_overlap_tokens):
        body = json.dumps(dict(query=query, code=code, threshold=threshold, always_keep_first_frags=False,
                               chunk_overlap_tokens=chunk_overlap_tokens)).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())


def render(text, query, prune, threshold=0.5, min_chars=500, chunk_overlap_tokens=50):
    """The output the agent sees (the original's PrunerClient.prune + _apply_pruner). Returns (text, stats)."""
    if not text or not query:
        return text, None
    if len(text) <= min_chars:
        return ALL_KEPT + text, dict(origin_token_cnt=0, left_token_cnt=0)
    try:
        r = prune(query, text, threshold, chunk_overlap_tokens)
    except Exception as e:                                  # noqa: BLE001 - the original reports any failure
        return f"[Pruner Error]: {e}\n\nOriginal Output:\n{text}", None
    if r.get("error_msg"):
        return f"[Pruner Error]: {r['error_msg']}\n\nOriginal Output:\n{text}", r
    if r["left_token_cnt"] == r["origin_token_cnt"]:
        return ALL_KEPT + text, r
    return FILTERED + r["pruned_code"], r


class SWEPruner(Method):
    """SWE-Pruner (2601.16746): outputs of commands that carry a focus question are pruned to the relevant lines
    by a small model when they enter the context."""
    source = "arXiv 2601.16746"
    instructions = INSTRUCTIONS

    def __init__(self, url=None, threshold=0.5, min_chars=500, chunk_overlap_tokens=50, fallback="definitions",
                 pruner=None, oracle=None):
        if oracle is not None:                              # older configs: SWEPruner(oracle=True)
            fallback = "oracle" if oracle else "definitions"
        self.threshold, self.min_chars, self.overlap, self.fallback = threshold, min_chars, chunk_overlap_tokens, fallback
        self.prune = pruner or (HTTPPruner(url) if url else None)
        label = "" if self.prune else ("（理想上界）" if fallback == "oracle" else "（定义行近似）")
        self.name = "SWE-Pruner" + label
        self.framework = dict(L1="带关注问题的命令输出：小模型只留相关行" + ("" if self.prune else "（无模型时：" + ("之后修改需要的行，理想上界）" if fallback == "oracle" else "定义行代替）")),
                              L2="无", L3="无", cross="无", memory="无", decider="小模型判断（计入费用）")

    def reset(self, sim):
        self.read = 0
        self.stats = []

    def validate_live(self):
        if self.prune is None:
            raise ValueError('SWEPruner requires a pruning service for live execution: '
                             'set args.url (for example http://127.0.0.1:8000/prune). '
                             'The definitions/oracle fallback is for replay only.')

    def future_anchors(self, sim, s):
        out = set()
        for p in s.get("res", []):
            for q in sim.reqs[sim.r + 1:]:
                stop = False
                for x in q["before"]:
                    if x["seg"] == "call" and x.get("kind") == "edit":
                        out |= set((x.get("anchors") or {}).get(p, []))
                    if x["seg"] == "out" and x.get("kind") in ("read", "spec") and p in x.get("res", []):
                        stop = True
                if stop:
                    break
        return out

    def on_ingest(self, sim, s):
        if s["seg"] != "out" or s.get("protected"):
            return
        if self.prune is None:                              # replay: no model to judge relevance
            if content_type(s) == "read_code":
                self.read += s["size"]
                sim.structure(s, keep_also=self.future_anchors(sim, s) if self.fallback == "oracle" else ())
            return
        q = focus_question(s.get("call_text") or "")
        if not q or s.get("text") is None:
            return
        source = s.get("kept_text") if s.get("form", "full") in ("truncated", "structured") else s["text"]
        if s.get("form", "full") not in ("full", "truncated", "structured") or source is None:
            return
        called = bool(source) and len(source) > self.min_chars
        text, stats = render(source, q, self.prune, self.threshold, self.min_chars, self.overlap)
        if called:
            sim.M["pruner_calls"] += 1
            if stats is not None and stats.get("model_input_token_cnt") is not None:
                sim.M["pruner_input_tokens"] += stats["model_input_token_cnt"]
            else:
                sim.M["pruner_usage_unknown"] += 1
        if stats is not None:
            # Short outputs report zero: the service was not called. The model's
            # input includes its query/prefix, not just the observed source text.
            self.read += stats.get("model_input_token_cnt", sim.seg_size(s) if len(source) > self.min_chars else 0)
        self.stats.append({key: stats[key] for key in ("origin_token_cnt", "left_token_cnt", "model_input_token_cnt")
                           if key in stats} if stats is not None else None)
        del self.stats[:-128]                               # diagnostics never retain large per-token arrays
        sim.keep_text(s, text)

    def overhead(self, sim, r):
        n, self.read = self.read, 0
        return n * sim.P.get("small_model_price")
