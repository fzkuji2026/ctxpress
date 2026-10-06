"""Plug-and-play path end to end, offline: `ctxpress codex` starts the proxy and a (fake) Codex; the fake sends a
real-format Responses request to the base URL it was given; a fake upstream records what arrives."""
import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from ctxpress.hosts.codex import launch
from ctxpress.methods import build, method_table, REGISTRY, METHODS
from ctxpress.live.context import LiveContext
import ctxpress.methods as M
from ctxpress.methods.scored import ScoredMethod
from ctxpress.methods.wrappers import Composed, EntryTruncation, PinRequirements, WithMemory, Trigger

HERE = os.path.dirname(__file__)


def fake_upstream():
    got = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.append((self.path, json.loads(body), self.headers.get("Authorization")))
            sse = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":123,"input_tokens_details":{"cached_tokens":45},"output_tokens":6}}}\n\n'
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Content-Length", str(len(sse)))
            self.end_headers(); self.wfile.write(sse)
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, got


def history(n):
    items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task"}]}]
    for i in range(n):
        items.append({"type": "function_call", "call_id": f"c{i}", "name": "exec", "arguments": f"cat a/f{i}.go"})
        items.append({"type": "function_call_output", "call_id": f"c{i}", "output": f"content {i} " * 100})
    return items


def test_codex_launcher_end_to_end():
    srv, got = fake_upstream()
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "in.json"); json.dump(history(6), open(inp, "w", encoding='utf-8'))
        os.environ["FAKE_INPUT"] = inp
        os.environ["CODEX_HOME"] = os.path.join(d, "codex")
        cmd, (rc, summ) = launch.run({"class": "ComplexityTrap", "args": {"n": 2}}, ["exec", "hi"],
                                     codex_bin=[sys.executable, os.path.join(HERE, "fake_codex.py")],
                                     upstream=f"http://127.0.0.1:{srv.server_address[1]}", via=None, log=os.path.join(d, "log.jsonl"))
    srv.shutdown()
    assert rc == 0 and len(got) == 1
    path, body, auth = got[0]
    assert path == "/responses" and auth == "Bearer test-token"            # forwarded unchanged
    outs = [x for x in body["input"] if x["type"] == "function_call_output"]
    assert [x["output"].startswith("Old environment output") for x in outs] == [True] * 4 + [False] * 2
    assert summ["requests"] == 1 and summ["api_input_tokens"] == 123 and summ["api_cached_tokens"] == 45
    log = open(os.path.join(d, "log.jsonl"), encoding='utf-8').read() if os.path.exists(os.path.join(d, "log.jsonl")) else ""
    assert "test-token" not in log                                         # headers are never logged


def feed(method, n=6, size=200):
    ctx = LiveContext(method)
    ctx.add_message("user", "task")
    for i in range(n):
        ctx.add_call(f"c{i}", f"sed -n 1,200p docs/srs/m_SRS.md" if i == 0 else f"cat a/f{i}.go")
        ctx.add_output(f"c{i}", "x\n" * size)
    return ctx, ctx.before_request()


def test_scored_method_respects_budget():
    ctx, view = feed(ScoredMethod(budget=600, score="recency", protect=1))
    outs = [v for v in view if v["seg"] == "out"]
    assert outs[-1]["form"] == "full" and outs[0]["form"] == "placeholder"
    assert sum(ctx.seg_size(s) for s in ctx.outputs()) <= 600


def test_wrappers():
    _, view = feed(PinRequirements(M.ComplexityTrap(1)))
    outs = [v for v in view if v["seg"] == "out"]
    assert outs[0]["form"] == "full" and outs[1]["form"] == "placeholder"   # the SRS stays
    _, view = feed(WithMemory(M.ComplexityTrap(1), label=True))
    assert [v for v in view if v["seg"] == "out"][1]["form"] == "memlabel"
    _, view = feed(Trigger(M.ComplexityTrap(1), threshold=10 ** 6))
    assert all(v["form"] == "full" for v in view if v["seg"] == "out")
    _, view = feed(EntryTruncation(M.NoCompaction(), budget=100), size=2000)
    assert all(v["form"] == "truncated" for v in view if v["seg"] == "out")
    m = build({"class": "Composed", "args": {"methods": [{"class": "EntryTruncation", "args": {"inner": {"class": "NoCompaction"}, "budget": 100}},
                                                         {"class": "ComplexityTrap", "args": {"n": 1}}]}})
    _, view = feed(m, size=2000)
    forms = [v["form"] for v in view if v["seg"] == "out"]
    assert forms[-1] == "truncated" and set(forms[:-1]) == {"placeholder"}


def test_method_table_covers_registry():
    assert set(REGISTRY) <= set(METHODS)
    assert "ComplexityTrap" in method_table()


def test_clawvm_selects_under_budget():
    ctx = LiveContext(M.ClawVM(budget=300))
    ctx.add_message("user", "task")
    for i in range(8):                                                        # one request per tool call, as live
        ctx.add_call(f"c{i}", "sed -n 1,200p docs/srs/m_SRS.md" if i == 0 else f"cat a/f{i}.go")
        ctx.add_output(f"c{i}", "".join(f"line {i} {j}\n" for j in range(60)))
        view = ctx.before_request()
    outs = [v for v in view if v["seg"] == "out"]
    assert outs[-1]["form"] == "full"                                         # demanded: the newest output
    assert outs[0]["form"] != "memid"                                         # the SRS: hard-pinned constraint page
    assert "memid" in {v["form"] for v in outs[1:-1]}                         # older outputs fall to pointers
    assert sum(ctx.seg_size(s) for s in ctx.outputs() if s["form"] != "memid") <= 300   # pages above pointer fit


def test_cliff_rewrites_requests():
    from ctxpress.live.rewrite import Rewriter
    rw = Rewriter(lambda: M.CliffCompaction(t=1500))
    body, info = rw.rewrite_body({"model": "m", "input": history(8)}, "s")
    inp = body["input"]
    assert info["changed"] and inp[1]["role"] == "user" and inp[1]["content"][0]["text"].startswith("The following is a summary")
    assert [x["type"] for x in inp[2:]] == ["function_call", "function_call_output"] * 3      # keep_recent = 3


def test_swepruner_prunes_annotated_outputs():
    from ctxpress.live.rewrite import Rewriter
    from ctxpress.methods.swepruner import SWEPruner, FILTERED, focus_question
    calls = []

    def fake(query, code, threshold, overlap):
        calls.append(query)
        return dict(pruned_code="(filtered 9 lines)\nkept line", origin_token_cnt=100, left_token_cnt=10)
    assert focus_question('{"cmd": "nl -ba a.py  # context_focus_question: where is x?"}') == "where is x?"
    items = [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "dev"}]},
             {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task"}]},
             {"type": "function_call", "call_id": "a", "name": "exec_command", "arguments": '{"cmd": "nl -ba a.py  # context_focus_question: where is x?"}'},
             {"type": "function_call_output", "call_id": "a", "output": "line\n" * 300},
             {"type": "function_call", "call_id": "b", "name": "exec_command", "arguments": '{"cmd": "ls"}'},
             {"type": "function_call_output", "call_id": "b", "output": "x\n" * 300}]
    rw = Rewriter(lambda: SWEPruner(pruner=fake))
    body, _ = rw.rewrite_body({"model": "m", "input": items}, "s")
    out = body["input"]
    assert out[1]["role"] == "developer" and "context_focus_question" in out[1]["content"][0]["text"]   # instructions
    assert out[4]["output"] == FILTERED + "(filtered 9 lines)\nkept line" and out[6]["output"] == "x\n" * 300
    assert calls == ["where is x?"]
