"""The pruner keeps original failure behavior and required guidance under composition."""
from pathlib import Path
import os, sys
import pytest

from ctxpress.live.rewrite import Rewriter
from ctxpress.methods.swepruner import SWEPruner, render, INSTRUCTIONS
from ctxpress.methods.wrappers import Composed, PinRequirements, WithMemory, EntryTruncation


def test_pruner_guidance_survives_nested_wrappers():
    method = Composed([WithMemory(PinRequirements(SWEPruner())), SWEPruner()])
    body, _ = Rewriter(lambda: method).rewrite_body({"input": [
        {"type": "message", "role": "user", "content": "task"}]}, "s")
    guidance = [x for x in body["input"] if x.get("role") == "developer"]
    assert len(guidance) == 1 and guidance[0]["content"][0]["text"] == INSTRUCTIONS


def test_no_question_and_empty_output_are_unchanged():
    def must_not_call(*args):
        raise AssertionError("no model call expected")
    assert render("source", None, must_not_call) == ("source", None)
    assert render("", "question", must_not_call) == ("", None)


def test_live_proxy_rejects_replay_fallback_even_inside_wrappers():
    from ctxpress.live.proxy import serve
    method = Composed([WithMemory(PinRequirements(SWEPruner()))])
    with pytest.raises(ValueError, match="pruning service"):
        serve(lambda: method, 0, "http://unused")
    method = WithMemory(SWEPruner(pruner=lambda *args: None))
    server, _ = serve(lambda: method, 0, "http://unused", host="127.0.0.1")
    server.server_close()


def test_pruning_overhead_uses_service_usage_and_skips_short_outputs():
    from ctxpress.live.context import LiveContext
    method = SWEPruner(pruner=lambda *args: dict(pruned_code="kept", origin_token_cnt=100,
                                                left_token_cnt=10, model_input_token_cnt=123))
    context = LiveContext(method)
    context.add_call("short", "cat a.py # context_focus_question: where is x?")
    context.add_output("short", "short output")
    assert method.read == 0
    context.add_call("long", "cat b.py # context_focus_question: where is y?")
    context.add_output("long", "line\n" * 300)
    assert method.read == 123
    assert method.overhead(context, 0) == 123 * context.P.get("small_model_price")
    assert method.read == 0


def test_entry_truncation_feeds_its_kept_text_to_pruner():
    from ctxpress.live.context import LiveContext
    from ctxpress.methods import NoCompaction
    original = "line of source\n" * 300
    baseline = LiveContext(EntryTruncation(NoCompaction(), budget=200))
    baseline.add_call("a", "cat a.py")
    baseline.add_output("a", original)
    expected = baseline.outputs()[0]["kept_text"]
    calls = []
    def prune(query, code, *args):
        calls.append(code)
        return dict(pruned_code="kept", origin_token_cnt=100, left_token_cnt=10, model_input_token_cnt=123)
    context = LiveContext(EntryTruncation(SWEPruner(pruner=prune, min_chars=0), budget=200))
    context.add_call("a", "cat a.py # context_focus_question: focus")
    context.add_output("a", original)
    assert calls == [expected] and context.retrieve(context.outputs()[0]["id"]) == original


def test_live_pruner_usage_is_counted_once_and_distinct_from_main_api_usage():
    method = SWEPruner(pruner=lambda *args: dict(pruned_code="kept", origin_token_cnt=100,
                                                left_token_cnt=10, model_input_token_cnt=123, token_scores=[0.1] * 1000))
    rw = Rewriter(lambda: method)
    inputs = [{"role": "user", "content": "task"},
              {"type": "function_call", "call_id": "a", "name": "exec", "arguments": "cat a.py # context_focus_question: focus"},
              {"type": "function_call_output", "call_id": "a", "output": "line\n" * 300}]
    _, first = rw.rewrite_body(dict(input=inputs), "s")
    _, second = rw.rewrite_body(dict(input=inputs), "s")
    assert first["pruner_calls"] == 1 and first["pruner_input_tokens"] == 123
    assert first["method_overhead_estimate"] == 123 * rw.sessions["s"].ctx.P.get("small_model_price")
    assert second["pruner_calls"] == second["pruner_input_tokens"] == second["method_overhead_estimate"] == 0
    assert method.read == 0 and "token_scores" not in method.stats[0]


def test_author_output_handling_in_live_requests():
    root = Path(__file__).resolve().parents[1]
    data = Path(os.environ.get('CTXPRESS_DATA', root.parent / 'data'))
    original = data / 'repro/swe-pruner'
    if not original.exists():
        pytest.skip("author's source not available")
    sys.path.insert(0, str(root / "repro"))
    from swe_pruner_compare import author_functions, cases, compare_case
    apply, prune, _ = author_functions(original)
    results = [compare_case(c, apply, prune) for c in cases()]
    assert all(r["same"] and r["calls_same"] for r in results)
