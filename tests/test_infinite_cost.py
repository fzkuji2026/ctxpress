"""infinite-context cost mode in the host: the deadline summary, requirements residency, the hold, recall reads."""
import collections, copy
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
    assert s.protect_user and s.spec_keep_frac == 0.2 and s.guard_active == 15 and s.cmd_max_chars == 300
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
