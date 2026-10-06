"""Requirements must remain intact through ingress compression, deletion and summaries."""
import pytest
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import ACON, ReSum, SlidingWindow, ComplexityTrap, CliffCompaction, Pichay
from ctxpress.methods.wrappers import Composed, PinRequirements


@pytest.mark.parametrize("factory", [
    lambda: PinRequirements(ACON(t_hist=10, t_obs=10)),
    lambda: PinRequirements(SlidingWindow(t=1)),
    lambda: Composed([PinRequirements(ComplexityTrap(n=0)), ReSum(k=1)]),
    lambda: PinRequirements(CliffCompaction(t=10, keep_recent=0)),
    lambda: PinRequirements(Pichay(age=0, min_size=1)),
])
def test_pin_requirements_survives_all_later_operations(factory):
    summary_inputs = []
    def summarize(items):
        summary_inputs.extend(items)
        return "continuation note"
    rewriter = Rewriter(factory, summarizer=summarize)
    spec_call = {"type": "function_call", "call_id": "spec", "name": "exec", "arguments": "cat docs/SPEC.md"}
    spec_out = {"type": "function_call_output", "call_id": "spec", "output": "REQUIREMENTS\n" * 500}
    inputs = [{"role": "user", "content": "task"}, spec_call, spec_out,
              {"type": "function_call", "call_id": "other", "name": "exec", "arguments": "echo done"},
              {"type": "function_call_output", "call_id": "other", "output": "other output\n" * 500},
              {"role": "assistant", "content": "finished"}]
    for _ in range(2):
        body, _ = rewriter.rewrite_body(dict(input=inputs), "s")
        assert spec_call in body["input"] and spec_out in body["input"]
    assert all(item.get("call_id") != "spec" for item in summary_inputs)


def test_protection_survives_common_window_fallback():
    from ctxpress.core.params import DEFAULT
    rewriter = Rewriter(lambda: PinRequirements(ComplexityTrap(n=10)),
                        params=DEFAULT.override(window=100), summarizer=lambda items: "note")
    call = {"type": "function_call", "call_id": "spec", "name": "exec", "arguments": "cat SPEC.md"}
    output = {"type": "function_call_output", "call_id": "spec", "output": "requirements\n" * 500}
    body, _ = rewriter.rewrite_body(dict(input=[{"role": "user", "content": "task"}, call, output,
                                               {"role": "assistant", "content": "analysis"}]), "s")
    assert call in body["input"] and output in body["input"]
