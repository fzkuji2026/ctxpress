"""infinite-context cost mode in the host: the deadline summary, requirements residency, the hold, recall reads."""
import collections, copy, json
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import InfiniteContext
from ctxpress.methods.infinite_v5 import Usage, _spec_paths
from test_infinite_v5 import requests


def counters(factory):
    rw = Rewriter(factory)
    for inp in requests():
        rw.rewrite_body(dict(model='gpt-5', instructions='sys', input=copy.deepcopy(inp), tools=[], stream=True), 's')
    return next(iter(rw.sessions.values())).ctx.M


def test_above_t_our_summary_runs_not_the_host():
    M = counters(lambda: InfiniteContext(version=5, t=500, choose="cost"))
    assert M["v5_choose_deadline"] > 0


def test_at_hard_host_leaves_it_to_codex():
    M = counters(lambda: InfiniteContext(version=5, t=500, choose="cost", at_hard="host"))
    assert M["v5_choose_deadline"] == 0 and M["v5_choose_host"] > 0


def test_cost_defaults_bound_specs_and_keep_user_messages():
    s = InfiniteContext(version=5, t=1000, choose="cost").spec
    assert s.protect_user and s.spec_keep_frac == 0.1 and s.guard_active == 15 and s.cmd_max_chars == 300
    assert s.at_hard == "summary" and s.summary_min_frac == 0.15


def test_a_requirements_read_inside_a_mixed_command_is_a_spec_read():
    assert _spec_paths('sed -n 1,200p srs/M002/SRS.md && sed -n 1,80p src/main.rs') == {'srs/M002/SRS.md'}
    assert _spec_paths('cat TASK_QUEUE.md') == {'TASK_QUEUE.md'}
    assert _spec_paths('rg -n spec src/') == set()
    assert _spec_paths('cat sklearn/inspection/_spec.py') == set()


class FakeSim:
    def __init__(self, ctx):
        self.ctx, self.M = ctx, collections.Counter()

    def seg_size(self, x):
        return len(x.get("text") or "") // 4

    def request_chars(self):
        return sum(len(x.get("text") or "") for x in self.ctx)


def test_window_hold_releases_a_superseded_read():
    m = InfiniteContext(version=5, t=100000, choose="cost")
    old = dict(id=1, seg="out", text="a" * 4000, call_text="cat f.py")
    new = dict(id=2, seg="out", text="b" * 4000, call_text="cat f.py")
    for x in (old, new):
        m._use.add(x)
    held = m._holding(FakeSim([old, new]))
    assert 2 in held and 1 not in held


def test_reuse_probability_does_not_move_the_resolution_horizon():
    u = Usage()
    x = dict(id=1, seg="out", text="x")
    u.add(x)
    short, long = u.p_reuse(x, 3), u.p_reuse(x, 60)
    assert u.horizon == 20 and short < u.p_reuse(x, 20) < long


def test_archive_read_finds_a_sibling_launch_and_search_is_newest_first(tmp_path):
    from ctxpress.methods.infinite_archive import read, search
    session = "a" * 32
    first, later = tmp_path / ("1" * 32), tmp_path / ("2" * 32)
    (first / session).mkdir(parents=True)
    (later / session).mkdir(parents=True)
    (first / session / "5.txt").write_text("needle five", encoding="utf-8")
    (later / session / "9.txt").write_text("needle nine", encoding="utf-8")
    assert read(str(later), 5, session=session)["text"] == "needle five"
    assert [m["id"] for m in search(str(later), "needle", session=session)["matches"]] == [9, 5]


def test_archive_reads_may_run_inside_a_code_mode_cell():
    from ctxpress.live.method_tools import MethodTools
    body = dict(input=[dict(type='custom_tool_call', name='exec', call_id='a', input='JS'),
        dict(type='custom_tool_call_output', call_id='a', output='',
             internal_chat_message_metadata_passthrough=dict(tool_calls_complete=True,
                 executed_tool_calls=[dict(name='mcp__ctxpress__infinite_context_read')]))])
    MethodTools(InfiniteContext(version=5, t=1000, choose="cost")).adapt(body)


def test_cache_keeps_the_first_request_as_a_point_after_many_requests():
    from ctxpress.core.cache_model import Cache
    c = Cache(fixed=0)
    head = [("i", 0)]
    for n in range(1, 100):
        c.record(head + [("i", k) for k in range(1, n + 1)], [1000] * (n + 1))
    assert c.point(head + [("i", 1), ("s", 0)], [1000, 1000, 50]) == 2000   # a summary right after the first request


class Recorder:
    """A summary service whose only use here is recording the writer's usage rows."""
    def __init__(self):
        self.rows = []

    def record(self, row):
        self.rows.append(row)

    def summarize(self, rendered, purpose=None, budget=None):
        raise AssertionError("the host's summary service must not write when DeepSeek is the writer")


def deepseek_run(monkeypatch, answer):
    import ctxpress.live.deepseek as deepseek
    bodies = []

    def fake(system, user, *, record, model):
        assert model == "deepseek-flash" and callable(record) and "JSON" in system
        bodies.append(json.loads(user))
        return answer(len(bodies))
    monkeypatch.setattr(deepseek, "summarize", fake)
    factory = lambda: InfiniteContext(version=5, t=500, choose="cost")
    rw = Rewriter(factory)                      # as live/proxy.py: no service at construction, one per request
    service = factory().requires_summary         # ... and only for a method that asks for it
    for inp in requests():
        rw.rewrite_body(dict(model='gpt-5', instructions='sys', input=copy.deepcopy(inp), tools=[], stream=True), 's',
                        summarizer=Recorder() if service else None)
    return next(iter(rw.sessions.values())).ctx, bodies


def test_deepseek_writes_the_state_and_carries_it_forward(monkeypatch):
    ctx, bodies = deepseek_run(monkeypatch, lambda n: json.dumps(dict(state=f"STATE {n}: parser test still failing")))
    assert len(bodies) >= 2 and ctx.M["v5_writer_deepseek-flash"] == len(bodies)
    assert bodies[0]["previous_state"] == "" and "STATE 1" in bodies[1]["previous_state"]
    assert any("cat f" in line for line in bodies[0]["steps"])
    summaries = [x for x in ctx.ctx if x["seg"] == "summary"]
    assert len(summaries) == 1 and f"[state]\nSTATE {len(bodies)}" in summaries[0]["text"]


def test_a_failed_deepseek_call_falls_back_to_the_mechanical_summary(monkeypatch):
    ctx, bodies = deepseek_run(monkeypatch, lambda n: "not json")
    assert len(bodies) == 2 and ctx.M["v5_writer_fallback"] == 2 and ctx.M["v5_choose_deadline"] >= 3
    assert all("[state]" not in (x.get("text") or "") for x in ctx.ctx if x["seg"] == "summary")


def test_a_fallback_keeps_the_state_written_so_far(monkeypatch):
    ctx, bodies = deepseek_run(monkeypatch, lambda n: json.dumps(dict(state="STATE ONE")) if n == 1 else "not json")
    assert ctx.M["v5_writer_deepseek-flash"] == 1 and ctx.M["v5_writer_fallback"] >= 1
    summaries = [x for x in ctx.ctx if x["seg"] == "summary"]
    assert len(summaries) == 1 and summaries[0]["text"].count("STATE ONE") == 1


def test_the_proxy_attaches_a_summary_service_for_the_deepseek_writer():
    from ctxpress.methods import build
    entry = {"class": "infinite-context", "args": dict(version=5, t=102400, tool_dialect="full", choose="cost",
             context_profile=dict(version=1, max_context_tokens=128000, compression_trigger_tokens=102400))}
    assert build(entry).requires_summary and build(entry).writer_model == "deepseek-flash"
    assert not build({"class": "infinite-context", "args": dict(version=5, t=102400)}).requires_summary


def test_without_a_summary_service_summaries_are_planned_and_written_mechanically():
    M = counters(lambda: InfiniteContext(version=5, t=500, choose="cost"))
    assert M["v5_choose_deadline"] > 0 and M["v5_writer_failed"] == 0 and M["v5_writer_fallback"] == 0


def test_writer_input_keeps_the_start_and_the_newest_steps():
    m = InfiniteContext(version=5, t=500, choose="cost", writer_input_chars=10000)
    steps = [dict(seg="out", id=i, text=f"out {i} " + "x" * 3000) for i in range(20)]
    lines = m._writer_input(None, steps)
    assert sum(len(l) for l in lines) <= 10100 and "steps left out" in "".join(lines)
    assert lines[0].startswith("output #0:") and lines[-1].startswith("output #19:") and "chars cut" in lines[-1]


def test_the_launcher_opens_deepseeks_route_for_the_cost_method():
    from ctxpress.benchmarks.milestone.runner import deepseek_writer
    assert deepseek_writer(dict(**{"class": "infinite-context"}, args=dict(version=5, t=102400, choose="cost")))
    assert not deepseek_writer(dict(**{"class": "infinite-context"}, args=dict(version=5, t=102400)))
    assert not deepseek_writer(dict(**{"class": "infinite-context"}, args=dict(version=5, choose="cost", writer_model="")))
