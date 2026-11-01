"""Read-less tool views: a tool output is shown in a smaller form the moment it enters the context.

The method is a wrapper around the host's tools, not a history compressor. When a read, search or test output
arrives, a *view policy* decides what the agent sees instead of the raw text (an outline of a long file with line
numbers, the first matches per file of a long search, the failures of a long test log). Nothing is lost: the file
is still on disk and the footer tells the agent how to print exact lines; running the same command again returns
the full output unchanged (the escape hatch is enforced here, not left to the policy).

The view is decided once, at entry, so it never invalidates the prompt cache later. The policy is a plain Python
module with one function

    view(cmd, out, ctx) -> str | None      # None: leave this output as it is

so that it can be rewritten by hand or by an automatic search (repro/evolve_readless.py) without touching the
method. `ctx` carries `tokens(text)` and per-session state the method keeps (`seen`: normalised commands already
returned in full). Codex Code Mode outputs (one `exec` call running several commands) are split into their JSON
chunks and each chunk is viewed with the command that produced it; a chunk that cannot be matched to its command
is left alone.
"""
from __future__ import annotations
import importlib, importlib.util, json, os, re
from ctxpress.core import tokens
from ctxpress.core.trace import shell_command, unescape
from ctxpress.methods.base import Method

DEFAULT_POLICY = "ctxpress.methods.readless_policies.seed"
_EXEC = re.compile(r'tools\.exec_command\s*\(\s*\{\s*"?cmd"?\s*:\s*"((?:[^"\\]|\\.)*)"')
_INNER = re.compile(r'tools\.(exec_command|apply_patch|write_stdin)\s*\(')
_HEADER_END = "\nOutput:\n"


def load_policy(spec):
    """A module name (ctxpress.methods.readless_policies.seed) or a path to a .py file."""
    if spec.endswith(".py") or os.sep in spec or "/" in spec:
        name = "readless_policy_" + re.sub(r"\W", "_", os.path.abspath(spec))
        loader = importlib.util.spec_from_file_location(name, spec)
        module = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(module)
        return module
    return importlib.import_module(spec)


def norm_cmd(cmd):
    return " ".join((cmd or "").split())


def split_code_mode(text):
    """(header, chunks) of a Code Mode exec output, chunks as dicts in order; None if it is not one."""
    i = text.find(_HEADER_END)
    if i < 0:
        return None
    header, body = text[:i + len(_HEADER_END)], text[i + len(_HEADER_END):]
    chunks = []
    for line in body.split("\n"):
        if not line.strip():
            continue
        try:
            c = json.loads(line)
        except ValueError:
            return None
        if not isinstance(c, dict):
            return None
        chunks.append(c)
    return header, chunks


def join_code_mode(header, chunks):
    return header + "\n" + "\n".join(json.dumps(c, ensure_ascii=False, separators=(",", ":")) for c in chunks)


def inner_commands(call_text):
    """Commands of the exec_command calls in a Code Mode script, in call order (None for other inner calls)."""
    s = call_text or ""
    out = []
    for m in _INNER.finditer(s):
        if m.group(1) != "exec_command":
            out.append(None)
            continue
        e = _EXEC.match(s, m.start())
        out.append(json.loads('"' + e.group(1) + '"') if e else None)
    return out


def host_commands(call):
    """Commands from the host's own record of a Code Mode cell (executed_tool_calls), None when it is incomplete."""
    tools = call.get("executed_tool_calls")
    if call.get("executed_tool_calls_complete") is not True or not isinstance(tools, list) or not tools:
        return None
    out = []
    for tool in tools:
        args = tool.get("arguments") if isinstance(tool, dict) else None
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = None
        if not isinstance(args, dict) or "_codex_executed_tool_call_truncated" in args:
            return None
        out.append(args.get("cmd") if tool.get("name") == "exec_command" and isinstance(args.get("cmd"), str) else None)
    return out


class ReadLess(Method):
    name = "Read-less tool views"
    framework = dict(L1="读入时按视图策略缩小读文件/搜索/测试输出（原文在磁盘，同一命令再跑一次返回全文）",
                     L2="无", L3="无", cross="无", memory="无", decider="视图策略（可手写或自动搜索）")
    source = "本文"

    def __init__(self, policy=DEFAULT_POLICY, min_tokens=800, **policy_args):
        self.policy_spec = policy
        self.policy = load_policy(policy)
        self.min_tokens = min_tokens
        self.policy_args = policy_args
        self.seen = set()

    def reset(self, sim):
        self.seen = set()

    def _ctx(self, sim):
        return dict(tokens=tokens.count, seen=self.seen, args=self.policy_args)

    def _view_one(self, sim, cmd, out):
        """The view of one command's output, or None. Repeating a command already viewed returns the full text."""
        key = norm_cmd(cmd)
        if not cmd or not out or tokens.count(out) < self.min_tokens:
            return None
        if key in self.seen:
            sim.M["readless_full_again"] += 1
            return None
        new = self.policy.view(cmd, out, self._ctx(sim))
        if not new or tokens.count(new) >= tokens.count(out):
            return None
        self.seen.add(key)
        return new

    def on_ingest(self, sim, item):
        if item.get("seg") != "out" or item.get("protected") or item.get("has_media") or item.get("form", "full") != "full":
            return
        text = item.get("text") or ""
        if tokens.count(text) < self.min_tokens:
            return
        call = unescape(item.get("call_text") or "")
        parts = split_code_mode(text)
        if parts is not None and "tools." in call:
            header, chunks = parts
            cmds = host_commands(getattr(sim, "calls", {}).get(item.get("call_id")) or {})
            if cmds is None:
                cmds = inner_commands(call)
            if len(cmds) != len(chunks):
                sim.M["readless_unmapped"] += 1
                return
            changed = False
            for c, cmd in zip(chunks, cmds):
                if cmd is None or not isinstance(c.get("output"), str):
                    continue
                new = self._view_one(sim, cmd, c["output"])
                if new is not None:
                    c["output"] = new; changed = True
            if not changed:
                return
            new_text = join_code_mode(header, chunks)
        else:
            new_text = self._view_one(sim, shell_command(call), text)
            if new_text is None:
                return
        before = tokens.count(text)
        if sim.keep_text(item, new_text, form="structured"):
            sim.M["readless_view"] += 1
            sim.M["readless_saved_tokens"] += before - tokens.count(new_text)
