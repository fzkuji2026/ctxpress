"""Process telemetry recorded by the rewriter: prefix kept against the previous request, host edits, composition."""
import pytest
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import build


def turn(n, size=400):
    return [{"type": "function_call", "call_id": f"c{n}", "name": "shell", "arguments": f'{{"cmd": "step {n}"}}'},
            {"type": "function_call_output", "call_id": f"c{n}", "output": f"result {n} " + "x" * size}]


def history(n):
    items = [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]},
             {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix it"}]}]
    for i in range(n):
        items += turn(i)
    return items


def send(rw, items, tools="a"):
    turn_metadata = {"turn": len(items)}          # changes every request but is not part of the prompt
    _, info = rw.rewrite_body({"input": items, "tools": [tools], "model": "m", "client_metadata": turn_metadata}, "s")
    return info


def test_untouched_history_keeps_the_whole_previous_prefix():
    rw = Rewriter(lambda: build({"class": "NoCompaction"}))
    first = send(rw, history(1))
    assert first["prefix"] is None and first["host_prefix"] is None and first["fixed_changed"] is None
    second = send(rw, history(2))
    assert second["prefix"] == dict(previous_items=4, kept_items=4, kept_tokens=second["prefix"]["kept_tokens"], edited=False)
    assert second["prefix"]["kept_tokens"] > 0 and second["host_prefix"]["edited"] is False and second["fixed_changed"] is False
    assert set(second["composition"]) == {"instructions", "user", "tool_call", "tool_output"}
    assert second["rewrite_seconds"] >= 0


def test_a_method_edit_shows_as_a_shorter_kept_prefix_without_a_host_edit():
    rw = Rewriter(lambda: build({"class": "ComplexityTrap", "args": {"n": 1}}))
    send(rw, history(1)); send(rw, history(2))
    info = send(rw, history(3))                    # the oldest output is now replaced by a placeholder
    assert info["prefix"]["edited"] and info["prefix"]["kept_items"] < info["prefix"]["previous_items"]
    assert info["host_prefix"]["edited"] is False


def test_a_host_revision_and_a_changed_tool_list_are_recorded():
    rw = Rewriter(lambda: build({"class": "NoCompaction"}))
    send(rw, history(2))
    revised = history(2)
    revised[1] = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "something else"}]}
    info = send(rw, revised, tools="b")
    assert info["host_prefix"] == dict(previous_items=6, kept_items=1, edited=True)
    assert info["prefix"]["kept_items"] == 1 and info["fixed_changed"] is True


def test_history_only_bodies_do_not_claim_a_fixed_part():
    rw = Rewriter(lambda: build({"class": "NoCompaction"}))
    rw.rewrite_body({"input": history(1)}, "s")
    _, info = rw.rewrite_body({"input": history(2)}, "s")
    assert info["fixed_changed"] is None


def test_model_calls_forwarded_unchanged_are_logged_billed_and_kept_distinct(tmp_path):
    import json, threading, urllib.request
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from ctxpress.live import usage as eval_usage
    from ctxpress.harness import run_analysis
    from ctxpress.live.proxy import serve

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if self.path.endswith("/messages"):
                value = dict(model="haiku", usage=dict(input_tokens=40, cache_read_input_tokens=10, output_tokens=3))
            else:
                value = dict(model="m", usage=dict(input_tokens=50, output_tokens=5, input_tokens_details=dict(cached_tokens=20)))
            raw = json.dumps(value).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    upstream = HTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    log = tmp_path / "requests.jsonl"
    proxy, _ = serve(lambda: build({"class": "NoCompaction"}), 0, f"http://127.0.0.1:{upstream.server_port}", host="127.0.0.1", log=str(log))
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def post(path, body):
        request = urllib.request.Request(f"http://127.0.0.1:{proxy.server_port}{path}", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=5) as response:
            return response.read()
    try:
        post("/v1/responses", dict(model="m", input="a plain string input is not rewritten"))
        post("/v1/messages", dict(model="haiku", max_tokens=5, messages=[dict(role="user", content="title?")]))   # no tools
        post("/v1/responses", dict(model="m", input=history(1)))
    finally:
        proxy.shutdown(); proxy.server_close(); upstream.shutdown(); upstream.server_close()
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    side = [r for r in rows if r.get("type") == "passthrough"]
    assert [(r["dialect"], r["model"], r["status"], r["response_model"]) for r in side] == [
        ("responses", "m", 200, "m"), ("anthropic", "haiku", 200, "haiku")]
    assert side[0]["usage"]["input_tokens"] == 50 and side[1]["usage"]["input_tokens"] == 50     # 40 + 10 cache reads
    # Official accounting bills them too, counted and priced as their own role.
    usage = eval_usage.analyze(dict(model="m", requests=1, rewrites=rows, usage=dict(summary_calls=0)))
    assert usage["complete"] and usage["api_input_tokens"] == 150 and usage["passthrough_calls"] == 2
    assert usage["usage_by_model"]["haiku"]["passthrough_calls"] == 1 and usage["usage_by_model"]["m"]["main_requests"] == 1
    prices = {"models": {name: {"input": 1.0, "cached": 0.1, "output": 2.0, "unit": "USD_per_million_tokens", "source": "t",
                                "as_of": "2026-10-06"} for name in ("m", "haiku")}}
    bill = eval_usage.bill(usage, prices)
    roles = bill["api_cost_by_role"]
    assert roles["summary"] == roles["native_compaction"] == 0.0 and roles["main"] > 0 and roles["passthrough"] > 0
    assert sum(roles.values()) == pytest.approx(bill["api_cost_at_declared_rates_usd"])
    # Without a declared rate for the side-call model the total is unknown, but the main role still prices.
    partial = eval_usage.bill(usage, {"models": {"m": prices["models"]["m"]}})
    assert partial["api_cost_at_declared_rates_usd"] is None and partial["missing_model_rates"] == ["haiku"]
    assert partial["api_cost_by_role"]["main"] == pytest.approx(roles["main"]) and partial["api_cost_by_role"]["passthrough"] is None
    metrics, _ = run_analysis.job_metrics(*run_analysis.split_rows(rows), prices, {})
    assert metrics["efficiency"]["side_calls"] == 2 and metrics["checks"]["cost_parity"] is True
    assert metrics["cost"]["total"] == pytest.approx(bill["api_cost_at_declared_rates_usd"])
    assert metrics["cost"]["side"] == pytest.approx(roles["passthrough"])
