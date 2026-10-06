"""Anthropic Messages (Claude Code) as a host: flatten, rewrite with any method, assemble a valid body back."""
import copy, json

import pytest
from ctxpress.live import anthropic
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import build

TOOLS = [{"name": "Bash", "input_schema": {"type": "object"}}]


def conversation(n, thinking=True, size=3000, cache_last=True):
    msgs = [{"role": "user", "content": [{"type": "text", "text": "<system-reminder>project rules</system-reminder>"},
                                         {"type": "text", "text": "Fix the failing test"}]}]
    for i in range(n):
        content = []
        if thinking:
            content.append({"type": "thinking", "thinking": f"plan {i}", "signature": f"sig{i}"})
        content += [{"type": "text", "text": f"step {i}"}, {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": f"cat f{i}"}}]
        msgs.append({"role": "assistant", "content": content})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": f"OUT{i} " + "x" * size}]})
    if cache_last:
        msgs[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
    return {"model": "claude-x", "system": [{"type": "text", "text": "You are Claude Code."}], "tools": TOOLS,
            "max_tokens": 1000, "messages": msgs, "metadata": {"user_id": "user_1_session_abc"}}


def valid(body):
    """The API's structural rules for a tool loop."""
    msgs = body["messages"]
    assert msgs and msgs[0]["role"] == "user"
    for a, b in zip(msgs, msgs[1:]):
        assert a["role"] != b["role"]
    for k, m in enumerate(msgs):
        assert m["content"], m
        uses = [b["id"] for b in m["content"] if b.get("type") == "tool_use"] if m["role"] == "assistant" else []
        if uses:
            nxt = msgs[k + 1]["content"]
            results = [b["tool_use_id"] for b in nxt if b.get("type") == "tool_result"]
            assert set(uses) <= set(results)
            assert [b.get("type") for b in nxt[:len(results)]] == ["tool_result"] * len(results)
        if m["role"] == "user":
            for b in m["content"]:
                if b.get("type") == "tool_result":
                    prev = msgs[k - 1]["content"]
                    assert any(x.get("type") == "tool_use" and x["id"] == b["tool_use_id"] for x in prev)
    return True


def rewrite(method, body, rw=None):
    rw = rw or Rewriter(lambda: build(method))
    return anthropic.rewrite_messages(rw, copy.deepcopy(body)), rw


def test_no_compaction_round_trips_exactly():
    body = conversation(4)
    (out, info), _ = rewrite({"class": "NoCompaction"}, body)
    assert out == body and info["request"] == 1


def test_masking_rewrites_old_results_in_place():
    body = conversation(6)
    (out, _), _ = rewrite({"class": "ComplexityTrap", "args": {"n": 2}}, body)
    results = [b for m in out["messages"] for b in m["content"] if b.get("type") == "tool_result"]
    assert [r["content"].startswith("Old environment output") for r in results] == [True] * 4 + [False] * 2   # keeps the newest N = 2
    assert results[-1]["cache_control"] == {"type": "ephemeral"} and valid(out)


def test_moving_cache_breakpoints_is_not_a_history_revision():
    rw = Rewriter(lambda: build({"class": "ComplexityTrap", "args": {"n": 2}}))
    first = conversation(3)
    anthropic.rewrite_messages(rw, copy.deepcopy(first))
    second = conversation(4)                                    # breakpoint moved to the new last block
    _, info = anthropic.rewrite_messages(rw, second)
    assert info["history_rebased"] is False and info["request"] == 2


def test_deletions_keep_a_valid_tool_loop_and_the_last_thinking():
    body = conversation(8, size=6000)
    (out, _), _ = rewrite({"class": "SlidingWindow", "args": {"t": 9000}}, body)
    assert valid(out) and len(out["messages"]) < len(body["messages"])
    last = out["messages"][-2]
    assert last["role"] == "assistant" and last["content"][0] == {"type": "thinking", "thinking": "plan 7", "signature": "sig7"}


def test_a_method_removing_thinking_cannot_remove_the_last_turns():
    body = conversation(3)
    flat, refs = anthropic.flatten(body)
    out = [x for x in flat if x["type"] != "reasoning"]          # e.g. CWL's thinking level
    assembled = anthropic.unflatten(body, refs, flat, out)
    assert assembled["messages"][-2]["content"][0]["type"] == "thinking"
    assert all(b["type"] != "thinking" for m in assembled["messages"][:-2] for b in m["content"])
    assert valid(assembled)


def test_method_instructions_go_to_system_and_summaries_become_text():
    (out, _), _ = rewrite({"class": "DTOC"}, conversation(2))
    assert out["system"][0]["text"] == "You are Claude Code." and "manage_context" in out["system"][-1]["text"]
    results = [b for m in out["messages"] for b in m["content"] if b.get("type") == "tool_result"]
    assert json.loads(results[0]["content"])["tool_key"] == "tk_001"


def test_side_requests_pass_and_sub_agents_are_separate_conversations():
    rw = Rewriter(lambda: build({"class": "ComplexityTrap", "args": {"n": 1}}))
    side = dict(conversation(3), tools=[])
    assert anthropic.rewrite_messages(rw, copy.deepcopy(side)) == (side, None)
    main, sub = conversation(3), conversation(3)
    sub["messages"][0]["content"][1]["text"] = "Sub-agent: search the code"
    assert anthropic.session_key(main) != anthropic.session_key(sub)
    moved = conversation(4)
    assert anthropic.session_key(main) == anthropic.session_key(moved)


def test_usage_from_a_stream():
    events = [{"type": "message_start", "message": {"model": "claude-x", "usage": {"input_tokens": 10,
               "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50, "output_tokens": 1}}},
              {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
              {"type": "message_delta", "usage": {"output_tokens": 42}}]
    text = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    assert anthropic.find_usage(text) == dict(input_tokens=960, cached_tokens=900, cache_write_tokens=50, output_tokens=42,
                                              reasoning_tokens=None)
    assert anthropic.find_model(text) == "claude-x"


def test_images_survive_and_media_tool_results_keep_their_images():
    body = conversation(3)
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    body["messages"][0]["content"].append(img)
    body["messages"][2]["content"][0]["content"] = [{"type": "text", "text": "screenshot " + "y" * 3000}, img]
    (out, _), _ = rewrite({"class": "ComplexityTrap", "args": {"n": 1}}, body)
    assert img in out["messages"][0]["content"]
    first = out["messages"][2]["content"][0]["content"]
    assert isinstance(first, list) and img in first and first[0]["text"].startswith("Old environment output") and valid(out)


def test_a_next_prompt_suggestion_fork_does_not_reset_the_method():
    """Claude Code asks for a suggestion with the whole history plus one instruction, then drops it: the next real
    turn continues the method's state instead of starting a new epoch."""
    rw = Rewriter(lambda: build({"class": "DTOC"}))
    turn = conversation(2)
    anthropic.rewrite_messages(rw, copy.deepcopy(turn))
    fork = copy.deepcopy(turn)
    fork["messages"].append({"role": "assistant", "content": [{"type": "text", "text": "done"}]})
    fork["messages"].append({"role": "user", "content": [{"type": "text", "text": "Suggest the user's next prompt."}]})
    anthropic.rewrite_messages(rw, fork)
    nxt = copy.deepcopy(turn)
    nxt["messages"].append({"role": "assistant", "content": [{"type": "text", "text": "done"}]})
    nxt["messages"].append({"role": "user", "content": [{"type": "text", "text": "real next message"}]})
    out, info = anthropic.rewrite_messages(rw, nxt)
    assert info["history_rebased"] is False and info["forks_undone"] == 1 and info["history_epoch"] == 0
    keys = [json.loads(b["content"])["tool_key"] for m in out["messages"] for b in m["content"] if b.get("type") == "tool_result"]
    assert keys == ["tk_001", "tk_002"]


def test_a_real_revision_still_starts_a_new_epoch():
    rw = Rewriter(lambda: build({"class": "NoCompaction"}))
    anthropic.rewrite_messages(rw, conversation(3))
    anthropic.rewrite_messages(rw, conversation(4))
    edited = conversation(4)
    edited["messages"][2]["content"][0]["content"] = "edited earlier output"
    _, info = anthropic.rewrite_messages(rw, edited)
    assert info["history_rebased"] is True and info["forks_undone"] == 0
