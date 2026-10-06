"""DTOC: tool keys, envelopes, manage_context results and timing, host requests, MCP."""
import json, os, shutil, sys

import pytest
from ctxpress.live import mcp
from ctxpress.live.context import LiveContext
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import DTOC, build

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA = os.environ.get("CTXPRESS_DATA", os.path.join(ROOT, "data"))


def run(rounds):
    """rounds: lists of (call_id, tool, output-or-manage-args)."""
    ctx = LiveContext(DTOC())
    ctx.add_message("user", "task")
    views = []
    for rnd in rounds:
        for cid, tool, value in rnd:
            args = value if tool == "manage_context" else {"path": "f"}
            ctx.add_call(cid, json.dumps(args), name=tool, args=json.dumps(args))
        for cid, tool, value in rnd:
            ctx.add_output(cid, "recorded" if tool == "manage_context" else value)
            ctx.ctx[-1]["t"] = 1.5
        ctx.before_request()
        views.append({s["call_id"]: v["text"] for v, s in zip(ctx.view(), ctx.pinned + ctx.ctx) if v["seg"] == "out"})
    return views


def test_keys_envelopes_and_hiding():
    views = run([[("a", "read", "x" * 40)], [("b", "bash", "ok")],
                 [("m", "manage_context", {"enable": [], "disable": ["tk_001", "tk_009"]})],
                 [("m2", "manage_context", {"enable": ["tk_001"], "disable": []})]])
    assert json.loads(views[0]["a"]) == {"tool_key": "tk_001", "tool": "read", "estimated_tokens": 10, "tool_result": "x" * 40}
    assert json.loads(views[2]["a"]) == {"tool_key": "tk_001", "tool": "read", "estimated_tokens": 10, "timestamp": 1500,
                                         "status": "hidden"}
    assert json.loads(views[2]["m"])["tool_result"] == ("Disabled: tk_001\nStatus: 1 visible, 1 hidden tool outputs\n"
                                                        "Tokens saved (cumulative): 10\nNot found: tk_009")
    assert json.loads(views[2]["m"])["tool_key"] == "tk_003"
    assert json.loads(views[3]["a"])["tool_result"] == "x" * 40
    # m2's own output registers when the next request is assembled
    assert json.loads(views[3]["m2"])["tool_result"].startswith("Re-enabled: tk_001\nStatus: 3 visible, 0 hidden")


def test_manage_sees_only_keys_of_the_previous_request():
    views = run([[("a", "read", "aaaa")], [("b", "read", "bbbb"), ("m", "manage_context", {"enable": [], "disable": ["tk_002"]})]])
    # in the same round b has no key yet when manage_context runs; afterwards b is tk_002 and m is tk_003
    assert "Not found: tk_002" in json.loads(views[1]["m"])["tool_result"]
    assert json.loads(views[1]["b"])["tool_key"] == "tk_002" and "status" not in json.loads(views[1]["b"])


def test_invalid_call_gets_no_key():
    views = run([[("a", "read", "aaaa")], [("m", "manage_context", {"disable": ["tk_001"]})], [("b", "read", "bbbb")]])
    assert views[1]["m"] == "recorded" and json.loads(views[2]["b"])["tool_key"] == "tk_002"
    assert "status" not in json.loads(views[2]["a"])


def test_host_request_carries_envelopes_and_placeholders():
    inputs = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task"}]},
              {"type": "function_call", "call_id": "a", "name": "shell", "arguments": "{}"},
              {"type": "function_call_output", "call_id": "a", "output": "file contents"},
              {"type": "function_call", "call_id": "m", "name": "mcp__ctxpress__manage_context",
               "arguments": json.dumps({"enable": [], "disable": ["tk_001"]})},
              {"type": "function_call_output", "call_id": "m", "output": "recorded"}]
    rw = Rewriter(lambda: build({"class": "DTOC"}))
    body, _ = rw.rewrite_body({"input": inputs[:3]}, "s")
    assert json.loads(body["input"][-1]["output"])["tool_result"] == "file contents"
    body, _ = rw.rewrite_body({"input": inputs}, "s")
    outs = {x["call_id"]: x["output"] for x in body["input"] if x.get("type") == "function_call_output"}
    assert json.loads(outs["a"])["status"] == "hidden"
    assert json.loads(outs["m"])["tool_result"].startswith("Disabled: tk_001")
    assert any(x.get("role") == "developer" and "DTOC" in json.dumps(x) for x in body["input"])


def test_mcp_offers_manage_context(monkeypatch):
    monkeypatch.setenv("CTXPRESS_METHOD_CONFIG", json.dumps({"class": "DTOC"}))
    monkeypatch.setattr(mcp, "_METHOD", [])
    assert "manage_context" in [t["name"] for t in mcp.handle({"id": 1, "method": "tools/list"})["result"]["tools"]]
    reply = mcp.handle({"id": 2, "method": "tools/call", "params": {"name": "manage_context", "arguments": {"enable": []}}})
    assert reply["result"]["isError"] is True


@pytest.mark.skipif(not shutil.which("node") or not os.path.isdir(os.path.join(DATA, "repro", "dtoc")),
                    reason="Node or the authors' DTOC sources not available")
def test_identical_to_the_authors_registry_tool_and_envelope(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "ctxpress", "repro"))
    import random, subprocess
    import dtoc_compare as cmp
    cmp.prepare(str(tmp_path))
    rng = random.Random(3)
    sessions = [cmp.session(rng) for _ in range(15)]
    proc = subprocess.run(["node", "--experimental-transform-types", "--no-warnings", "driver.mjs"], cwd=tmp_path,
                          input=json.dumps(dict(sessions=sessions)), capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr[-2000:]
    pairs = [(o, t) for s, tv in zip(sessions, json.loads(proc.stdout)) for o, t in zip(cmp.ours(s), tv)]
    assert pairs and all(o == t for o, t in pairs)
    assert any('"status":"hidden"' in x for _, t in pairs for _, x in t)
