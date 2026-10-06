"""Request-level traces: the agent's real sequence of requests, tool calls and outputs.

A trace is a dict
  name, file, prefix (tokens of the fixed prefix), alpha (token scale), compacted (bool), harness
  reqs: list of requests; request r = dict(input, cached, out_tokens, t, before=[segments appended since r-1])
A segment is a dict
  seg      'call' | 'out' | 'msg' | 'reason'
  size     tokens (chars/4 before scaling by alpha)
  kind     for call/out: 'read' | 'spec' | 'search' | 'command' | 'edit' | 'other'
  res      file paths the call touches
  outpaths file paths listed in a search / command output
  sub      for read outputs: 'code' or 'other' (config, logs, data)
  test     for command outputs: whether it ran a build / test
  role     for messages
  text     the content (calls and outputs; kept when keep_text=True) – used by truncation, structured
           compression and by the coverage check
  dur      seconds the call took (output timestamp - call timestamp)
Loaders: codex.load (Codex rollouts), claude_code.load (Claude Code transcripts). Both return the same shape.
"""
from __future__ import annotations
import json
import re

PATH = re.compile(r"(?<![\w.-])((?:[\w.-]+/)*[\w.-]+\.(?:go|py|ts|tsx|js|jsx|rs|java|md|json|toml|ya?ml|sql|sh|txt|proto|c|h|cc|cpp|css|html|vue|lock|mod|sum))\b")
CODE_EXT = re.compile(r"\.(go|py|ts|tsx|js|jsx|rs|java|c|h|cc|cpp|css|html|vue|sql|sh|proto)$")
READ = re.compile(r"\b(sed\s+-n|cat|nl|head|tail|less|git\s+show|wc)\b")
SEARCH = re.compile(r"\b(rg|grep|find|ls|tree|fd)\b|git\s+(grep|ls-files)")
TEST = re.compile(r"\b(go\s+(test|build|vet|run)|npm|npx|pnpm|yarn|make|pytest|python3?\s+-m\s+pytest|cargo|tsc|gofmt|eslint|vitest|jest)\b")
SPEC = re.compile(r"(srs/|_SRS\.md|TASK_QUEUE\.md|AGENTS\.md|README\.md|/docs?/|requirements|spec)", re.I)

# content types of the framework (preliminary study table 25)
CONTENT_TYPES = ["prefix", "user", "spec", "read_code", "read_other", "search", "run", "patch", "call",
                 "agent_text", "reasoning", "memory", "summary", "subagent"]


def unescape(s):
    return s.replace("\\\\", "\x00").replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\x00", "\\")


def norm(p):
    return "/".join(p.lstrip("./").split("/")[-3:])


def classify(inp):
    """Type of a tool call (from its input text) and the file paths it touches."""
    s = unescape(inp)
    patch = re.findall(r"\*\*\* (?:Update|Add|Delete) File: ([^\n]+)", s)
    if patch:
        return "edit", {norm(p.strip()) for p in patch}
    paths = {norm(m.group(1)) for m in PATH.finditer(s)}
    if paths and SPEC.search(s) and READ.search(s) and all(SPEC.search(p) for p in paths):
        return "spec", paths
    if READ.search(s) and paths:
        return "read", paths
    if TEST.search(s):
        return "command", paths
    if SEARCH.search(s):
        return "search", paths
    return "command", paths


def sub_of(kind, res):
    if kind != "read":
        return None
    return "code" if any(CODE_EXT.search(p) for p in res) else "other"


def content_type(s):
    """Fine content type of a segment (table 25)."""
    seg = s["seg"]
    if seg == "summary":
        return "summary"
    if s.get("source") == "memory":
        return "memory"
    if seg == "msg":
        return "user" if s.get("role") == "user" else "agent_text"
    if seg == "reason":
        return "reasoning"
    if seg == "call":
        return "patch" if s.get("kind") == "edit" else "call"
    k = s.get("kind")
    if k == "read":
        return "read_code" if s.get("sub", "code") == "code" else "read_other"
    if k == "command":
        return "run"
    return k if k in ("spec", "search") else "call"


def patch_anchors(patch_text, path):
    """Lines a patch expects to find in `path` (context and removed lines of its hunks)."""
    s = unescape(patch_text)
    out, cur = [], None
    for line in s.split("\n"):
        m = re.match(r"\*\*\* (?:Update|Add|Delete) File: (.+)", line)
        if m:
            cur = norm(m.group(1).strip()); continue
        if cur != path or not line or line.startswith(("@@", "***")):
            continue
        if line[0] in " -":
            t = line[1:].strip()
            if len(t) >= 4:
                out.append(t)
    return out


def fit_alpha(reqs, prefix):
    """Token scale: real input tokens vs chars/4 of the context, least squares through the origin."""
    c = 0; xs = []; ys = []
    for r in reqs:
        c += sum(x["size"] for x in r["before"]); xs.append(c); ys.append(r["input"] - prefix)
    return sum(x * y for x, y in zip(xs, ys)) / sum(x * x for x in xs)


def finish(name, file, reqs, compacted, harness, fit=True, alpha=1.17):
    prefix = reqs[0]["input"] - sum(x["size"] for x in reqs[0]["before"])
    if fit:
        alpha = fit_alpha(reqs, prefix)
    return dict(name=name, file=file, reqs=reqs, prefix=prefix, alpha=alpha, compacted=compacted, harness=harness)


_EXEC_CMD = re.compile(r'cmd\s*:\s*"((?:[^"\\]|\\.)*)"')


def shell_command(text):
    """The command line of a shell call: JSON arguments ({"cmd": ...} / {"command": ...}), or the JavaScript of
    new Codex's `exec` tool (tools.exec_command({cmd: "..."})); otherwise the text itself."""
    try:
        a = json.loads(text)
    except (ValueError, TypeError):
        a = None
    if isinstance(a, dict):
        c = a.get("cmd") or a.get("command") or ""
        return " ".join(c) if isinstance(c, list) else str(c)
    m = _EXEC_CMD.search(text or "")
    return m.group(1) if m else (text or "")
