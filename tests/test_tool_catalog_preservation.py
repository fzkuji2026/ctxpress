"""Compression must retain Codex's executable additional_tools input catalog."""
import copy

import pytest

from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import ComplexityTrapHybrid, ComplexityTrapSummary


class SummaryService:
    def summarize_prompt(self, system, user, purpose="history"):
        return "Continue the unfinished task using the available workspace tools."


@pytest.mark.parametrize("method,turns", [(ComplexityTrapSummary, 31), (ComplexityTrapHybrid, 53)])
@pytest.mark.parametrize("catalog_after_first_turn", [False, True])
def test_catalog_survives_real_default_summary_threshold_and_next_request(method, turns, catalog_after_first_turn):
    catalog = {
        "type": "additional_tools", "role": "developer",
        "tools": [{"type": "namespace", "name": "functions", "tools": [
            {"type": "custom", "name": "exec", "description": "Execute workspace code",
             "format": {"type": "text"}},
        ]}],
    }
    inputs = [{"type": "message", "role": "user", "content": "Complete the task"}]
    if not catalog_after_first_turn:
        inputs.insert(0, copy.deepcopy(catalog))
    for index in range(turns):
        inputs.extend([
            {"type": "custom_tool_call", "call_id": f"c{index}", "name": "exec", "input": "text(1)"},
            {"type": "custom_tool_call_output", "call_id": f"c{index}", "output": "1"},
        ])
        if index == 0 and catalog_after_first_turn:
            inputs.append(copy.deepcopy(catalog))
    original = {"model": "test-model", "input": inputs, "tools": [], "parallel_tool_calls": False}
    saved = copy.deepcopy(original)
    rewriter = Rewriter(method, summarizer=SummaryService())
    rewritten, info = rewriter.rewrite_body(original, "session")
    assert info["operations"]["segment_summary"] == 1
    assert [i for i in rewritten["input"] if i.get("type") == "additional_tools"] == [catalog]
    assert any(i.get("type") == "custom_tool_call" for i in rewritten["input"])
    assert any(i.get("type") == "message" and "Checkpoint" in str(i.get("content"))
               for i in rewritten["input"])
    assert {k: v for k, v in rewritten.items() if k != "input"} == {
        k: v for k, v in original.items() if k != "input"
    }
    assert original == saved

    # Codex sends its full original history again, plus the new continuation.
    continued = copy.deepcopy(original)
    continued["input"].extend([
        {"type": "custom_tool_call", "call_id": "next", "name": "exec", "input": "text(2)"},
        {"type": "custom_tool_call_output", "call_id": "next", "output": "2"},
    ])
    rewritten, _ = rewriter.rewrite_body(continued, "session")
    assert [i for i in rewritten["input"] if i.get("type") == "additional_tools"] == [catalog]
    assert any(i.get("call_id") == "next" for i in rewritten["input"])
