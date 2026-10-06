"""Dynamic Tool Output Compression (Chaturvedi et al., arXiv 2609.26121; reference implementation: an OpenCode fork,
chaturvediabhay24/opencode, session/dtoc.ts, tool/manage_context.ts and the tool-result envelope in message-v2.ts)."""
from __future__ import annotations
import json, math
from ctxpress.methods.base import Method
from ctxpress.core.compat import js_length

# tool/manage_context.txt
MANAGE_DESCRIPTION = """Use this tool to manage context visibility when DTOC (Dynamic Tool Output Compression) is enabled.

You can disable tool outputs that are no longer relevant to your current reasoning step, freeing up context space. You can also re-enable previously disabled outputs if you need to reference them again.

Each tool output is assigned a unique `tool_key` (e.g. "tk_001", "tk_002") shown in the tool result envelope. Use these keys to control visibility.

When you disable a tool output, its full content is replaced with a compact placeholder showing only metadata (tool_key, tool name, estimated tokens, timestamp). The original content is preserved and can be restored at any time by re-enabling it.

Guidelines:
- Disable outputs that contain information you have already extracted or that is no longer relevant
- Re-enable outputs when you need to revisit earlier evidence
- Consider disabling large outputs (high token count) first for maximum context savings
- You can see the estimated_tokens for each tool output to make informed decisions"""
MANAGE_SCHEMA = {"type": "object", "required": ["enable", "disable"], "properties": {
    "enable": {"type": "array", "items": {"type": "string"},
               "description": "List of tool_key values to re-enable (restore full content in context)"},
    "disable": {"type": "array", "items": {"type": "string"},
                "description": "List of tool_key values to disable (replace with compact placeholder)"}}}
# command/template/dtoc.txt without its last line (the toggle notice): sent when the user turns DTOC on with /dtoc
STRATEGY = """DTOC (Dynamic Tool Output Compression) mode has been toggled for this session.

When DTOC is enabled:
- Every tool output you receive includes a `tool_key` identifier and `estimated_tokens` count in a JSON envelope
- You have access to the `manage_context` tool to disable/re-enable tool outputs by their tool_key
- Disabled outputs are replaced with compact placeholders to save context space
- You can re-enable any disabled output at any time — no information is ever lost

The purpose of DTOC is to minimize cost, time, and token usage for the conversation while maintaining output accuracy.

Strategy:
- Load multiple files in parallel in a single call to gather context quickly, then disable outputs that are not relevant to the task — this reduces back-and-forth turns
- When you re-read a file after editing it, disable the older stale version so only the current content remains in context
- Before producing a final response, disable irrelevant outputs so your context contains only what is needed for an accurate answer
- Focus on disabling high-token outputs first for maximum savings
- Re-enable outputs only when you need to revisit earlier evidence"""


def js(value):
    """JSON.stringify of plain objects."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def is_manage(name):
    return bool(name) and (name == "manage_context" or name.endswith("__manage_context"))


class Registry:
    """session/dtoc.ts: tool keys in registration order, visibility, cumulative tokens saved."""

    def __init__(self):
        self.entries, self.by_call, self.saved = {}, {}, 0

    def register(self, call_id, tool, tokens, timestamp):
        if call_id in self.by_call:
            return self.by_call[call_id]
        key = f"tk_{len(self.entries) + 1:03d}"
        self.entries[key] = dict(tool_key=key, tool=tool, tokens=tokens, timestamp=timestamp, visible=True)
        self.by_call[call_id] = key
        return key

    def set_visibility(self, keys, visible):
        modified = []
        for key in keys:
            e = self.entries.get(key)
            if e and e["visible"] != visible:
                e["visible"] = visible
                modified.append(key)
                self.saved += -e["tokens"] if visible else e["tokens"]
        return modified

    def manage(self, enable, disable):
        """manage_context.ts execute(): the tool's result text."""
        enabled, disabled = self.set_visibility(enable, True), self.set_visibility(disable, False)
        lines = []
        if enabled:
            lines.append(f"Re-enabled: {', '.join(enabled)}")
        if disabled:
            lines.append(f"Disabled: {', '.join(disabled)}")
        visible = sum(e["visible"] for e in self.entries.values())
        lines.append(f"Status: {visible} visible, {len(self.entries) - visible} hidden tool outputs")
        lines.append(f"Tokens saved (cumulative): {self.saved}")
        missing = [k for k in enable if k not in enabled and k not in self.entries] + \
                  [k for k in disable if k not in disabled and k not in self.entries]
        if missing:
            lines.append(f"Not found: {', '.join(missing)}")
        return "\n".join(lines)


class DTOC(Method):
    """DTOC: every tool output is shown in an envelope {tool_key, tool, estimated_tokens, tool_result} (keys
    tk_001, tk_002, ... in history order, estimated_tokens = ceil(chars / 4)); the agent calls
    manage_context(enable, disable) with keys to hide outputs, which then read {tool_key, tool, estimated_tokens,
    timestamp, status: "hidden"}, or to show them again. As in the fork, keys are registered when a request is
    assembled, so a manage_context call sees the keys of the previous request; its own result is an output too.
    The keys and visibility live in the proxy: the result text the agent reads is the fork's (the tool process,
    which cannot see the conversation, only acknowledges). `strategy`: also give the /dtoc command's guidance."""
    source = "arXiv 2609.26121"
    agent_tools = ({"name": "manage_context", "description": MANAGE_DESCRIPTION, "inputSchema": MANAGE_SCHEMA,
                    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}},)

    def __init__(self, strategy=True):
        self.strategy = bool(strategy)
        self.instructions = STRATEGY if strategy else ""
        self.name = "DTOC" + ("" if strategy else "（无 /dtoc 指导）")
        self.framework = dict(L1="每条工具输出带 tool_key；Agent 调 manage_context 隐藏 / 恢复", L2="无", L3="无",
                              cross="无", memory="原文保留，可恢复", decider="Agent 自己（工具）")
        self.reset(None)

    def call_tool(self, name, args):
        if not is_manage(name):
            raise KeyError(f"unknown tool {name}")
        if not isinstance(args.get("enable"), list) or not isinstance(args.get("disable"), list):
            return "enable and disable must both be lists of tool_key values", True
        return "recorded", False

    def reset(self, sim):
        self.registry, self.answers, self.done = Registry(), {}, set()

    def step(self, sim, r):
        calls = {s.get("call_id"): s for s in sim.ctx if s["seg"] == "call"}
        new = [s for s in sim.ctx if s["seg"] == "out" and s["id"] not in self.done]
        failed = set()
        for s in new:                                         # tool calls that ran since the last request
            call = calls.get(s.get("call_id"))
            if call is not None and is_manage(call.get("name")):
                try:
                    args = json.loads(call.get("args") or "{}")
                except ValueError:
                    args = {}
                if isinstance(args, dict) and isinstance(args.get("enable"), list) and isinstance(args.get("disable"), list):
                    self.answers[s["id"]] = self.registry.manage([str(k) for k in args["enable"]], [str(k) for k in args["disable"]])
                else:                                         # a rejected call errs: no key, no envelope
                    failed.add(s["id"])
        for s in new:                                         # assembling this request registers them
            self.done.add(s["id"])
            if s["id"] in failed:
                continue
            call = calls.get(s.get("call_id")) or {}
            s["dtoc_key"] = self.registry.register(s.get("call_id") or s["id"], call.get("name") or "tool",
                                                   math.ceil(js_length(self.answers.get(s["id"], s.get("text", ""))) / 4),
                                                   int(s.get("t", 0) * 1000))
        for s in sim.ctx:
            key = s.get("dtoc_key")
            if key is None:
                continue
            hidden = not self.registry.entries[key]["visible"]
            if hidden and s.get("form", "full") == "full":
                sim.to_placeholder([s])
            elif not hidden and s.get("form") == "placeholder":
                s["form"] = "full"; s.pop("kept", None)

    def output_text(self, s, text):
        e = self.registry.entries.get(s.get("dtoc_key"))
        if e is None:
            return text
        return js(dict(tool_key=e["tool_key"], tool=e["tool"], estimated_tokens=e["tokens"],
                       tool_result=self.answers.get(s["id"], text)))

    def placeholder_text(self, s, text):
        e = self.registry.entries[s["dtoc_key"]]
        return js(dict(tool_key=e["tool_key"], tool=e["tool"], estimated_tokens=e["tokens"], timestamp=e["timestamp"],
                       status="hidden"))
