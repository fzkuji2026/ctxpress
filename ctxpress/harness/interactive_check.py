"""Ordinary interactive sessions through ctxpress: the real Claude Code or Codex TUI, driven by keystrokes in tmux.

`ctxpress claude` / `ctxpress codex` start as a user starts them (no -p, no exec) against a loopback model that
speaks Anthropic Messages and OpenAI Responses. Turn 1: the user types a message; the model calls ctxpress_status
(served by `ctxpress mcp`), then answers. Turn 2: the user types again; that request must carry both earlier
messages, the answer, the tool output rewritten by the method (DTOC's tool_key envelope) and the method's
instructions. Then the user quits the TUI and the launcher must exit cleanly. Configuration lives in temporary
CLAUDE_CONFIG_DIR / CODEX_HOME directories (onboarding and folder trust pre-accepted there); credentials are dummies.
Needs Linux or WSL with tmux. Synthetic evidence: no real model or task quality.

    python -m ctxpress.harness.interactive_check --host claude --bin claude
    python -m ctxpress.harness.interactive_check --host codex --bin codex
"""
from __future__ import annotations
import argparse, json, os, shlex, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

KEY = "ctxpress-offline-fixture-key-000000000000"
FIRST, SECOND = "Please check the ctxpress status", "Thanks, now summarize what you saw"


def tmux(*args, check=True):
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check)


class Fixture:
    """A scripted model: in each user turn, call ctxpress_status once (first turn only), then answer in text."""

    def __init__(self):
        self.turns, self.seen, self.lock = [], [], threading.Lock()

    def answer(self, dialect, body):
        """(kind, value): ('tool', name) or ('text', text) for a main-agent request; None for side requests."""
        if dialect == "anthropic":
            tools = [t.get("name", "") for t in body.get("tools") or []]
            msgs = body.get("messages") or []
            last = msgs[-1] if msgs else {}
            from_tool = isinstance(last.get("content"), list) and any(b.get("type") == "tool_result" for b in last["content"])
            users = [m for m in msgs if m.get("role") == "user" and not (isinstance(m.get("content"), list) and
                     all(b.get("type") == "tool_result" for b in m["content"]))]
            status = next((t for t in tools if t.endswith("ctxpress_status")), None)
        else:
            tools = [(t.get("name"), u.get("name")) for t in body.get("tools") or [] if t.get("type") == "namespace"
                     for u in t.get("tools") or []]
            items = body.get("input") or []
            from_tool = bool(items) and items[-1].get("type") == "function_call_output"
            users = [x for x in items if x.get("role") == "user"]
            status = next(((ns, n) for ns, n in tools if n == "ctxpress_status"), None)
        typed = json.dumps(users[-1] if users else {}, ensure_ascii=False)
        structured = ((body.get("text") or {}).get("format") or {}).get("type") == "json_schema"   # e.g. Codex's task title
        if not status or structured or not (from_tool or FIRST in typed or SECOND in typed):
            return None                               # side calls (e.g. Claude Code's next-prompt suggestion)
        with self.lock:
            self.turns.append((dialect, body))
            n = len(self.turns)
        if not from_tool and n == 1:
            return ("tool", status)
        return ("text", f"fixture answer {n}")


def sse_anthropic(kind, value, n):
    if kind == "tool":
        block = dict(type="tool_use", id=f"toolu_fixture_{n}", name=value, input={})
        start, delta, stop = dict(block, input={}), dict(type="input_json_delta", partial_json="{}"), "tool_use"
    else:
        start, delta, stop = dict(type="text", text=""), dict(type="text_delta", text=value), "end_turn"
    events = [dict(type="message_start", message=dict(id=f"msg_{n}", type="message", role="assistant", model="fixture-model",
                   content=[], stop_reason=None, stop_sequence=None, usage=dict(input_tokens=50, output_tokens=1))),
              dict(type="content_block_start", index=0, content_block=start), dict(type="content_block_delta", index=0, delta=delta),
              dict(type="content_block_stop", index=0),
              dict(type="message_delta", delta=dict(stop_reason=stop, stop_sequence=None), usage=dict(output_tokens=5)),
              dict(type="message_stop")]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def sse_responses(kind, value, n):
    if kind == "tool":
        item = dict(id=f"fc_{n}", type="function_call", status="completed", call_id=f"call_fixture_{n}", name=value[1],
                    namespace=value[0], arguments="{}")
    else:
        item = dict(id=f"msg_{n}", type="message", status="completed", role="assistant",
                    content=[dict(type="output_text", text=value, annotations=[])])
    response = dict(id=f"resp_{n}", object="response", created_at=1, status="completed", model="fixture-model", output=[item],
                    usage=dict(input_tokens=50, output_tokens=5, total_tokens=55, input_tokens_details=dict(cached_tokens=0)))
    events = [dict(type="response.created", response=dict(response, status="in_progress", output=[])),
              dict(type="response.output_item.added", output_index=0, item=dict(item, status="in_progress")),
              dict(type="response.output_item.done", output_index=0, item=item), dict(type="response.completed", response=response)]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def serve(fixture):
    class API(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a):
            pass
        def reply(self, raw, kind="application/json"):
            self.send_response(200); self.send_header("Content-Type", kind); self.send_header("Content-Length", str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        def do_GET(self):
            self.reply(json.dumps(dict(data=[], models=[], object="list", has_more=False)).encode())
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            path = self.path.split("?")[0]
            if path.endswith("/count_tokens"):
                return self.reply(json.dumps(dict(input_tokens=100)).encode())
            dialect = "anthropic" if path.endswith("/messages") else "responses"
            step = fixture.answer(dialect, body)
            n = len(fixture.turns)
            last = json.dumps((body.get("messages") or body.get("input") or [{}])[-1], ensure_ascii=False)
            fixture.seen.append(dict(t=round(time.time(), 1), path=path, model=body.get("model"), stream=body.get("stream"),
                                     main=step is not None, tools=len(body.get("tools") or []), last=last[-160:]))
            kind, value = step or ("text", "ok")
            if dialect == "anthropic" and not body.get("stream"):      # side calls may ask for one JSON message
                return self.reply(json.dumps(dict(id=f"msg_{n}", type="message", role="assistant", model=body.get("model"),
                    content=[dict(type="text", text=value)], stop_reason="end_turn", stop_sequence=None,
                    usage=dict(input_tokens=5, output_tokens=1))).encode())
            raw = (sse_anthropic if dialect == "anthropic" else sse_responses)(kind, value, n).encode()
            self.reply(raw, "text/event-stream")
    server = HTTPServer(("127.0.0.1", 0), API)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def prepare(host, home, work, endpoint, binary):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_", "CODEX_", "OPENAI_"))}
    env.update(CTXPRESS_HOME=str(home / "ctxpress"), NO_PROXY="localhost,127.0.0.1", no_proxy="localhost,127.0.0.1",
               PYTHONPATH=os.pathsep.join(p for p in (str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH", "")) if p),
               TERM="xterm-256color")
    if host == "claude":
        config = home / "claude-config"; config.mkdir()
        (config / ".claude.json").write_text(json.dumps({
            "hasCompletedOnboarding": True, "theme": "dark", "customApiKeyResponses": {"approved": [KEY[-20:]], "rejected": []},
            "projects": {str(work): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True, "allowedTools": []}}}), encoding='utf-8')
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_API_KEY=KEY, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_AUTOUPDATER="1")
        command = [sys.executable, "-m", "ctxpress", "claude", "--method", "DTOC", "--upstream", endpoint, "--claude-bin", binary,
                   "--", "--model", "fixture-model"]
    else:
        codex_home = home / "codex-home"; codex_home.mkdir()
        (codex_home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": KEY}), encoding='utf-8')
        (codex_home / "config.toml").write_text(f'model = "fixture-model"\n\n[projects."{work}"]\ntrust_level = "trusted"\n', encoding='utf-8')
        env.update(CODEX_HOME=str(codex_home), OPENAI_API_KEY=KEY)
        command = [sys.executable, "-m", "ctxpress", "codex", "--method", "DTOC", "--upstream", endpoint, "--codex-bin", binary,
                   "--", "--model", "fixture-model"]
    return env, command


def wait(predicate, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.5)
    return False


def check(host, binary, directory=None, timeout=60):
    fixture = Fixture()
    server = serve(fixture)
    endpoint = f"http://127.0.0.1:{server.server_port}"
    session = f"ctxpress-{host}-{os.getpid()}"
    root = Path(directory or tempfile.gettempdir()).resolve(); root.mkdir(parents=True, exist_ok=True)
    pane, exited, problems, rows, code, warnings = "", False, [], [], None, ""
    try:
        with tempfile.TemporaryDirectory(prefix=f"interactive-{host}-", dir=root) as h:
            home = Path(h); work = home / "work"; work.mkdir()
            env, command = prepare(host, home, work, endpoint, binary)
            exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items() if k.isidentifier())
            done = home / "exit-code"
            shell = f"cd {shlex.quote(str(work))} && env -i {exports} {shlex.join(command)}; echo $? > {shlex.quote(str(done))}"
            tmux("new-session", "-d", "-s", session, "-x", "200", "-y", "50", "bash", "-lc", shell)
            capture = lambda: tmux("capture-pane", "-p", "-S", "-200", "-t", session, check=False).stdout
            # Start-up: in an isolated config Claude Code accepts a submission only after ~45 s (earlier Enter adds
            # a line); Codex is ready in seconds.
            time.sleep(60 if host == "claude" else 12)
            tmux("send-keys", "-t", session, "-l", FIRST); time.sleep(1.5); tmux("send-keys", "-t", session, "Enter")
            if not wait(lambda: len(fixture.turns) >= 2 and "fixture answer" in capture(), timeout):
                problems.append("first turn did not finish")
            time.sleep(1)
            tmux("send-keys", "-t", session, "-l", SECOND); time.sleep(1.5); tmux("send-keys", "-t", session, "Enter")
            if not wait(lambda: len(fixture.turns) >= 3, timeout):
                problems.append("second turn did not reach the model")
            wait(lambda: "fixture answer 3" in capture(), 15)
            pane = capture()
            warnings = ""
            if host == "codex" and "warning" in pane:              # what the TUI warns about (f2 opens the list)
                tmux("send-keys", "-t", session, "F2", check=False); time.sleep(2)
                warnings = capture()
                for _ in range(3):                                   # the other warnings, one per page
                    tmux("send-keys", "-t", session, "k", check=False); time.sleep(1)
                    warnings += "\n" + capture().split("Warnings ·")[-1]
                tmux("send-keys", "-t", session, "Escape", check=False); time.sleep(1)
            for keys in (["-l", "/exit" if host == "claude" else "/quit"], ["Enter"], ["C-c"], ["C-c"], ["C-d"]):
                if done.exists():
                    break
                tmux("send-keys", "-t", session, *keys, check=False); time.sleep(1.5)
            exited = wait(done.exists, 20)
            code = done.read_text(encoding='utf-8').strip() if exited else None
            rows = [json.loads(line) for log in (home / "ctxpress").glob("runs/*/requests.jsonl")
                    for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    finally:
        tmux("kill-session", "-t", session, check=False)
        server.shutdown(); server.server_close()
    checks = observe(fixture.turns)
    managed = [r for r in rows if "request" in r]
    checks["no_history_reset"] = bool(managed) and not any(r.get("history_rebased") for r in managed)
    passed = not problems and all(checks.values()) and exited and code == "0"
    return dict(schema="ctxpress.interactive-check", version=1, test_only=True, host=host, binary=binary, passed=passed,
                checks=checks, problems=problems, model_requests=len(fixture.turns), requests=fixture.seen, exited=exited, exit_code=code,
                managed_requests=len(managed), forks_undone=max((r.get("forks_undone") or 0 for r in managed), default=0),
                screen_tail=pane[-1500:], host_warnings=warnings[-2500:], evidence="real TUI driven by keystrokes through ctxpress; loopback model, dummy credentials")


def observe(turns):
    """The request answering the second message must show the whole conversation, rewritten by DTOC."""
    if len(turns) < 3:
        return dict(three_model_requests=False)
    dialect, body = turns[-1]
    text = json.dumps(body, ensure_ascii=False)
    if dialect == "anthropic":
        results = [b for m in body["messages"] for b in (m["content"] if isinstance(m["content"], list) else [])
                   if b.get("type") == "tool_result"]
        outputs = [r["content"] if isinstance(r["content"], str) else "".join(x.get("text", "") for x in r["content"]) for r in results]
        instructions = "manage_context" in json.dumps(body.get("system"))
    else:
        from ctxpress.live.rewrite import output_text
        outputs = [output_text(x) for x in body.get("input") or [] if x.get("type") == "function_call_output"]
        instructions = any(x.get("role") == "developer" and "manage_context" in json.dumps(x) for x in body.get("input") or [])
    def envelope(o):
        try:
            value = json.loads(o)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}
    return dict(first_message_kept=FIRST in text, first_answer_kept="fixture answer 2" in text, second_message_sent=SECOND in text,
                tool_output_rewritten=len(outputs) == 1 and envelope(outputs[0]).get("tool_key") == "tk_001"
                and "method: DTOC" in envelope(outputs[0]).get("tool_result", ""),
                method_instructions=instructions)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", choices=["claude", "codex"], required=True)
    ap.add_argument("--bin", required=True); ap.add_argument("--directory"); ap.add_argument("--output")
    a = ap.parse_args(argv)
    result = check(a.host, a.bin, a.directory)
    if a.output:
        Path(a.output).write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=1, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
