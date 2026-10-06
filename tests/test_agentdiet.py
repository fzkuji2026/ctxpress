"""AgentDiet: window, threshold, prompt, acceptance, whole-step replacement and the authors' re-serialization."""
import glob, json, os, re, sys

import pytest
from ctxpress.live.context import LiveContext
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import AgentDiet, build
from ctxpress.methods.agentdiet import AGENTDIET_SYSTEM, ERASED

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA = os.environ.get("CTXPRESS_DATA", os.path.join(ROOT, "data"))


class Reflector:
    def __init__(self, reply=lambda idx, user: f"short {idx}"):
        self.calls, self.reply = [], reply

    def summarize_prompt(self, system, user, purpose="history", model=None):
        self.calls.append((system, user, model))
        idx = re.search(r"Now, compress the step (-?\d+)\.", user).group(1)
        return f'Sure. Here is the compressed content of step {idx}: <step id="{idx}">{self.reply(idx, user)}</step>'


def run(method, n, size=3000, reflector=None):
    ref = reflector or Reflector()
    ctx = LiveContext(method, summarizer=ref)
    ctx.add_message("user", "THE TASK")
    for i in range(n):
        ctx.add_message("assistant", f"thinking {i}")
        ctx.add_call(f"c{i}", f"cat f{i}.py", name="bash", args=json.dumps({"command": f"cat f{i}.py"}))
        ctx.add_output(f"c{i}", f"{i}" * size)
        ctx.before_request()
    return ctx, ref


def test_window_prompt_and_whole_step_replacement():
    ctx, ref = run(AgentDiet(a=2, b=1, threshold=500), 3)
    system, user, model = ref.calls[0]
    assert system == AGENTDIET_SYSTEM and model is None
    assert user.startswith("THE TASK\n<step id=\"0\">\n<think>thinking 0</think>\n<call tool=\"bash\">")
    assert '<step id="2">' in user and '<step id="3">' not in user
    assert "Now, compress the step 0." in user
    first = ctx.view()[1]
    assert first["seg"] == "summary" and first["role"] == "assistant" and first["text"] == ERASED + "short 0"
    assert [v["text"] for v in ctx.view() if v["seg"] == "out"] == ["1" * 3000, "2" * 3000]


def test_short_steps_and_small_savings_are_left_alone():
    _, ref = run(AgentDiet(threshold=10 ** 6), 5)
    assert not ref.calls
    ctx, ref = run(AgentDiet(), 5, reflector=Reflector(lambda idx, user: "y" * 11000))
    assert len(ref.calls) == 3 and not any(v["seg"] == "summary" for v in ctx.view())


def test_rewritten_step_reappears_double_wrapped_like_the_authors_code():
    _, ref = run(AgentDiet(a=2, b=1), 4)
    later = ref.calls[1][1]                                   # compressing step 1 shows step 0 as context
    assert later.startswith('THE TASK') is False and later.startswith('<step id="0">\n<step id="0">\n<think>thinking 0</think>')


def test_gpt5_reflection_model_uses_the_authors_wording():
    _, ref = run(AgentDiet(reflect_model="gpt-5-mini-2025-08-07"), 3)
    system, user, model = ref.calls[0]
    assert model == "gpt-5-mini-2025-08-07" and "<talk>thinking 0</talk>" in user
    assert "talk in <talk>" in system and "engineer" in system and "think" not in system


def test_host_receives_a_paired_request_with_the_rewrite():
    inputs = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task"}]}]
    for i in range(4):
        inputs += [{"type": "function_call", "call_id": f"c{i}", "name": "exec", "arguments": json.dumps({"cmd": f"cat f{i}"})},
                   {"type": "function_call_output", "call_id": f"c{i}", "output": f"{i}" * 3000}]
    body, _ = Rewriter(lambda: build({"class": "AgentDiet"}), summarizer=Reflector()).rewrite_body({"input": inputs}, "s")
    kinds = [(x.get("type"), x.get("role"), x.get("call_id")) for x in body["input"]]
    # one reflection per request, on step s - a: a history that arrives whole (a resumed session) gets only that
    # step rewritten; earlier steps stay as they are
    assert kinds[3] == ("message", "assistant", None) and ("function_call", None, "c1") not in kinds
    assert [k[2] for k in kinds if k[0] == "function_call_output"] == ["c0", "c2", "c3"]


RAW = glob.glob(os.path.join(DATA, "repro", "ct-traj", "trajectories", "lindenbauer", "main_experiments",
                             "*baseline_raw*", "*", "*.traj"))


@pytest.mark.skipif(not RAW or not os.path.isdir(os.path.join(DATA, "repro", "agentdiet", "artifact")),
                    reason="authors' artifact or trajectories not available")
def test_identical_to_the_authors_reflection_module():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "repro"))
    import agentdiet_compare as cmp
    ta, manager, prompt = cmp.load_authors()
    results = [cmp.check(p, ta, manager, prompt) for p in sorted(RAW)[:8]]
    assert all(r["same"] == r["steps"] for r in results)
    assert sum(r["erased"] for r in results) > 0 and sum(r["analyses"] - r["erased"] for r in results) > 0
