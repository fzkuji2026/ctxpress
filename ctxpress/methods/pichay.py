"""Pichay (arXiv 2603.09023) on the ctxpress framework.

Follows the authors' pager (github.com/fsgeek/pichay: pager.py `compact_messages`, `PageStore`,
`_check_pin_freshness`, `_make_summary`; message_store.py and gateway.py for the per-request order):
  1. every tool output at least `age` (4) user turns from the end and of at least `min_size` (500) bytes is
     replaced by a stub "[tensor:HANDLE — <what> (N bytes, ...)]", permanently; the original stays in the store.
     Outputs of file reads are evictions (keyed by the file); other outputs are garbage collection;
  2. page faults: a read call of a file whose read output was evicted (and whose own output was not) is a fault;
     the evicted content's hash is remembered, and a later read output of that file with the same content is
     pinned (never evicted) until a read shows different content.
User turns are counted as in Anthropic messages, which Pichay is written for (each run of tool outputs and each
user message; `engine.Simulator.mark`). A tool is a "Read" when the call reads exactly one file (the framework's
call classification), "Bash" for other shell commands, otherwise its own name. Not ported: model-initiated
release tags and the recall tool (both need the model to cooperate through injected instructions).
`repro/pichay_compare.py` checks every request against the authors' MessageStore and pager.
"""
from __future__ import annotations
import hashlib
from ctxpress.methods.base import Method
from ctxpress.core.trace import shell_command

SHELL_TOOLS = {"exec", "exec_command", "shell", "shell_command", "local_shell", "local_shell_call"}


def tool_of(call_name, call_kind, call_res, call_text):
    """(name, input) of a call as Pichay sees it."""
    if call_kind in ("read", "spec") and len(call_res or []) == 1:
        return "Read", {"file_path": sorted(call_res)[0]}
    if call_name in SHELL_TOOLS:
        return "Bash", {"command": shell_command(call_text or "")}
    return call_name or "unknown", {}


def content_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def stub(name, inp, size, text, handle):
    size_str = f"{size:,}"
    if name == "Read":
        return f"[tensor:{handle} — {inp.get('file_path', 'unknown')} ({size_str} bytes, {text.count(chr(10))} lines)]"
    if name == "Bash":
        cmd = inp.get("command", "?")
        cmd = cmd[:77] + "..." if len(cmd) > 80 else cmd
        return f"[tensor:{handle} — Bash `{cmd}` ({size_str} bytes)]"
    return f"[tensor:{handle} — {name} ({size_str} bytes)]"


class Pichay(Method):
    """Pichay (2603.09023): demand paging. Tool outputs `age` user turns old and >= `min_size` bytes become
    stubs, permanently; re-reading an evicted file is a page fault, and a fault followed by an unchanged read
    pins that file."""
    source = "arXiv 2603.09023"

    def __init__(self, age=4, min_size=500):
        self.age, self.min_size = age, min_size
        self.name = f"Pichay（{age} 轮）"
        self.framework = dict(L1=f"{age} 轮之前、≥{min_size} 字节的工具输出永久换成存根", L2="无", L3="模型主动释放（未移植）",
                              cross="缺页后内容未变的文件固定保留", memory="有：原文存在代理里（按存根编号）", decider="固定规则 + 缺页反馈")

    def reset(self, sim):
        self.evicted = {}            # call_id -> item
        self.index = {}              # file -> evicted read item
        self.faults = set()          # call_ids
        self.fault_content = {}      # file -> content hash at eviction
        self.pinned = {}             # file -> content hash
        self.M = dict(evictions=0, gc=0, faults=0, pins=0)

    def _tool(self, sim, s):
        c = getattr(sim, "calls", {}).get(s.get("call_id"), {})
        return tool_of(s.get("call_name") or c.get("name"), c.get("kind", s.get("kind")), c.get("res", s.get("res")),
                       s.get("call_text") or c.get("text", ""))

    def step(self, sim, r):
        outs = sim.outputs()
        if self.pinned:
            self._freshness(sim, outs)
        for s in outs:
            if s.get("form", "full") != "full":
                continue                                   # a stub (or changed by another method)
            text = s.get("text") or ""
            size = len(text.encode("utf-8"))
            if sim.uturn - s.get("uturn", 0) < self.age or size < self.min_size:
                continue
            name, inp = self._tool(sim, s)
            key = inp.get("file_path") if name == "Read" else None
            if key is not None:
                if key in self.pinned:
                    continue
                if self.fault_content.get(key) == content_hash(text):
                    self.pinned[key] = content_hash(text); self.fault_content.pop(key, None); self.M["pins"] += 1
                    continue
            handle = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
            self.evicted[s.get("call_id")] = s
            if key is not None:
                self.index[key] = s; self.M["evictions"] += 1
            else:
                self.M["gc"] += 1
            s["stub"] = stub(name, inp, size, text, handle)
            sim.to_placeholder([s], form="placeholder")
        for c in sim.ctx:                                  # page faults (after compaction, as the gateway)
            if c["seg"] != "call" or c.get("call_id") in self.evicted or c.get("call_id") in self.faults:
                continue
            name, inp = tool_of(c.get("name"), c.get("kind"), c.get("res"), c.get("text"))
            key = inp.get("file_path") if name == "Read" else None
            if key is not None and key in self.index:
                self.faults.add(c.get("call_id")); self.M["faults"] += 1
                self.fault_content[key] = content_hash(self.index[key].get("text"))

    def _freshness(self, sim, outs):
        latest = {}
        for s in outs:
            name, inp = self._tool(sim, s)
            key = inp.get("file_path") if name == "Read" else None
            if key is None or key not in self.pinned or s.get("form", "full") != "full":
                continue
            latest[key] = content_hash(s.get("text"))
        for key, h in latest.items():
            if h != self.pinned[key]:
                self.pinned.pop(key, None); self.fault_content.pop(key, None)

    @staticmethod
    def placeholder_text(s, text):
        return s.get("stub") or text
