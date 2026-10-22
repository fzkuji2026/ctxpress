"""infinite-context cost mode in the host: the deadline summary, requirements residency, the hold, recall reads."""
import collections, copy, dataclasses, json
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
    assert s.protect_user and s.spec_keep_frac == 0.15 and s.guard_active == 15 and s.cmd_max_chars == 300
    assert s.release_at_topic and s.commit_topic
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


def boundary_model():
    m = InfiniteContext(version=5, t=100000, choose="cost")
    m._use.model = 1                              # every output below has been seen
    return m


def add_at(m, x, req):
    m._use.req = req
    m._use.add(x)
    m._use.model += 1


def test_a_topic_change_releases_holds_of_the_old_topic_unless_used_again():
    m = boundary_model()
    old, again, new = (dict(id=i, seg="out", text=c * 4000, call_text="cat %s.py" % c) for i, c in ((1, "a"), (2, "b"), (3, "c")))
    add_at(m, old, 10)
    add_at(m, again, 10)
    m._use.req = 14
    m._topic_use = 12                             # the topic changed at request 14 (lead of two)
    m._use.used[2] = 14                           # the new topic used it again
    add_at(m, new, 14)
    m._use.req = 15
    held = m._holding(FakeSim([old, again, new]))
    assert 1 not in held and {2, 3} <= held
    m.spec = dataclasses.replace(m.spec, release_at_topic=False)
    assert 1 in m._holding(FakeSim([old, again, new]))


def call(m, sim, i, cmd):
    m.on_ingest(sim, dict(id=i, seg="call", call_id="c%d" % i, text=cmd))


def test_a_commit_releases_the_documents_it_names_and_keeps_those_read_ahead():
    m, sim = boundary_model(), FakeSim([])
    call(m, sim, 1, "cat srs/M01_SRS.md srs/M02_SRS.md")
    call(m, sim, 3, "sed -n 1,200p /e2e_workspace/srs/M03_SRS.md")
    both = dict(id=2, seg="out", kind="spec", call_id="c1", text="r" * 4000)
    ahead = dict(id=4, seg="out", kind="spec", call_id="c3", text="s" * 4000)
    sim.ctx = [both, ahead]
    call(m, sim, 5, "git add src && git commit -m 'Implement M01' && git tag agent-impl-M01 && cat TASK_QUEUE.md")
    assert m._resident(sim) == {2, 4}             # M02 in the same read is still open
    call(m, sim, 6, "git commit -am 'Implement M02' && git tag agent-impl-M02")
    assert m._resident(sim) == {4}
    call(m, sim, 7, "git tag -l && git log --oneline")
    assert set(m._done) == {"Implement", "M01", "M02", "agent-impl-M01", "agent-impl-M02"}
    call(m, sim, 9, "cat /e2e_workspace/srs/M01_SRS.md")       # rework after the commit: open again
    sim.ctx.append(dict(id=10, seg="out", kind="spec", call_id="c9", text="r" * 400))
    assert 10 in m._resident(sim) and 2 not in m._resident(sim)


def test_a_commit_not_a_first_read_of_a_document_is_the_topic_change():
    m, sim = InfiniteContext(version=5, t=100000, choose="cost"), FakeSim([])
    m._new_spec, m._use.req = True, 20
    m._observe(sim)
    assert not m._topic_pending and m._topic_use == -1
    call(m, sim, 1, "git commit -m 'Implement M01' && git tag agent-impl-M01")
    m._observe(sim)
    assert m._topic_pending and m._topic_use == 17 and sim.M["v5_topic_commit"] == 1


def test_deepseek_json_with_raw_line_breaks_or_sections_is_a_state(monkeypatch):
    ctx, bodies = deepseek_run(monkeypatch, lambda n: '{"state": "TASK: fix the parser\nDONE:\n- M01"}')
    assert ctx.M["v5_writer_deepseek-flash"] == len(bodies) >= 1 and ctx.M["v5_writer_failed"] == 0
    assert any("TASK: fix the parser\nDONE:\n- M01" in (x.get("text") or "") for x in ctx.ctx if x["seg"] == "summary")
    ctx, bodies = deepseek_run(monkeypatch, lambda n: json.dumps(dict(state=dict(TASK="queue", DONE=["M01 parser"]))))
    text = next(x["text"] for x in ctx.ctx if x["seg"] == "summary")
    assert ctx.M["v5_writer_failed"] == 0 and "TASK:\nqueue" in text and "DONE:\n- M01 parser" in text


def test_a_markdown_rule_in_the_state_is_carried_forward_whole():
    from ctxpress.methods.cliff import SUMMARY_HEADER
    from ctxpress.methods.infinite_v5 import STEPS_MARK, _state_of
    text = SUMMARY_HEADER + "\n\n[state]\nTASK: a\n\n---\n\nDONE: b" + STEPS_MARK + "- [exec] pytest"
    assert _state_of(text) == "TASK: a\n\n---\n\nDONE: b"


def test_a_writer_that_failed_twice_is_tried_again_later():
    from ctxpress.methods.infinite_v5 import WRITER_RETRY
    m, sim = InfiniteContext(version=5, t=100000, choose="cost"), FakeSim([])
    sim.summarizer, sim.model_text = object(), None
    m._writer_fails, m._use.req, m._writer_retry = 2, 10, 10 + WRITER_RETRY
    assert not m._can_write(sim)
    m._use.req = 10 + WRITER_RETRY
    assert m._can_write(sim)


def test_only_the_newest_task_queue_listing_stays_resident():
    m, sim = boundary_model(), FakeSim([])
    call(m, sim, 1, "cat /e2e_workspace/TASK_QUEUE.md")
    call(m, sim, 3, "cat /e2e_workspace/srs/M02_SRS.md")
    call(m, sim, 5, "git add -A && git commit -m 'Implement M01' && git tag agent-impl-M01 && cat /e2e_workspace/TASK_QUEUE.md")
    sim.ctx = [dict(id=i, seg="out", kind="spec", call_id="c%d" % (i - 1), text="q" * 400) for i in (2, 4, 6)]
    assert m._resident(sim) == {4, 6} and m._spec_skip == {2}


def test_git_commit_and_tag_forms():
    from ctxpress.methods.infinite_v5 import _committed
    assert _committed("git status --short\ngit tag\ngit log --oneline -3") is None
    assert _committed("git tag 2>/dev/null | tail") is None and _committed("git tag --list 'agent-*'") is None
    assert _committed("git -C /repo commit -m 'Implement M01' && git -C /repo tag agent-impl-M01") == \
        {"Implement", "M01", "agent-impl-M01"}
    assert _committed("git tag agent-impl-M02>/tmp/tag.log") == {"agent-impl-M02"}


def test_the_deepseek_route_needs_a_key_on_the_host(monkeypatch, tmp_path):
    import pathlib
    from ctxpress.live import deepseek
    from ctxpress.benchmarks.milestone.driver import worker_env
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert not deepseek.key_available() and "DEEPSEEK_API_KEY" not in worker_env("auth")
    (tmp_path / ".config/ctxpress").mkdir(parents=True)
    (tmp_path / ".config/ctxpress/deepseek-api-key").write_text("\n")
    assert not deepseek.key_available()                         # an empty key file is no key
    (tmp_path / ".config/ctxpress/deepseek-api-key").write_text("key")
    assert deepseek.key_available()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    assert worker_env("auth")["DEEPSEEK_API_KEY"] == "k" and worker_env("auth")["CTXPRESS_CODEX_AUTH_FILE"] == "auth"


def test_fixed_mode_holds_specs_as_before():
    m = InfiniteContext(version=5, t=100000, tool_dialect="wire", entry=True, segment=True, topic=True, guard=True,
                        hold="value", supersede=True, spec_keep_frac=0.1)
    a = dict(id=1, seg="out", kind="spec", text="a" * 24000, call_text="cat srs/A_SRS.md")
    b = dict(id=2, seg="out", kind="spec", text="b" * 24000, call_text="cat srs/B_SRS.md")
    m._use.req = 20
    m._use.add(b)
    m._use.add(a)
    m._use.used[1] = 21
    m._use.model, m._use.req = 5, 22
    assert m._holding(FakeSim([a, b])) >= {1, 2}


def test_the_writer_keeps_outputs_it_judges_not_recoverable(monkeypatch):
    keep = {}

    def answer(n):
        return json.dumps(dict(state=f"STATE {n}", keep=keep.setdefault("ids", [])))
    ctx, bodies = deepseek_run(monkeypatch, lambda n: answer(n))
    assert ctx.M["v5_writer_kept"] == 0
    first = next(int(l.split("#")[1].split(":")[0].split(" ")[0]) for l in bodies[0]["steps"] if l.startswith("output #"))
    keep["ids"] = [first, "junk"]
    ctx, bodies = deepseek_run(monkeypatch, lambda n: answer(n))
    assert ctx.M["v5_writer_kept"] >= 1 and any(x.get("id") == first and x["seg"] == "out" for x in ctx.ctx)


def test_an_edit_supersedes_earlier_reads_and_identical_outputs_supersede():
    m, sim = InfiniteContext(version=5, t=100000, choose="cost", stale_after_edit=True, same_output=True), FakeSim([])
    m._use.model = 1
    read = dict(id=1, seg="out", text="def f(): pass\n" * 30, res=["src/a.py"])
    m._use.add(read)
    m.on_ingest(sim, dict(id=2, seg="call", call_id="c2", kind="edit", res=["src/a.py"], text="apply_patch a.py"))
    assert m._use.superseded(read)
    m.on_ingest(sim, dict(id=3, seg="out", call_id="c3", text="# Task Queue\n" + "- M01: See SRS\n" * 20))
    m.on_ingest(sim, dict(id=4, seg="out", call_id="c4", text="# Task Queue\n" + "- M01: See SRS\n" * 20))
    assert m._use.superseded(dict(id=3)) and not m._use.superseded(dict(id=4))


def test_failure_lines_in_the_middle_of_a_long_log_reach_the_writer():
    m = InfiniteContext(version=5, t=500, choose="cost")
    log = "x\n" * 800 + "FAILED tests/test_parser.py::test_empty - IndexError\n" + "y\n" * 800
    line = m._writer_input(None, [dict(seg="out", id=7, text=log)])[0]
    assert "FAILED tests/test_parser.py::test_empty" in line
