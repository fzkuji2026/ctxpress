from ctxpress.live.context import LiveContext
import ctxpress.methods as M


def test_live_placeholder_and_retrieve():
    ctx = LiveContext(M.ARC(n=1))
    ctx.add_message("user", "fix the bug")
    for i in range(3):
        ctx.add_call(f"c{i}", f"sed -n 1,200p a/f{i}.go")
        ctx.add_output(f"c{i}", f"content of file {i}\n" * 50)
    view = ctx.before_request()
    assert view[0]["role"] == "user" and view[0]["text"] == "fix the bug"      # the task is pinned
    outs = [v for v in view if v["seg"] == "out"]
    assert outs[-1]["form"] == "full" and outs[0]["form"] == "memid"
    assert "stored at #" in outs[0]["text"]
    assert ctx.retrieve(outs[0]["id"]).startswith("content of file 0")


def test_live_fault_marks_reread():
    m = M.PichayApprox(a=0, memory=None)
    ctx = LiveContext(m)
    ctx.add_message("user", "task")
    ctx.add_call("c0", "sed -n 1,200p a/x.go"); ctx.add_output("c0", "x" * 400)
    ctx.add_call("c1", "ls"); ctx.add_output("c1", "a b")
    ctx.before_request(); ctx.before_request()
    ctx.add_call("c2", "sed -n 1,200p a/x.go")                                    # re-read of a paged-out file
    assert ctx.M["faults"] == 1 and "a/x.go" in m.pinned


def test_live_summary_uses_summarizer():
    ctx = LiveContext(M.CodexAutoCompact(t=100), summarizer=lambda items: f"SUMMARY of {len(items)} items")
    ctx.add_message("user", "task")
    ctx.add_call("c", "cat a/x.go"); ctx.add_output("c", "x" * 2000)
    view = ctx.before_request()
    assert view[0]["text"] == "task" and view[1]["seg"] == "summary" and view[1]["text"].startswith("SUMMARY")


def test_live_summary_skipped_without_model():
    ctx = LiveContext(M.CodexAutoCompact(t=100))
    ctx.add_message("user", "task")
    ctx.add_call("c", "cat a/x.go"); ctx.add_output("c", "x" * 2000)
    view = ctx.before_request()
    assert ctx.M["summary_skipped"] == 1 and any(v["seg"] == "out" and v["form"] == "full" for v in view)
