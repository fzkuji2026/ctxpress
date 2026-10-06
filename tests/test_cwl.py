"""CWL: the delimiter tool (rules, texts, MCP), graduated eviction, dependency order and re-admission."""
import json, os, shutil, sys

import pytest
from ctxpress.live import mcp
from ctxpress.live.context import LiveContext
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import CWL, Composed, ComplexityTrap, build

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA = os.environ.get("CTXPRESS_DATA", os.path.join(ROOT, "data"))


def test_delimiter_rules_and_texts_match_the_tool():
    m = CWL()
    assert m.call_tool("delimiter", {"action": "end"}) == ("No active chunk to end.", True)
    assert m.call_tool("delimiter", {"action": "start", "name": "a", "type": "act", "dependencies": ["x"]}) == \
        ('Chunk dependency "x" was not found in this session.', True)
    assert m.call_tool("delimiter", {"action": "start", "name": "look", "type": "expl"}) == ("start [expl] look", False)
    assert m.call_tool("delimiter", {"action": "start", "name": "b", "type": "expl"})[1]
    assert m.call_tool("delimiter", {"action": "end"}) == \
        ('Exploration chunks must include a non-empty "description" when ended.', True)
    assert m.call_tool("delimiter", {"action": "end", "description": "read the router"}) == \
        ("end [expl] look — read the router", False)
    assert m.call_tool("delimiter", {"action": "start", "name": "fix", "type": "act", "dependencies": ["look", "look"]}) == \
        ("start [act] fix dep=look", False)
    assert m.call_tool("delimiter", {"action": "end", "description": "x"}) == ('Only exploration chunks accept "description" on end.', True)
    assert m.call_tool("delimiter", {"action": "end"}) == ("end [act] fix dep=look", False)
    assert m.call_tool("delimiter", {"action": "start", "name": "look", "type": "expl"}) == \
        ('Chunk "look" already exists in this session.', True)


def test_mcp_serves_the_methods_tool(monkeypatch):
    monkeypatch.setenv("CTXPRESS_METHOD_CONFIG", json.dumps({"class": "Composed", "args": {"methods": [
        {"class": "CWL", "args": {"budget": 1000}}, {"class": "ComplexityTrap", "args": {"n": 10}}]}}))
    monkeypatch.setattr(mcp, "_METHOD", [])
    names = [t["name"] for t in mcp.handle({"id": 1, "method": "tools/list"})["result"]["tools"]]
    assert names == ["ctxpress_retrieve", "ctxpress_status", "delimiter"]
    call = lambda args: mcp.handle({"id": 2, "method": "tools/call", "params": {"name": "delimiter", "arguments": args}})["result"]
    assert call({"action": "start", "name": "e", "type": "expl"}) == {"content": [{"type": "text", "text": "start [expl] e"}], "isError": False}
    assert call({"action": "start", "name": "f", "type": "expl"})["isError"] is True


def session(ctx, tool, script):
    """script: ('d', args) delimiter call; (tool, text) a tool call with its result; ('u', text) a user message."""
    for i, (kind, value) in enumerate(script):
        if kind == "u":
            ctx.add_message("user", value)
            continue
        ctx.add_other("reasoning", 10, text="thinking " * 20)
        args = value if kind == "d" else {"path": "f.py"}
        ctx.add_call(f"c{i}", json.dumps(args), name="delimiter" if kind == "d" else kind, args=json.dumps(args, separators=(",", ":")))
        ctx.add_output(f"c{i}", tool.call_tool("delimiter", value)[0] if kind == "d" else value)
        ctx.before_request()
    return [(v["seg"], v["text"]) for v in ctx.view()]


START = lambda n, t="expl", d=None: ("d", dict({"action": "start", "name": n, "type": t}, **({"dependencies": d} if d else {})))
END = lambda desc=None: ("d", dict({"action": "end"}, **({"description": desc} if desc else {})))


def test_graduated_eviction_oldest_action_first_and_dependency_order():
    big = "x" * 4000
    script = [START("e1"), ("read", big), ("grep", big), END("found it"),
              START("a1", "act", ["e1"]), ("grep", big), ("bash", big), ("read", big), ("edit", big), END(),
              START("e2"), ("read", "small")]
    ctx = LiveContext(CWL(budget=3500))
    view = session(ctx, CWL(), script)
    outs = [t for s, t in view if s == "out"]
    # e1 has a dependent, so a1 goes first: search, bash, read, then the whole episode (its opening call stays, as
    # in pi-cwl, where the range starts at the delimiter's result); that is enough, so e1 is left alone
    assert outs == ["start [expl] e1", big, big, "end [expl] e1 — found it", "start [expl] e2", "small"]
    assert ('call', json.dumps(START("a1", "act", ["e1"])[1])) in view


def test_action_loses_search_then_bash_then_read_before_the_rest():
    big = "y" * 4000
    script = [START("e1"), ("read", "r"), END("ok"), START("a1", "act", ["e1"]), ("grep", big), ("bash", big),
              ("read", big), ("edit", "patched"), END(), START("e2"), ("read", "tail")]
    ctx = LiveContext(CWL(budget=2600))
    outs = [t for s, t in session(ctx, CWL(), script) if s == "out"]
    assert outs.count("y" * 4000) == 1 and "patched" in outs                # grep and bash gone, read and edit kept


def test_evicted_exploration_returns_when_a_new_action_depends_on_it():
    big = "z" * 6000
    script = [START("e1"), ("read", big), END("context"), START("e2"), ("read", "s"), END("more"),
              ("bash", "w" * 3000), START("a1", "act", ["e1"])]
    views = []
    for k in range(len(script)):
        ctx2 = LiveContext(CWL(budget=2500))
        views.append([t for s, t in session(ctx2, CWL(), script[:k + 1]) if s == "out"])
    assert "z" * 6000 not in views[-2] and "z" * 6000 in views[-1]


def test_user_messages_survive_and_the_host_request_stays_paired():
    big = "q" * 5000
    inputs = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task"}]}]
    tool = CWL()
    for i, (kind, value) in enumerate([START("e1"), ("read", big), ("u", "please also check docs"), END("read"),
                                        START("e2"), ("read", "keep")]):
        if kind == "u":
            inputs.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": value}]})
            continue
        name = "mcp__ctxpress__delimiter" if kind == "d" else "read"
        inputs += [{"type": "function_call", "call_id": f"c{i}", "name": name, "arguments": json.dumps(value if kind == "d" else {})},
                   {"type": "function_call_output", "call_id": f"c{i}", "output": tool.call_tool("delimiter", value)[0] if kind == "d" else value}]
    body, _ = Rewriter(lambda: build({"class": "CWL", "args": {"budget": 200}})).rewrite_body({"input": inputs}, "s")
    texts = json.dumps(body["input"])
    assert "please also check docs" in texts and big not in texts and "keep" in texts
    calls = {x["call_id"] for x in body["input"] if x.get("type") == "function_call"}
    outs = {x["call_id"] for x in body["input"] if x.get("type") == "function_call_output"}
    assert calls == outs


@pytest.mark.skipif(not shutil.which("node") or not os.path.isdir(os.path.join(DATA, "repro", "pi-cwl")),
                    reason="Node or the authors' pi-cwl sources not available")
def test_identical_to_the_authors_filter(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "ctxpress", "repro"))
    import subprocess, random
    import cwl_compare as cmp
    cmp.prepare(str(tmp_path))
    rng = random.Random(7)
    sessions = [cmp.session(rng) for _ in range(12)]
    proc = subprocess.run(["node", "--experimental-transform-types", "--no-warnings", "driver.mjs"], cwd=tmp_path,
                          input=json.dumps(dict(sessions=sessions)), capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr[-2000:]
    theirs = json.loads(proc.stdout)
    pairs = [(o, cmp.canon_theirs(t)) for s, tv in zip(sessions, theirs) for o, t in zip(cmp.ours(s), tv)]
    assert pairs and all(o == t for o, t in pairs)
    assert any(len(t) < len(o) + 1 for o, t in pairs)
