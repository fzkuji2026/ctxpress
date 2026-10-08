"""Measure a Codex CLI's own auto-compact threshold for one model, without overriding it.

    python -m ctxpress.hosts.codex.threshold --codex-bin /pinned/bin/codex --model gpt-6.1-sol \\
        --model-catalog /frozen/models.json [--start 220000 --step 2000]

A loopback Responses API answers each turn with a code-mode shell call and reports a growing input usage; the first
request Codex marks as compaction (request_kind=compaction, or /responses/compact) brackets the threshold. Isolated
CODEX_HOME, dummy key, proxies removed; no model, network or user configuration is involved. Synthetic evidence:
real runs confirm the threshold from their own logs. Run once per pinned host release.
"""
from __future__ import annotations
import argparse, json, os, subprocess, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def request_kind(headers, body):
    for raw in (headers.get("x-codex-turn-metadata"), (body.get("client_metadata") or {}).get("x-codex-turn-metadata")):
        try:
            meta = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        if isinstance(meta, dict) and meta.get("request_kind"):
            return meta["request_kind"]
    return None


def tool_names(body):
    declared = list(body.get("tools") or [])
    for entry in body.get("input") or []:
        if isinstance(entry, dict) and entry.get("type") == "additional_tools":
            declared += entry.get("tools") or []
    return {t.get("name") for t in declared if isinstance(t, dict)}


def measure(binary, model, catalog=None, start=220000, step=2000, turns=40, timeout=240):
    state = dict(n=0, reported=[], first=None)

    class API(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            compaction = self.path.rstrip("/").endswith("/compact") or request_kind(self.headers, body) == "compaction"
            if compaction and state["first"] is None:
                state["first"] = dict(request=state["n"], last_reported_total=state["reported"][-1] if state["reported"] else None,
                                      previous_reported_total=state["reported"][-2] if len(state["reported"]) > 1 else None)
            names = tool_names(body)
            if compaction or state["first"] is not None or state["n"] >= turns:
                item = dict(id=f"msg_{state['n']}", type="message", status="completed", role="assistant",
                            content=[dict(type="output_text", text="done", annotations=[])])
            elif "functions" in names:                     # code-mode hosts
                item = dict(id=f"ct_{state['n']}", type="custom_tool_call", status="completed", call_id=f"call_{state['n']}",
                            name="exec", input='text(await tools.exec_command({cmd:"echo ok",max_output_tokens:50}));')
            else:
                tool = next((t for t in ("shell", "exec_command", "local_shell") if t in names), "shell")
                args = {"command": ["bash", "-lc", "echo ok"]} if tool == "shell" else {"cmd": "echo ok"}
                item = dict(id=f"fc_{state['n']}", type="function_call", status="completed", call_id=f"call_{state['n']}",
                            name=tool, arguments=json.dumps(args))
            reported = start + step * state["n"]
            state["reported"].append(reported + 100)
            state["n"] += 1
            usage = dict(input_tokens=reported, output_tokens=100, total_tokens=reported + 100,
                         input_tokens_details=dict(cached_tokens=0), output_tokens_details=dict(reasoning_tokens=0))
            response = dict(id=f"resp_{state['n']}", object="response", created_at=1, status="completed", model=model,
                            output=[item], usage=usage)
            events = [dict(type="response.created", response=dict(response, status="in_progress", output=[])),
                      dict(type="response.output_item.added", output_index=0, item=item),
                      dict(type="response.output_item.done", output_index=0, item=item),
                      dict(type="response.completed", response=response)]
            raw = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
            self.send_response(200); self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    server = HTTPServer(("127.0.0.1", 0), API)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        with tempfile.TemporaryDirectory(prefix="ctxpress-threshold-", dir=os.path.expanduser("~")) as home:
            lines = [f"model = {json.dumps(model)}", 'model_reasoning_effort = "medium"', 'model_provider = "probe"',
                     'approval_policy = "never"', 'sandbox_mode = "danger-full-access"']
            if catalog:
                lines.append(f"model_catalog_json = {json.dumps(os.path.abspath(catalog))}")
            lines += ["[features]", "enable_request_compression = false", "[model_providers.probe]", 'name = "probe"',
                      f"base_url = {json.dumps(endpoint)}", 'env_key = "CTXPRESS_THRESHOLD_KEY"', 'wire_api = "responses"',
                      "supports_websockets = false"]
            with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            env = {key: value for key, value in os.environ.items() if key.upper() not in
                   ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "OPENAI_API_KEY", "CODEX_API_KEY")}
            env.update(CODEX_HOME=home, CTXPRESS_THRESHOLD_KEY="dummy", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
            work = tempfile.mkdtemp(prefix="work-", dir=home)
            try:
                code = subprocess.run([binary, "--no-daemon", "exec", "--skip-git-repo-check", "Run echo ok repeatedly."],
                                      cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                      timeout=timeout).returncode
            except subprocess.TimeoutExpired:
                code = "timeout"
    finally:
        server.shutdown(); server.server_close()
    version = subprocess.run([binary, "--version"], capture_output=True, text=True).stdout.strip()
    first = state["first"]
    return dict(schema="ctxpress.hosts.codex.threshold", version=1, test_only=True, codex_version=version, model=model,
                model_catalog=catalog, returncode=code, requests=state["n"], measured=first is not None,
                threshold_between=[first["previous_reported_total"], first["last_reported_total"]] if first else None,
                evidence="synthetic loopback usage; the real threshold is confirmed from run logs")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m ctxpress.hosts.codex.threshold")
    ap.add_argument("--codex-bin", required=True); ap.add_argument("--model", required=True)
    ap.add_argument("--model-catalog"); ap.add_argument("--start", type=int, default=220000)
    ap.add_argument("--step", type=int, default=2000); ap.add_argument("--output")
    a = ap.parse_args(argv)
    result = measure(a.codex_bin, a.model, a.model_catalog, a.start, a.step)
    text = json.dumps(result, indent=2)
    if a.output:
        with open(a.output, "x", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0 if result["measured"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
