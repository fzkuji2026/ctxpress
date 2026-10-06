"""Real Claude Code / MCP / proxy check against a loopback Anthropic Messages fixture (no credentials, no model).

The same scripted steps and expectations as the Codex check (ctxpress.hosts.codex.toolcheck): the fixture model calls
ctxpress_status and the method's own tool, and every following request the proxy forwards must show the method's
rewrite. Claude Code runs with a temporary CLAUDE_CONFIG_DIR and working directory, a dummy API key and
non-essential traffic off, so the user's own Claude Code configuration is neither read nor changed.

    python -m ctxpress.hosts.claude.toolcheck --claude-bin claude --method DTOC
"""
from __future__ import annotations
import argparse, json, os, shutil, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from ctxpress.hosts.codex.toolcheck import steps, observe
from ctxpress.live import anthropic


def sse(message_id, blocks, stop):
    events = [dict(type="message_start", message=dict(id=message_id, type="message", role="assistant", model="fixture-model",
                   content=[], stop_reason=None, stop_sequence=None,
                   usage=dict(input_tokens=50, cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=1)))]
    for i, block in enumerate(blocks):
        if block["type"] == "tool_use":
            events += [dict(type="content_block_start", index=i, content_block=dict(block, input={})),
                       dict(type="content_block_delta", index=i, delta=dict(type="input_json_delta", partial_json=json.dumps(block["input"])))]
        else:
            events += [dict(type="content_block_start", index=i, content_block=dict(type="text", text="")),
                       dict(type="content_block_delta", index=i, delta=dict(type="text_delta", text=block["text"]))]
        events.append(dict(type="content_block_stop", index=i))
    events += [dict(type="message_delta", delta=dict(stop_reason=stop, stop_sequence=None), usage=dict(output_tokens=5)),
               dict(type="message_stop")]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def check(binary, method, directory=None, timeout=120):
    script = steps(method)
    agent_turns, side, errors = [], [], []

    class API(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *args):
            pass
        def do_GET(self):
            raw = json.dumps(dict(data=[], has_more=False)).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path.split("?")[0].endswith("/count_tokens"):
                raw = json.dumps(dict(input_tokens=100)).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw)))
                self.end_headers(); self.wfile.write(raw); return
            names = [t.get("name", "") for t in body.get("tools") or []]
            n = len(agent_turns)
            if not anthropic.rewritable(body) or not any(name.startswith("mcp__ctxpress__") for name in names):
                side.append(body.get("model")); blocks, stop = [dict(type="text", text="ok")], "end_turn"
            else:
                agent_turns.append(body)
                if n < len(script):
                    tool, args = script[n]
                    name = next((x for x in names if x == f"mcp__ctxpress__{tool}"), None)
                    if name is None:
                        errors.append(f"tool {tool} not offered"); blocks, stop = [dict(type="text", text="missing tool")], "end_turn"
                    else:
                        blocks, stop = [dict(type="tool_use", id=f"fixture_{n}", name=name, input=args)], "tool_use"
                else:
                    blocks, stop = [dict(type="text", text="fixture completed")], "end_turn"
            raw = sse(f"msg_{len(agent_turns)}_{len(side)}", blocks, stop)
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Content-Length", str(len(raw)))
            self.end_headers(); self.wfile.write(raw)

    server = HTTPServer(("127.0.0.1", 0), API)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    root = Path(directory or tempfile.gettempdir()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    entry = {"class": method, "args": {"budget": 50} if method == "CWL" else {}}
    try:
        with tempfile.TemporaryDirectory(prefix="claude-tools-", dir=root) as home:
            config, work = Path(home) / "claude-config", Path(home) / "work"
            config.mkdir(); work.mkdir()
            env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_"))}
            env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_API_KEY="ctxpress-offline-fixture",
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1", DISABLE_TELEMETRY="1",
                       CTXPRESS_HOME=str(Path(home) / "ctxpress"), NO_PROXY="localhost,127.0.0.1", no_proxy="localhost,127.0.0.1")
            command = [sys.executable, "-m", "ctxpress", "claude", "--method", method, "--args", json.dumps(entry["args"]),
                       "--upstream", endpoint, "--claude-bin", binary, "--quiet", "--",
                       "-p", "Follow the fixture.", "--model", "fixture-model", "--max-turns", str(len(script) + 2)]
            env["PYTHONPATH"] = os.pathsep.join(p for p in (str(Path(__file__).resolve().parents[3]), env.get("PYTHONPATH", "")) if p)
            try:
                result = subprocess.run(command, env=env, cwd=work, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                        timeout=timeout)   # claude -p reads piped stdin first
                timed_out = False
            except subprocess.TimeoutExpired as expired:
                result, timed_out = expired, True
            rows = []
            for log in (Path(home) / "ctxpress").glob("runs/*/requests.jsonl"):
                rows += [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    finally:
        server.shutdown(); server.server_close()
    requests = [{"input": anthropic.flatten(body)[0]} for body in agent_turns]
    checks = observe(method, requests)
    instructions = all(method_mark(method) in json.dumps(body.get("system")) for body in agent_turns)
    logged = [r for r in rows if "request" in r]
    usage = (len(logged) == len(agent_turns) and all(r.get("dialect") == "anthropic" and r.get("model") == "fixture-model"
             and r.get("response_model") == "fixture-model" and r.get("usage") == dict(input_tokens=50, cached_tokens=0,
             cache_write_tokens=0, output_tokens=5, reasoning_tokens=None) for r in logged))
    passed = (not timed_out and getattr(result, "returncode", 1) == 0 and not errors and bool(checks)
              and all(checks.values()) and instructions and usage)
    return dict(schema="ctxpress.hosts.claude.tool-check", version=1, test_only=True, method=method, binary=binary, passed=passed,
                checks=checks, method_instructions_in_system=instructions, usage_logged=usage, agent_requests=len(agent_turns), side_requests=side,
                errors=errors, returncode=getattr(result, "returncode", None), timed_out=timed_out,
                stderr_tail=(getattr(result, "stderr", "") or "")[-1500:],
                evidence="synthetic loopback with the real Claude Code CLI, ctxpress mcp and proxy; no real model or task quality")


def method_mark(method):
    return {"CWL": "delimiter tool", "DTOC": "manage_context"}[method]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--claude-bin", default=shutil.which("claude") or "claude")
    ap.add_argument("--method", choices=["CWL", "DTOC"], required=True)
    ap.add_argument("--directory"); ap.add_argument("--output")
    a = ap.parse_args(argv)
    result = check(a.claude_bin, a.method, a.directory)
    if a.output:
        Path(a.output).write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=1, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
