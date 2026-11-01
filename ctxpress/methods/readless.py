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
_RANGED = re.compile(r"sed\s+-n\s+['\"]?\d+,\d+p|head\s+(-n\s*)?-?\d+|tail\s+(-n\s*)?-?\d+|grep\s+[^|;]*-[A-Za-z]*[ABC]\s*\d")


_POLICIES = {}


def load_policy(spec):
    """A module name (ctxpress.methods.readless_policies.seed) or a path to a .py file; loaded once per process."""
    if spec in _POLICIES:
        return _POLICIES[spec]
    if spec.endswith(".py") or os.sep in spec or "/" in spec:
        name = "readless_policy_" + re.sub(r"\W", "_", os.path.abspath(spec))
        loader = importlib.util.spec_from_file_location(name, spec)
        module = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(module)
    else:
        module = importlib.import_module(spec)
    _POLICIES[spec] = module
    return module


_IDS = re.compile(r'"?ids"?\s*:\s*\[([\d,\s]*)\]')
_NOTE = re.compile(r'"?note"?\s*:\s*"((?:[^"\\]|\\.)*)"')


def drop_requests(text):
    """(ids, note) of each readless_drop request in a call: JSON arguments of a direct call, or the object literal
    of a call made from a Code Mode script (tools.mcp__ctxpress__readless_drop({ids:[3], note:"..."}))."""
    out = []
    try:
        args = json.loads(text)
    except ValueError:
        args = None
    if isinstance(args, dict) and isinstance(args.get("ids"), list):
        return [([n for n in args["ids"] if isinstance(n, int)], str(args.get("note") or "")[:600])]
    for part in text.split(DROP_TOOL["name"])[1:]:
        head = part[:2000]
        m = _IDS.search(head)
        if not m:
            continue
        note = _NOTE.search(head)
        out.append(([int(x) for x in re.findall(r"\d+", m.group(1))],
                    (_js_string(note.group(1)) or "")[:600] if note else ""))
    return out


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
        out.append(_js_string(e.group(1)) if e else None)
    return out


def _js_string(body):
    """A double-quoted JavaScript string body; JS allows escapes JSON does not (\\' and the like)."""
    try:
        return json.loads('"' + body + '"')
    except ValueError:
        try:
            return json.loads('"' + re.sub(r"\\([^\"\\/bfnrtu])", r"\1", body) + '"')
        except ValueError:
            return None


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


DROP_TOOL = dict(name="readless_drop",
                 description="Remove tool outputs you have finished with from your context. Give their numbers "
                             "(the [output #N] labels) and, in `note`, the facts from them you still need. The full "
                             "text can always be printed again by running the same command.",
                 inputSchema=dict(type="object", properties=dict(
                     ids=dict(type="array", items=dict(type="integer")),
                     note=dict(type="string", description="what you still need from these outputs, briefly")),
                     required=["ids", "note"]))
DROP_GUIDANCE = ("Long tool outputs are labeled [output #N]. You decide what stays in your context: as soon as you "
                 "have taken what you need from a long output (for example after reading a file you will not edit, a "
                 "listing, a search or a test log), call mcp__ctxpress.readless_drop with its number and a short note "
                 "of the facts you still need; you can do this in the same step as your next command. Keep outputs "
                 "you are about to edit from. Removed text can be printed again by running the same command.")


class ReadLess(Method):
    """policy=None: no rule views. agent=True: long outputs get a number at entry and the agent removes the ones it
    has finished with through readless_drop (its own decision of what matters); the removal happens at the next
    request, while the output is still near the end of the context, so little of the cached prefix is lost."""
    name = "Read-less tool views"
    framework = dict(L1="读入时按视图策略缩小读文件/搜索/测试输出（原文在磁盘，同一命令再跑一次返回全文）；可选由 Agent 自己标出用完的输出",
                     L2="无", L3="无", cross="无", memory="无", decider="视图策略（可手写或自动搜索）+ Agent 自己的判断")
    source = "本文"

    def __init__(self, policy=DEFAULT_POLICY, min_tokens=800, agent=False, ranged=True, **policy_args):
        self.policy_spec = policy
        if policy:
            load_policy(policy)                 # fail early on a bad policy; the method itself holds only the name
        self.min_tokens = min_tokens
        self.agent = agent
        self.ranged = ranged                    # False: never view a command that already asked for line ranges
        self.policy_args = policy_args
        self.agent_tools = (DROP_TOOL,) if agent else ()
        self.instructions = DROP_GUIDANCE if agent else ""
        self.seen = set()
        self.numbered = {}                      # output number -> context item id
        self.applied = set()                    # ids of drop calls already applied

    def reset(self, sim):
        self.seen = set(); self.numbered = {}; self.applied = set()

    def call_tool(self, name, args):
        if name == DROP_TOOL["name"]:
            return json.dumps(dict(ok=True, note="removed at your next step; continue the task")), False
        raise KeyError(f"unknown tool {name}")

    def _ctx(self, sim):
        return dict(tokens=tokens.count, seen=self.seen, args=self.policy_args)

    def _view_one(self, sim, cmd, out):
        """The view of one command's output, or None. Repeating a command already viewed returns the full text."""
        key = norm_cmd(cmd)
        if not self.policy_spec or not cmd or not out or tokens.count(out) < self.min_tokens:
            return None
        if not self.ranged and _RANGED.search(cmd):
            return None
        if key in self.seen:
            sim.M["readless_full_again"] += 1
            return None
        new = load_policy(self.policy_spec).view(cmd, out, self._ctx(sim))
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
        self._view(sim, item, text)
        if self.agent:
            n = len(self.numbered) + 1
            self.numbered[n] = item["id"]
            shown = item.get("kept_text") if item.get("form") == "structured" else text
            if sim.keep_text(item, f"{shown}\n[output #{n}]", form="structured"):
                sim.M["readless_numbered"] += 1

    def step(self, sim, r):
        if not self.agent:
            return
        for call in sim.ctx:
            text = unescape(call.get("text") or "") if call["seg"] == "call" else ""
            if call["id"] in self.applied or DROP_TOOL["name"] not in text + (call.get("name") or ""):
                continue
            self.applied.add(call["id"])
            for ids, note in drop_requests(text):
                self._drop(sim, ids, note)

    def _drop(self, sim, ids, note):
        for n in ids:
            item = next((x for x in sim.ctx if x["id"] == self.numbered.get(n)), None)
            if item is None or item.get("readless_dropped") or item.get("form") not in ("full", "structured"):
                sim.M["readless_drop_missing"] += 1; continue
            cmd = shell_command(unescape(item.get("call_text") or ""))[:300]
            before = item.get("kept", tokens.count(item.get("text") or ""))
            text = (f"[output #{n} removed by you; your note: {note or '(none)'}. "
                    f"Print it again with the same command if needed: {cmd}]")
            if sim.keep_text(item, text, form="structured"):
                item["readless_dropped"] = True
                sim.M["readless_drop"] += 1
                sim.M["readless_drop_saved_tokens"] += max(0, before - tokens.count(text))

    def _view(self, sim, item, text):
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
