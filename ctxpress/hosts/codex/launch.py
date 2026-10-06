"""Plug-and-play: run your own, unmodified Codex with any ctxpress method.

    ctxpress codex --method ComplexityTrap --args '{"n": 10}' -- exec "fix the failing test"
    ctxpress codex --method WithMemory --args '{"inner": {"class": "Pichay", "args": {"age": 4}}}'

What it does: starts the ctxpress proxy on 127.0.0.1, then runs
    codex --profile ctxpress-<run-id> <your args>
so every model request passes through the method; when Codex exits it prints what the method did and the real
token usage the API reported. Codex's own settings, login and tools are untouched. The upstream is the ChatGPT
Codex endpoint by default (ChatGPT login); use --upstream https://api.openai.com/v1 with an API key.
"""
from __future__ import annotations
import copy, json, os, shutil, subprocess, sys, tempfile, threading, time, uuid
from ctxpress.live.proxy import serve
from ctxpress.live.factory import frozen_factory
from ctxpress.methods import build
from ctxpress import settings
from ctxpress.hosts.codex import profiles
from ctxpress.core import toml
from ctxpress.live.telemetry import summary

CHATGPT = "https://chatgpt.com/backend-api/codex"


PROFILE = "ctxpress"

MANAGEMENT = {"login", "logout", "mcp", "plugin", "completion", "update", "doctor", "features", "help",
              "app-server", "remote-control", "agents", "apply", "a", "sandbox", "debug", "migrate-rollouts",
              "archive", "delete", "unarchive", "queue", "cloud", "exec-server"}
VALUE_FLAGS = {"-c", "--config", "-m", "--model", "-p", "--profile", "-s", "--sandbox", "-C", "--cd",
               "--add-dir", "-a", "--ask-for-approval", "--enable", "--disable", "-i", "--image",
               "--remote", "--remote-auth-token-env", "--local-provider"}
VALUE_FLAGS |= profiles.VALUE_FLAGS


def management_command(args):
    """Utilities and other host entry points run as the original native command."""
    skip, command_seen = False, False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg == "--":
            return False
        if arg in ("-h", "--help", "-V", "--version"):
            return True
        if arg in VALUE_FLAGS:
            skip = True
        elif not arg.startswith("-") and not command_seen:
            command_seen = True
            if arg in MANAGEMENT:
                return True
    return False


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def mcp_config(tools, tool_env=None):
    # Native Codex still validates a disabled server's transport. Replacing the
    # full server also avoids merging an inherited URL with our stdio command.
    package_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    return dict(command=sys.executable.replace('\\', '/'), args=['-m','ctxpress','mcp'],
                cwd=package_root.replace('\\', '/'), enabled=tools, env=tool_env or {},
                required=bool(tools and (tool_env or {}).get('CTXPRESS_REQUIRED_TOOLS')))


def write_profile(port, tools=True, profile=PROFILE, tool_env=None, codex_config=None, profile_data=None):
    """Write a private run profile without modifying native user configuration."""
    data = copy.deepcopy(profile_data or {})
    data['openai_base_url'] = f'http://127.0.0.1:{port}'
    data = profiles.merge(data, codex_config or {})
    data = profiles.merge(data, {'features':{'enable_request_compression':False}})
    mcp = data.setdefault('mcp_servers', {})
    mcp['ctxpress'] = mcp_config(tools, tool_env)
    os.makedirs(codex_home(), exist_ok=True)
    path = os.path.join(codex_home(), f'{profile}.config.toml')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=codex_home(), prefix='.ctxpress-', suffix='.toml', delete=False) as stream:
            temporary = stream.name
            stream.write('# Written by ctxpress for this run; safe to delete.\n' + toml.dumps(data))
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)
    return path


def codex_command(codex_bin, port, codex_args, tools=True, write=True, profile=PROFILE, tool_env=None, codex_config=None,
                  prepared=None):
    prefix = list(codex_bin) if isinstance(codex_bin, (list, tuple)) else [codex_bin]
    prepared = prepared or profiles.prepare(codex_args, codex_home())
    native = dict(codex_config or {})
    native["mcp_servers.ctxpress"] = mcp_config(tools, tool_env)
    data, args = prepared.runtime(port, native)
    if write:
        write_profile(port, tools, profile, tool_env, profile_data=data)
    flags = args[:args.index('--')] if '--' in args else args
    standalone = [] if '--no-daemon' in flags else ['--no-daemon']
    return [*prefix, '--profile', profile, *standalone, *args]


def run(method_entry=None, codex_args=(), codex_bin=None, port=0, upstream=None, via=None, log=None, dry_run=False,
        store_dir=None, tools=True, budget=None):
    """method_entry None: the method chosen with `ctxpress use` (snapshotted for this launch)."""
    codex_bin = codex_bin or shutil.which("codex") or "codex"
    if management_command(codex_args):
        prefix = list(codex_bin) if isinstance(codex_bin, (list, tuple)) else [codex_bin]
        command = [*prefix, *codex_args]
        return command, None if dry_run else (subprocess.call(command, env=dict(os.environ)), dict(requests=0, passthrough=True))
    prepared = profiles.prepare(codex_args, codex_home())
    cfg = settings.load()
    run_id = uuid.uuid4().hex
    profile = f"{PROFILE}-{run_id}"
    run_dir = os.path.join(settings.home(), "runs", run_id)
    configured = cfg.get("upstream")
    configured = configured if configured != settings.DEFAULTS["upstream"] else None
    upstream = upstream or configured or prepared.upstream or cfg.get("upstream") or CHATGPT
    via = via or cfg.get("via") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    log = log or os.path.join(run_dir, "requests.jsonl")
    store_dir = os.path.join(store_dir or settings.store_dir(), run_id)
    entry = copy.deepcopy(method_entry or {"class": cfg["method"], "args": cfg.get("args") or {}})
    if budget is not None:
        from ctxpress.methods import with_budget
        entry = with_budget(entry, budget)
    sample = build(entry)                                                # resolve external policies once
    factory = frozen_factory(sample)
    if not dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(log)), exist_ok=True)
    from ctxpress.live.control_receipts import ControlReceipts
    receipts = ControlReceipts(store_dir, log) if tools and sample.agent_tools else None
    srv, _ = serve(factory, port, upstream, via, log, host="127.0.0.1", store_dir=store_dir, store_prefix=store_dir,
                   retrieve_tool=tools, codex_method_tools=tools, control_receipts=receipts)
    port = srv.server_address[1]
    tool_env = dict(CTXPRESS_STORE_DIR=store_dir, CTXPRESS_LOG_PATH=log,
                    CTXPRESS_METHOD_CONFIG=json.dumps(entry),
                    CTXPRESS_REQUIRED_TOOLS='1' if sample.agent_tools else '',
                    CTXPRESS_CONTROL_RECEIPTS='1' if receipts else '')
    env = dict(os.environ)
    env["NO_PROXY"] = ",".join(x for x in [env.get("NO_PROXY", ""), "127.0.0.1", "localhost"] if x)
    env["CTXPRESS_MCP"] = "1" if tools else ""
    env.update(tool_env)
    t0 = time.time()
    started = False
    try:
        cmd = codex_command(codex_bin, port, list(codex_args), tools, write=not dry_run, profile=profile,
                            tool_env=tool_env, codex_config=dict(sample.codex_config, **({"features.executed_tool_call_metadata": True}
                                if tools and sample.agent_tools else {})), prepared=prepared)
        if dry_run:
            return cmd, None
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started = True
        try:
            rc = subprocess.call(cmd, env=env)
        except KeyboardInterrupt:
            rc = 130
    finally:
        if started:
            srv.shutdown()
        srv.server_close()
        if not dry_run:
            try:
                os.remove(os.path.join(codex_home(), f"{profile}.config.toml"))
            except FileNotFoundError:
                pass
    if receipts:
        receipts.audit()
    telemetry = summary(log, t0)
    if telemetry["method_tool_route_failures"]:
        rc = rc or 1
    return cmd, (rc, telemetry)


