"""`ctxpress mcp`: a tiny MCP server (stdio, JSON-RPC, standard library only) that Codex starts by itself.

The launcher starts the proxy and gives this process its run-specific store and log.
This server provides tools only; it never starts a proxy or persistent daemon.
Tools it gives the agent:
  ctxpress_retrieve(id)   the original text of an output the method moved to the dedicated store
  ctxpress_status()       which method is active and what it did so far
  the active method's own agent tools (Method.agent_tools), answered by one method object for this process
"""
from __future__ import annotations
import glob, json, os, re, sys
from collections import deque
from ctxpress import settings
from ctxpress.core.version import __version__

PROTOCOL = "2025-06-18"
TOOLS = [
    {"name": "ctxpress_retrieve", "description": "Return the original text of an earlier tool output that context management "
     "moved out of the conversation. Use the id shown in its placeholder.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"},
                      "session": {"type": "string", "description": "Session identifier shown beside the id in the placeholder."}},
                     "required": ["id"]}},
    {"name": "ctxpress_status", "description": "Show the active context-management method and what it changed so far.",
     "inputSchema": {"type": "object", "properties": {}}},
]


_METHOD = []
_METHOD_RECEIPTS = []


def active_method(force=False):
    """The launch's method, built once per tool process; its agent tools keep their state across calls."""
    if force:
        _METHOD.clear()
    if not _METHOD:
        entry = json.loads(os.environ["CTXPRESS_METHOD_CONFIG"]) if os.environ.get("CTXPRESS_METHOD_CONFIG") else None
        if entry is None:
            cfg = settings.load()
            entry = dict({"class": cfg["method"]}, args=cfg.get("args") or {})
        from ctxpress.methods import build
        _METHOD.append(build(entry))
    return _METHOD[0]



def synchronize_control_history(receipts):
    """Rebuild author tool state from verified direct history on a new MCP process."""
    history = receipts.history()
    identities = [row['receipt'] for row in history]
    if identities == _METHOD_RECEIPTS[:len(identities)]:
        return  # Includes locally executed calls awaiting the next model request.
    from ctxpress.live.control_receipts import digest
    method = active_method(force=True)
    _METHOD_RECEIPTS.clear()
    for row in history:
        text, _ = method.call_tool(row['name'], row['arguments'])
        if digest(text) != row['output']:
            raise ValueError('verified control history cannot reproduce author tool state')
        _METHOD_RECEIPTS.append(row['receipt'])


def tools():
    own = [{key: value for key, value in t.items() if key in ('name', 'description', 'inputSchema', 'annotations')}
           for t in active_method().agent_tools]
    readonly = {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}
    return [dict(t, annotations=readonly) for t in TOOLS] + own


def status():
    cfg = settings.load()
    entry = json.loads(os.environ["CTXPRESS_METHOD_CONFIG"]) if os.environ.get("CTXPRESS_METHOD_CONFIG") else None
    if entry:
        cfg = dict(method=entry["class"], args=entry.get("args") or {})
    rows = []
    paths = [settings.log_path()]
    if not os.environ.get("CTXPRESS_LOG_PATH"):
        paths += glob.glob(os.path.join(settings.home(), "runs", "*", "requests.jsonl"))
    for path in paths:
        try:
            with open(path, encoding="utf-8") as stream:
                for line in deque(stream, maxlen=200):
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue  # A still-running writer can have an incomplete final line.
        except FileNotFoundError:
            continue
    rows = [r for r in rows if "request" in r]
    rows.sort(key=lambda row: row.get("t", 0))
    rows = rows[-200:]
    last = rows[-1] if rows else {}
    return (f"method: {cfg['method']} {json.dumps(cfg.get('args') or {})}\nrecent requests: {len(rows)}\n"
            f"last request: history {last.get('tokens_before')} -> {last.get('tokens_after')} tokens, "
            f"{last.get('changed')} outputs changed, forms {last.get('forms')}")


def retrieve(i, session=None):
    directory = settings.store_dir()
    if session is not None:
        if not isinstance(session, str) or not re.fullmatch(r"[0-9a-f]{32}", session):
            raise ValueError("invalid session identifier")
        directory = os.path.join(directory, session)
    p = os.path.join(directory, f"{int(i)}.txt")
    try:
        return open(p, encoding="utf-8").read()
    except FileNotFoundError:
        return f"no stored output with id {i}"


def handle(msg):
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:                                          # notifications
        return None
    if method == "initialize":
        return dict(jsonrpc="2.0", id=mid, result=dict(protocolVersion=(msg.get("params") or {}).get("protocolVersion", PROTOCOL),
                                                        capabilities={"tools": {}}, serverInfo={"name": "ctxpress", "version": __version__}))
    if method == "ping":
        return dict(jsonrpc="2.0", id=mid, result={})
    if method == "tools/list":
        return dict(jsonrpc="2.0", id=mid, result={"tools": tools()})
    if method == "tools/call":
        p = msg.get("params") or {}
        name, args = p.get("name"), p.get("arguments") or {}
        if name == "ctxpress_retrieve":
            try:
                text = retrieve(args.get("id"), args.get("session"))
            except (ValueError, TypeError) as error:
                return dict(jsonrpc="2.0", id=mid, error={"code": -32602, "message": str(error)})
        elif name == "ctxpress_status":
            text = status()
        elif any(t["name"] == name for t in active_method().agent_tools):
            receipts = None
            if os.environ.get("CTXPRESS_CONTROL_RECEIPTS") == '1':
                from ctxpress.live.control_receipts import ControlReceipts
                receipts = ControlReceipts(settings.store_dir(), settings.log_path())
                synchronize_control_history(receipts)
            text, failed = active_method().call_tool(name, args)
            content = [{"type": "text", "text": text}]
            if receipts:
                from ctxpress.live.control_receipts import MARKER
                marker = receipts.issue(name, args, text, failed)
                _METHOD_RECEIPTS.append(':'.join(MARKER.fullmatch(marker).groups()))
                content.append(dict(type='text', text=marker))
            return dict(jsonrpc="2.0", id=mid, result={"content": content, "isError": bool(failed)})
        else:
            return dict(jsonrpc="2.0", id=mid, error={"code": -32602, "message": f"unknown tool {name}"})
        return dict(jsonrpc="2.0", id=mid, result={"content": [{"type": "text", "text": text}], "isError": False})
    return dict(jsonrpc="2.0", id=mid, error={"code": -32601, "message": f"method not found: {method}"})


def main():
    """Tools only; the proxy is started by `ctxpress codex` (the `codex` command after `ctxpress install codex`)."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except Exception as e:
            reply = dict(jsonrpc="2.0", id=None, error={"code": -32700, "message": repr(e)})
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n"); sys.stdout.flush()


if __name__ == "__main__":
    main()
