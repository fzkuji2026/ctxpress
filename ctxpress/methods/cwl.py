"""Context Window Lifecycle (Semenov & Dorofeev, arXiv 2606.11213; reference implementation Kiz8-Team/pi-cwl).

The agent marks its work as typed, dependency-linked episodes with a `delimiter` tool; over the token threshold a
deterministic policy strips completed episodes level by level (pi-cwl core/context-filter.ts `filterContext`).
"""
from __future__ import annotations
import json, math
from ctxpress.methods.base import Method, K
from ctxpress.methods.budget import BudgetSpec
from ctxpress.core.compat import js_length, js_stringify

# core/tools/delimiter.ts
DELIMITER_DESCRIPTION = """Use this tool to mark semantic chunk boundaries for the current task.

Call this tool with a JSON object. Use {"action":"start","name":"chunk-name","type":"expl"} when you begin an exploration chunk, {"action":"start","name":"chunk-name","type":"act","dependencies":["earlier-exploration"]} when you begin an action chunk, and {"action":"end","description":"short summary"} when you finish an exploration chunk.

Chunk types:
- `expl`: exploration work that gathers context
- `act`: execution work that applies changes or validation

Rules:
- Keep only one active chunk at a time.
- End the current chunk before starting another one.
- For end calls, omit name/type/dependencies. When ending an exploration chunk, include `description`.
- `description` should briefly summarize the information gathered in that exploration chunk.
- Each dependencies entry may only point from an act chunk to an earlier expl chunk.
- Keep names broad and outcome-based so they remain meaningful in later context."""
DELIMITER_SCHEMA = {"type": "object", "required": ["action"], "properties": {
    "action": {"type": "string", "enum": ["start", "end"], "description": 'Either "start" or "end"'},
    "name": {"type": "string", "description": "The chunk name (required when action is start)"},
    "type": {"type": "string", "enum": ["expl", "act"],
             "description": 'Chunk type: "expl" for exploration, "act" for action (required when action is start)'},
    "dependencies": {"type": "array", "items": {"type": "string"},
                     "description": "Exploration chunks this action chunk depends on (required when type is act)"},
    "description": {"type": "string",
                    "description": "Short 1-2 sentence summary of what the chunk contains. Required when ending an exploration chunk."}}}
GUIDELINES = [
    "Use the delimiter tool to structure your work into named chunks before starting any non-trivial task.",
    "Open an expl chunk when gathering context, reading files, or understanding the codebase.",
    "Open an act chunk (with dependencies on your expl chunks) when making changes, running commands, or validating results.",
    "Only one chunk may be active at a time — end the current one before starting the next.",
    "Keep chunk names broad and outcome-based (e.g. 'explore-auth-flow', 'patch-login-handler') so they remain meaningful later.",
    "End every chunk you open. Unclosed chunks leave stale context in the conversation window."]

SEARCH_TOOLS, BASH_TOOLS, READ_TOOLS = {"grep", "glob", "find", "ls"}, {"bash"}, {"read"}
SHELL_TOOLS = {"shell", "exec_command", "local_shell", "container.exec"}    # Codex: one shell for everything
SHELL_KIND = {"search": "search", "read": "read", "spec": "read", "command": "bash"}


class DelimiterState:
    """delimiter.ts execute(): the events so far decide whether a call is accepted."""

    def __init__(self):
        self.active, self.seen = None, {}

    def apply(self, params):
        """(event, None) for an accepted call, (None, error message) otherwise; only accepted calls change state."""
        try:
            event = self._event(params if isinstance(params, dict) else {})
        except ValueError as error:
            return None, str(error)
        self.seen[event["chunk"]["name"]] = event["chunk"]
        self.active = None if event["action"] == "end" else event["chunk"]
        return event, None

    def _event(self, p):
        if p.get("action") == "end":
            if not self.active:
                raise ValueError("No active chunk to end.")
            description = (p.get("description") or "").strip()
            if self.active["type"] == "expl":
                if not description:
                    raise ValueError('Exploration chunks must include a non-empty "description" when ended.')
                return dict(action="end", chunk=dict(self.active, description=description))
            if description:
                raise ValueError('Only exploration chunks accept "description" on end.')
            return dict(action="end", chunk=self.active)
        name, kind = (p.get("name") or "").strip(), p.get("type")
        if not name:
            raise ValueError('Chunk "name" is required when action is "start".')
        if not kind:
            raise ValueError('Chunk "type" is required when action is "start".')
        if self.active:
            raise ValueError(f'Chunk "{self.active["name"]}" is already active. End it before starting another chunk.')
        if name in self.seen:
            raise ValueError(f'Chunk "{name}" already exists in this session.')
        if kind == "act":
            if not p.get("dependencies"):
                raise ValueError('Action chunks must declare at least one "dependencies" entry.')
            deps = list(dict.fromkeys(d.strip() for d in p["dependencies"] if d.strip()))
            if name in deps:
                raise ValueError(f'Chunk "{name}" cannot depend on itself.')
            for dep in deps:
                if dep not in self.seen:
                    raise ValueError(f'Chunk dependency "{dep}" was not found in this session.')
                if self.seen[dep]["type"] != "expl":
                    raise ValueError(f'Chunk dependency "{dep}" must reference an exploration chunk.')
            return dict(action="start", chunk=dict(name=name, type="act", dependencies=deps))
        return dict(action="start", chunk=dict(name=name, type="expl"))


def line(event):
    """chunk.ts line(): the delimiter tool's result text."""
    c = event["chunk"]
    dep = f" dep={','.join(c['dependencies'])}" if c.get("dependencies") else ""
    description = f" — {c['description']}" if c.get("description") else ""
    return f"{event['action']} [{c['type']}] {c['name']}{dep}{description}"


def is_delimiter(name):
    return bool(name) and (name == "delimiter" or name.endswith("__delimiter"))


class CWL(Method):
    """Context Window Lifecycle: the agent opens and closes typed episodes with the `delimiter` tool (exploration
    `expl`, ending with a one-line description; action `act`, declaring the explorations it depends on). Above
    `budget` tokens (the authors' estimate: ceil(chars / 4) per message, system prompt excluded), completed episodes
    are stripped oldest first, an exploration only after every action depending on it: an action loses its search,
    then bash, then read calls with their results, then the whole episode; an exploration first loses its reasoning.
    The check runs after every level. User messages and everything before the first episode are never removed.
    As in pi-cwl, every request is filtered from the whole history: an exploration evicted earlier returns in full
    when a later action declares a dependency on it."""
    source = "arXiv 2606.11213"
    budget_spec = BudgetSpec("budget", "tokens", "conversation (the authors' estimate)", 80 * K)
    agent_tools = ({"name": "delimiter", "description": DELIMITER_DESCRIPTION, "inputSchema": DELIMITER_SCHEMA,
                    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}},)
    instructions = "Guidelines for context management:\n" + "\n".join(f"- {g}" for g in GUIDELINES)

    def __init__(self, budget=None):
        budget = self.budget_spec.resolve(budget, None)
        self.budget = budget
        self.name = f"CWL（{budget // K}k）"
        self.framework = dict(L1="无", L2="Agent 用 delimiter 标注探索 / 行动片段及依赖；超过阈值从最早的已完成片段逐级剥离",
                              L3="无", cross="用户消息与首个片段之前的内容不动", memory="无", decider="Agent 标注 + 固定规则")
        self.tool_state = DelimiterState()                     # the tool process's view (ctxpress mcp)
        self.reset(None)

    # ------------------------------------------------------------------ the agent's tool (tool process)
    def call_tool(self, name, args):
        if not is_delimiter(name):
            raise KeyError(f"unknown tool {name}")
        event, error = self.tool_state.apply(args)
        return (error, True) if event is None else (line(event), False)

    # ------------------------------------------------------------------ the proxy's side
    def reset(self, sim):
        self.state, self.events, self.history = DelimiterState(), {}, []

    def _record(self, sim):
        """Accept delimiter calls in arrival order with the tool's own rules (same sequence the tool process saw)."""
        calls = {s.get("call_id"): s for s in sim.ctx if s["seg"] == "call"}
        for s in sim.ctx:
            if s["seg"] != "out" or s.get("call_id") in self.events:
                continue
            call = calls.get(s.get("call_id"))
            if call is None or not is_delimiter(call.get("name")):
                continue
            try:
                params = json.loads(call.get("args") or "{}")
            except ValueError:
                params = {}
            self.events[s["call_id"]] = self.state.apply(params)[0]

    @staticmethod
    def category(s):
        name = s.get("name") or ""
        if name in ("exec", "functions.exec"):
            tools = s.get("executed_tool_calls")
            if s.get("executed_tool_calls_complete") is not True or not isinstance(tools, list) or not tools:
                return None
            levels = []
            for tool in tools:
                if not isinstance(tool, dict):
                    return None
                nested_name, arguments = tool.get("name"), tool.get("arguments")
                if not isinstance(nested_name, str) or not isinstance(arguments, (dict, str)):
                    return None
                if isinstance(arguments, dict) and "_codex_executed_tool_call_truncated" in arguments:
                    return None
                from ctxpress.core.trace import classify
                text = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
                level = CWL.category(dict(name=nested_name, kind=classify(text)[0]))
                if level is None:
                    return None
                levels.append(level)
            # A compound output is atomic: remove it at the earliest level at
            # which every constituent would be eligible, never split its text.
            return max(levels, key=("search", "bash", "read").index)
        if name in SEARCH_TOOLS:
            return "search"
        if name in BASH_TOOLS:
            return "bash"
        if name in READ_TOOLS:
            return "read"
        if name in SHELL_TOOLS:
            return SHELL_KIND.get(s.get("kind"))
        return None

    @staticmethod
    def estimate(sim):
        """compaction.ts estimateTokens summed over the messages: one assistant message per model turn."""
        total, chars, turn = 0, None, None
        for s in [x for x in sim.sent() if not (x["seg"] == "msg" and x.get("role") in ("system", "developer"))]:
            model = s["seg"] in ("reason", "call") or (s["seg"] == "msg" and s.get("role") == "assistant")
            if not model or s.get("turn") != turn:
                if chars is not None:
                    total += math.ceil(chars / 4)
                chars, turn = None, None
            text = sim.render(s) if hasattr(sim, "render") else s.get("text", "")
            if model:
                chars, turn = (chars or 0) + (js_length(s.get("name") or "") + js_length(js_stringify(s.get("args") or "{}")) if s["seg"] == "call"
                                              else js_length(text or "")), s.get("turn")
            else:
                total += math.ceil(js_length(text or "") / 4)
        return total + (math.ceil(chars / 4) if chars is not None else 0)

    def segments(self, sim):
        """segmentChunks: start / end anchors are the delimiter outputs; open episodes run to the end."""
        segs, open_ = [], {}
        for s in sim.ctx:
            event = self.events.get(s.get("call_id")) if s["seg"] == "out" else None
            if event is None:
                continue
            c = event["chunk"]
            if event["action"] == "start":
                segs.append(dict(name=c["name"], kind=c["type"], deps=c.get("dependencies") or [], done=False, start=s, end=s))
                open_[c["name"]] = segs[-1]
            elif c["name"] in open_:
                seg = open_.pop(c["name"]); seg.update(done=True, end=s)
        for seg in open_.values():
            seg["end"] = None
        return segs

    def step(self, sim, r):
        # pi-cwl filters the whole stored history before every call: an exploration evicted earlier comes back when
        # a later action declares a dependency on it. Restore what the last pass removed, then filter again.
        seen = {id(s) for s in self.history}
        self.history += [s for s in sim.ctx if id(s) not in seen]
        sim.reinstate(self.history)
        self._record(sim)
        if self.estimate(sim) <= self.budget:
            return
        segs = self.segments(sim)
        dependents = {}
        for seg in segs:
            for d in seg["deps"]:
                dependents.setdefault(d, set()).add(seg["name"])
        evicted = set()
        while self.estimate(sim) > self.budget:
            cands = [g for g in segs if g["done"] and g["name"] not in evicted and dependents.get(g["name"], set()) <= evicted]
            if not cands:
                break
            seg = cands[0]                                    # earliest start (segments are in start order)
            levels = (["search", "bash", "read"] if seg["kind"] == "act" else ["thinking", "search", "bash", "read"])
            for level in levels + ["entire"]:
                items = self.range(sim, seg)
                if items is None:
                    break
                self.strip(sim, items, level)
                if level != "entire" and self.estimate(sim) <= self.budget:
                    break
            evicted.add(seg["name"])

    @staticmethod
    def range(sim, seg):
        ids = [i for i, s in enumerate(sim.ctx) if s is seg["start"] or s is seg["end"]]
        if not any(s is seg["start"] for s in sim.ctx):
            return None
        return sim.ctx[ids[0]:ids[-1] + 1]

    def strip(self, sim, items, level):
        if level == "entire":
            sim.delete([s for s in items if not (s["seg"] == "msg" and s.get("role") == "user")])
        elif level == "thinking":
            sim.delete([s for s in items if s["seg"] == "reason"])
        else:
            calls = [s for s in items if s["seg"] == "call" and self.category(s) == level]
            ids = {s.get("call_id") for s in calls}
            sim.delete(calls + [s for s in sim.ctx if s["seg"] == "out" and s.get("call_id") in ids])
