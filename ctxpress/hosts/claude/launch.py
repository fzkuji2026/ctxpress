"""`ctxpress claude --method X -- <claude arguments>`: run the user's own Claude Code through the ctxpress proxy.

The proxy listens on loopback and forwards to the configured Anthropic endpoint (ANTHROPIC_BASE_URL if already
set, else https://api.anthropic.com); Claude Code reaches it through ANTHROPIC_BASE_URL in its own process
environment only. Credentials stay where Claude Code keeps them: the proxy passes request headers through and
logs no headers. The method's agent tools (and ctxpress_retrieve / ctxpress_status) come from `ctxpress mcp`,
given with --mcp-config and pre-allowed with --allowedTools; no Claude Code settings file is written or changed.
Claude Code's own compaction stays as the user configured it.
"""
from __future__ import annotations
import copy, json, os, shutil, subprocess, sys, threading, time, uuid
from ctxpress.live.proxy import serve
from ctxpress.live.factory import frozen_factory
from ctxpress.methods import build
from ctxpress import settings
from ctxpress.live.telemetry import summary

ANTHROPIC = "https://api.anthropic.com"
BASE_TOOLS = ("ctxpress_retrieve", "ctxpress_status")


def package_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def mcp_config(tool_env):
    """Claude Code --mcp-config: the same `ctxpress mcp` server, importing this ctxpress."""
    env = dict(tool_env, PYTHONPATH=os.pathsep.join(p for p in (package_root(), os.environ.get("PYTHONPATH", "")) if p))
    return {"mcpServers": {"ctxpress": {"type": "stdio", "command": sys.executable, "args": ["-m", "ctxpress", "mcp"], "env": env}}}


def allowed_tools(method):
    return [f"mcp__ctxpress__{name}" for name in (*BASE_TOOLS, *(t["name"] for t in method.agent_tools))]


def claude_command(claude_bin, claude_args, mcp_path, method, tools=True):
    prefix = list(claude_bin) if isinstance(claude_bin, (list, tuple)) else [claude_bin]
    extra = ["--mcp-config", mcp_path, "--allowedTools", ",".join(allowed_tools(method))] if tools else []
    return [*prefix, *extra, *claude_args]


def run(method_entry=None, claude_args=(), claude_bin=None, port=0, upstream=None, via=None, log=None, dry_run=False,
        store_dir=None, tools=True, budget=None, env=None):
    claude_bin = claude_bin or shutil.which("claude") or "claude"
    cfg = settings.load()
    run_id = uuid.uuid4().hex
    run_dir = os.path.join(settings.home(), "runs", run_id)
    base = dict(os.environ if env is None else env)
    upstream = upstream or base.get("ANTHROPIC_BASE_URL") or ANTHROPIC
    via = via or base.get("HTTPS_PROXY") or base.get("https_proxy")
    log = log or os.path.join(run_dir, "requests.jsonl")
    store_dir = os.path.join(store_dir or settings.store_dir(), run_id)
    entry = copy.deepcopy(method_entry or {"class": cfg["method"], "args": cfg.get("args") or {}})
    if budget is not None:
        from ctxpress.methods.budget import with_budget
        entry = with_budget(entry, budget)
    sample = build(entry)
    factory = frozen_factory(sample)
    tool_env = dict(CTXPRESS_STORE_DIR=store_dir, CTXPRESS_LOG_PATH=log, CTXPRESS_METHOD_CONFIG=json.dumps(entry))
    if not dry_run:
        os.makedirs(run_dir, exist_ok=True)
    srv, _ = serve(factory, port, upstream, via, log, host="127.0.0.1", store_dir=store_dir, store_prefix=store_dir,
                   retrieve_tool=tools)
    port = srv.server_address[1]
    mcp_path = os.path.join(run_dir, "mcp.json")
    cmd = claude_command(claude_bin, list(claude_args), mcp_path, sample, tools)
    if dry_run:
        srv.server_close()
        return cmd, None
    with open(mcp_path, "w", encoding="utf-8") as stream:
        json.dump(mcp_config(tool_env), stream)
    child = dict(base, ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", CTXPRESS_MCP="1" if tools else "", **tool_env)
    child["NO_PROXY"] = ",".join(x for x in [child.get("NO_PROXY", ""), "127.0.0.1", "localhost"] if x)
    t0 = time.time()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        try:
            rc = subprocess.call(cmd, env=child)
        except KeyboardInterrupt:
            rc = 130
    finally:
        srv.shutdown(); srv.server_close()
    return cmd, (rc, summary(log, t0))
