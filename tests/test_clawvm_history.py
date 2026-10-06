"""ClawVM's live demand mapping at frozen and revised history boundaries."""
import copy

import pytest

from ctxpress import ContextManager
from ctxpress.methods import ClawVM


def call(i):
    return dict(type="function_call", name="shell", call_id=f"c{i}", arguments=f"cat a/f{i}.py")


def output(i):
    return dict(type="function_call_output", call_id=f"c{i}", output=f"output {i}\n" + "x" * 6000)


def history():
    items = [dict(type="message", role="user", content="keep this task")]
    for i in range(30):
        items.extend([call(i), output(i)])
    return items


def outputs(items):
    return {s["call_id"]: s["output"] for s in items if s.get("type") == "function_call_output"}


@pytest.mark.parametrize("incremental", [False, True])
def test_latest_output_survives_bootstrap_and_incremental_history(incremental):
    manager = ContextManager(ClawVM())
    original = history()
    if incremental:
        for end in range(3, len(original), 2):
            manager.apply(original[:end])
    result = manager.apply(original)
    assert outputs(result)["c29"] == outputs(original)["c29"]
    assert outputs(result)["c0"] != outputs(original)["c0"]
    assert result[0] == original[0]
    assert [s.get("call_id") for s in result] == [s.get("call_id") for s in original]
    assert manager.apply(original) == result


def test_parallel_outputs_in_latest_turn_survive_first_request():
    original = history() + [call(30), call(31), output(30), output(31)]
    manager = ContextManager(ClawVM())
    result = manager.apply(original)
    for i in (30, 31):
        assert outputs(result)[f"c{i}"] == outputs(original)[f"c{i}"]
    assert outputs(result)["c29"] != outputs(original)["c29"]


def test_rebased_history_uses_latest_turn_and_preserves_archives():
    manager = ContextManager(ClawVM())
    original = history()
    manager.apply(original)
    ctx = next(iter(manager.rewriter.sessions.values())).ctx
    old_id = next(s["id"] for s in ctx.outputs() if s["call_id"] == "c0")
    revised = copy.deepcopy(original)
    revised[2]["output"] = "revised old output\n" + "z" * 6000
    result, info = manager.apply(revised, return_info=True)
    assert info["history_rebased"]
    assert outputs(result)["c29"] == outputs(revised)["c29"]
    assert outputs(result)["c0"] != outputs(revised)["c0"]
    assert manager.retrieve(old_id) == outputs(original)["c0"]
