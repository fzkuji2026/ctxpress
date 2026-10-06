"""The Complexity Trap's LLM-Summary and hybrid: trigger, prompt, checkpoint order, masking, host messages."""
import glob, json, os, sys

import pytest
from ctxpress.live.context import LiveContext
from ctxpress.live.rewrite import Rewriter
from ctxpress.live.summarize import ResponsesSummarizer, DATA_BOUNDARY
from ctxpress.methods import ComplexityTrapSummary, ComplexityTrapHybrid, build
from ctxpress.methods.summaries import CT_SUMMARY_SYSTEM, CT_SUMMARY_ASK

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA = os.environ.get("CTXPRESS_DATA", os.path.join(ROOT, "data"))


class Service:
    def __init__(self):
        self.prompts = []

    def summarize_prompt(self, system, user, purpose="history"):
        self.prompts.append((system, user))
        return f"S{len(self.prompts)}"


def turn(ctx, i, out=None):
    ctx.add_message("assistant", f"thought {i}")
    ctx.add_call(f"c{i}", f"cat f{i}.py")
    ctx.add_output(f"c{i}", out or f"line\n" * (i + 1) + f"OUT{i}")


def visible(ctx):
    return [(v["seg"], v["role"], v["text"]) for v in ctx.view() if v["seg"] in ("summary", "out")]


def test_checkpoint_after_n_plus_m_turns_with_original_prompt():
    svc = Service()
    ctx = LiveContext(ComplexityTrapSummary(n=3, m=2), summarizer=svc)
    ctx.add_message("system", "sys", fixed=True)
    ctx.add_message("user", "THE TASK")
    for i in range(4):
        turn(ctx, i)
        ctx.before_request()
    assert not svc.prompts
    turn(ctx, 4)
    ctx.before_request()
    system, user = svc.prompts[0]
    assert system == CT_SUMMARY_SYSTEM
    assert user.startswith("<PROBLEM_STATEMENT>\nTHE TASK\n</PROBLEM_STATEMENT>\n\n<TURN-0>\nASSISTANT: thought 0\nACTION: cat f0.py\nTOOL: ")
    assert "<TURN-2>" in user and "<TURN-3>" not in user and user.endswith(CT_SUMMARY_ASK)
    shown = visible(ctx)
    assert shown[0] == ("summary", "assistant", "Checkpoint for the last 3 turns:\nS1")
    assert [t for _, _, t in shown[1:]] == ["line\n" * 4 + "OUT3", "line\n" * 5 + "OUT4"]


def test_static_checkpoint_shows_newest_then_all_like_the_authors_code():
    svc = Service()
    ctx = LiveContext(ComplexityTrapSummary(n=2, m=1), summarizer=svc)
    ctx.add_message("user", "task")
    texts = []
    for i in range(7):
        turn(ctx, i)
        ctx.before_request()
        texts.append([t for s, _, t in visible(ctx) if s == "summary"])
    # turns 0-1 summarized at the 3rd request, 2-3 at the 5th, 4-5 at the 7th
    assert texts[2] == ["Checkpoint for the last 2 turns:\nS1"]
    assert texts[4] == ["Checkpoint for the last 2 turns:\nS2"]                      # writing request: newest only
    assert texts[5] == ["Checkpoint for the last 2 turns:\nS1", "Checkpoint for the last 2 turns:\nS2"]
    assert svc.prompts[1][1].startswith("<PREVIOUS_CHECKPOINT>\nCheckpoint for the last 2 turns:\nS1\n</PREVIOUS_CHECKPOINT>\n")


def test_hybrid_summarizes_unmasked_text_and_keeps_masking_wording():
    svc = Service()
    ctx = LiveContext(ComplexityTrapHybrid(n=3, m=2, w=1), summarizer=svc)
    ctx.add_message("user", "task")
    for i in range(5):
        turn(ctx, i, out=f"ORIGINAL-{i}\nsecond line")
        ctx.before_request()
    user = svc.prompts[0][1]
    assert all(f"ORIGINAL-{i}" in user for i in range(3)) and "Old environment output" not in user
    outs = [t for s, _, t in visible(ctx) if s == "out"]
    assert outs == ["Old environment output: (2 lines omitted)", "ORIGINAL-4\nsecond line"]


def test_without_service_history_stays():
    ctx = LiveContext(ComplexityTrapSummary(n=1, m=0))
    ctx.add_message("user", "task")
    turn(ctx, 0)
    ctx.before_request()
    assert not any(s == "summary" for s, _, _ in visible(ctx)) and ctx.M["summary_skipped"] == 1


def test_host_receives_checkpoint_as_assistant_output_text():
    inputs = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task"}]}]
    for i in range(3):
        inputs += [{"type": "function_call", "call_id": f"c{i}", "name": "exec", "arguments": json.dumps({"cmd": f"cat f{i}"})},
                   {"type": "function_call_output", "call_id": f"c{i}", "output": f"out {i}"}]
    body, _ = Rewriter(lambda: build({"class": "ComplexityTrapSummary", "args": {"n": 2, "m": 1}}),
                       summarizer=Service()).rewrite_body({"input": inputs}, "s")
    msgs = [x for x in body["input"] if x.get("type") == "message" and x.get("role") == "assistant"]
    assert msgs == [{"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "Checkpoint for the last 2 turns:\nS1"}]}]
    assert [x["call_id"] for x in body["input"] if x.get("type") == "function_call_output"] == ["c2"]


def test_service_sends_a_methods_prompt_verbatim():
    sent = {}

    class Conn:
        def request(self, method, path, body, headers):
            sent.update(json.loads(body))

        def getresponse(self):
            done = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}
            return type("R", (), dict(status=200, read=lambda self: json.dumps(done).encode()))()

        def close(self):
            pass

    svc = ResponsesSummarizer(Conn, "/v1/responses", {}, {"model": "m", "input": []})
    assert svc.summarize_prompt("SYSTEM TEXT", "USER TEXT") == "ok"
    assert sent["instructions"] == "SYSTEM TEXT" and DATA_BOUNDARY not in sent["instructions"]
    assert sent["input"][0]["content"][0]["text"] == "USER TEXT" and sent["model"] == "m"


RAW = glob.glob(os.path.join(DATA, "repro", "ct-traj", "trajectories", "lindenbauer", "main_experiments",
                             "*baseline_raw*", "*", "*.traj"))


@pytest.mark.skipif(not RAW or not os.path.isdir(os.path.join(DATA, "repro", "the-complexity-trap")),
                    reason="authors' code or released trajectories not available")
@pytest.mark.parametrize("mode", ["summary", "hybrid"])
def test_identical_to_the_authors_history_processors(mode):
    sys.path.insert(0, os.path.join(ROOT, "ctxpress", "repro"))
    import complexity_trap_summary_compare as cmp
    hp, prompt = cmp.load_authors()
    system, procs = cmp.config(mode)
    long = sorted(RAW, key=os.path.getsize)[-12:]                 # the longest runs reach several checkpoints
    results = [cmp.check(p, mode, hp, prompt, system, procs) for p in long]
    assert all(r["same"] == r["steps"] for r in results)
    assert sum(r["summaries"] > 1 for r in results) >= 2
