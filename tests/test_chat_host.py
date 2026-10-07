"""Chat Completions as a host: flatten, rewrite with any method, assemble a valid body back, log usage via the proxy."""
import copy, http.client, json, threading
from http.server import BaseHTTPRequestHandler

from ctxpress.live import chat
from ctxpress.live.proxy import ThreadingHTTPServer, make_handler
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import build

TOOLS = [{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}]


def conversation(n, size=3000, reasoning=True):
    msgs = [{"role": "system", "content": "You are a research agent."},
            {"role": "user", "content": "Find the answer."}]
    for i in range(n):
        assistant = {"role": "assistant", "content": f"step {i}",
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "search", "arguments": json.dumps({"query": f"q{i}"})}}]}
        if reasoning:
            assistant["reasoning_content"] = f"plan {i}"
        msgs.append(assistant)
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"RESULT{i} " + "x" * size})
    return {"model": "acm-9b", "messages": msgs, "tools": TOOLS, "max_tokens": 1000}


def valid(body):
    """The API's structural rules for a tool loop: each call answered right after its assistant message."""
    msgs = body["messages"]
    for k, m in enumerate(msgs):
        if m["role"] == "assistant":
            assert m.get("content") is not None or m.get("tool_calls"), m
            ids = [c["id"] for c in m.get("tool_calls") or []]
            following = [x["tool_call_id"] for x in msgs[k + 1:k + 1 + len(ids)] if x["role"] == "tool"]
            assert following == ids, (ids, following)
        if m["role"] == "tool":
            prev = next(x for x in reversed(msgs[:k]) if x["role"] != "tool")
            assert m["tool_call_id"] in [c["id"] for c in prev.get("tool_calls") or []]
    return True


def rewrite(method, body, rw=None):
    rw = rw or Rewriter(lambda: build(method))
    return chat.rewrite_chat(rw, copy.deepcopy(body)), rw


def test_no_compaction_round_trips_exactly():
    body = conversation(4)
    (out, info), _ = rewrite({"class": "NoCompaction"}, body)
    assert out == body and info["request"] == 1
    assert json.dumps(out) == json.dumps(body)                      # key order too: the serialized prompt is unchanged


def test_assistant_without_text_and_parallel_calls_round_trip():
    body = conversation(1)
    body["messages"].append({"role": "assistant", "content": None, "tool_calls": [
        {"id": "p1", "type": "function", "function": {"name": "search", "arguments": "{}"}},
        {"id": "p2", "type": "function", "function": {"name": "search", "arguments": "{}"}}]})
    body["messages"] += [{"role": "tool", "tool_call_id": "p1", "content": "a"},
                         {"role": "tool", "tool_call_id": "p2", "content": "b"}]
    (out, _), _ = rewrite({"class": "NoCompaction"}, body)
    assert out == body and valid(out)


def test_masking_rewrites_old_results_in_place():
    body = conversation(6)
    (out, _), _ = rewrite({"class": "ComplexityTrap", "args": {"n": 2}}, body)
    results = [m["content"] for m in out["messages"] if m["role"] == "tool"]
    assert [r.startswith("Old environment output") for r in results] == [True] * 4 + [False] * 2
    assert out["messages"][:2] == body["messages"][:2] and valid(out)


def test_deletions_keep_a_valid_tool_loop():
    body = conversation(8, size=6000)
    (out, _), _ = rewrite({"class": "SlidingWindow", "args": {"t": 9000}}, body)
    assert valid(out) and len(out["messages"]) < len(body["messages"])
    assert out["messages"][0] == body["messages"][0] and out["messages"][-1] == body["messages"][-1]


def test_method_instructions_go_to_the_first_system_message():
    (out, _), _ = rewrite({"class": "DTOC"}, conversation(2))
    system = out["messages"][0]
    assert system["role"] == "system" and system["content"].startswith("You are a research agent.")
    assert "manage_context" in system["content"]
    assert sum(m["role"] == "system" for m in out["messages"]) == 1
    tool = next(m for m in out["messages"] if m["role"] == "tool")
    assert json.loads(tool["content"])["tool_key"] == "tk_001" and valid(out)


def test_summary_waits_until_the_open_tool_block_is_answered():
    body = conversation(2)
    flat, refs = chat.flatten(body)
    call = next(n for n, x in enumerate(flat) if x["type"] == "function_call")
    note = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "summary"}]}
    out = flat[:call + 1] + [note] + flat[call + 1:]             # a summary placed between a call and its result
    assembled = chat.unflatten(body, refs, flat, out)
    roles = [m["role"] for m in assembled["messages"]]
    assert roles[:6] == ["system", "user", "assistant", "tool", "user", "assistant"] and valid(assembled)


def test_side_requests_pass_and_conversations_are_separate():
    rw = Rewriter(lambda: build({"class": "ComplexityTrap", "args": {"n": 1}}))
    side = dict(conversation(3), tools=[])
    assert chat.rewrite_chat(rw, copy.deepcopy(side)) == (side, None)
    main, other = conversation(3), conversation(3)
    other["messages"][1]["content"] = "Another question"
    assert chat.session_key(main) != chat.session_key(other)
    assert chat.session_key(main) == chat.session_key(conversation(5))


def test_reasoning_and_images_survive():
    body = conversation(3)
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    body["messages"][1]["content"] = [{"type": "text", "text": "Find the answer."}, image]
    body["messages"][3]["content"] = [{"type": "text", "text": "screenshot " + "y" * 3000}, image]
    (out, _), _ = rewrite({"class": "ComplexityTrap", "args": {"n": 1}}, body)
    assert out["messages"][1] == body["messages"][1]
    first = out["messages"][3]["content"]
    assert isinstance(first, list) and image in first and first[0]["text"].startswith("Old environment output")
    assert [m.get("reasoning_content") for m in out["messages"] if m["role"] == "assistant"] == ["plan 0", "plan 1", "plan 2"]


def test_usage_from_json_and_stream():
    body = {"model": "acm-9b", "choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 900}, "completion_tokens_details": {"reasoning_tokens": 5}}}
    expected = dict(input_tokens=1000, cached_tokens=900, cache_write_tokens=None, output_tokens=20, reasoning_tokens=5)
    assert chat.find_usage(json.dumps(body)) == expected and chat.find_model(json.dumps(body)) == "acm-9b"
    chunks = [{"model": "acm-9b", "choices": [{"delta": {"content": "hi"}}], "usage": None},
              {"model": "acm-9b", "choices": [], "usage": body["usage"]}]
    stream = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    assert chat.find_usage(stream) == expected and chat.find_model(stream) == "acm-9b"


class Upstream(BaseHTTPRequestHandler):
    """An OpenAI-compatible chat server that records what it received."""
    received = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Upstream.received.append((self.path, body))
        reply = json.dumps({"id": "x", "object": "chat.completion", "model": body["model"],
                            "choices": [{"index": 0, "finish_reason": "stop",
                                         "message": {"role": "assistant", "content": "<think>t</think>A short summary."}}],
                            "usage": {"prompt_tokens": 50, "completion_tokens": 7,
                                      "prompt_tokens_details": {"cached_tokens": 10}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


def serving(server):
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return worker


def post(port, path, body):
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    client.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json",
                                                                 "Authorization": "Bearer local"})
    response = client.getresponse()
    data = response.read()
    client.close()
    return response.status, json.loads(data)


def test_proxy_rewrites_chat_turns_and_logs_usage_and_side_calls(tmp_path):
    Upstream.received = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    up = serving(upstream)
    log = tmp_path / "requests.jsonl"
    rw = Rewriter(lambda: build({"class": "ComplexityTrap", "args": {"n": 1}}))
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(rw, f"http://127.0.0.1:{upstream.server_port}", None, str(log)))
    worker = serving(proxy)
    try:
        status, reply = post(proxy.server_port, "/v1/chat/completions", conversation(3))
        assert status == 200 and reply["choices"][0]["message"]["content"].endswith("A short summary.")
        side = {"model": "summarizer", "messages": [{"role": "user", "content": "Summarize this."}]}
        assert post(proxy.server_port, "/v1/chat/completions", side)[0] == 200
    finally:
        for server, thread in ((proxy, worker), (upstream, up)):
            server.shutdown(); server.server_close(); thread.join(timeout=2)
    path, sent = Upstream.received[0]
    assert path == "/v1/chat/completions"
    assert [m["content"].startswith("Old environment output") for m in sent["messages"] if m["role"] == "tool"] == [True, True, False]
    assert Upstream.received[1][1] == side                                  # side calls are forwarded unchanged
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    turn, passthrough = rows
    assert turn["dialect"] == "chat" and turn["status"] == 200 and turn["response_model"] == "acm-9b"
    assert turn["usage"] == dict(input_tokens=50, cached_tokens=10, cache_write_tokens=None, output_tokens=7,
                                 reasoning_tokens=None)
    assert turn["operations"] and turn["host_contract"]["valid"]
    assert passthrough["type"] == "passthrough" and passthrough["dialect"] == "chat" and passthrough["model"] == "summarizer"
    assert passthrough["usage"]["input_tokens"] == 50


def test_summaries_use_the_same_upstream_and_strip_thinking(tmp_path):
    Upstream.received = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    up = serving(upstream)
    rows = []
    try:
        def connect():
            return http.client.HTTPConnection("127.0.0.1", upstream.server_port, timeout=10)
        summarizer = chat.ChatSummarizer(connect, "/v1/chat/completions", {"Authorization": "Bearer local"},
                                         conversation(1), rows.append)
        text = summarizer.summarize_prompt("Summarize.", "history", purpose="history")
    finally:
        upstream.shutdown(); upstream.server_close(); up.join(timeout=2)
    assert text == "A short summary."
    assert Upstream.received[0][1]["model"] == "acm-9b" and Upstream.received[0][1]["messages"][0]["role"] == "system"
    assert rows[0]["type"] == "summary" and rows[0]["completed"] and rows[0]["usage"]["output_tokens"] == 7
