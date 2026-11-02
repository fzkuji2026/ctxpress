"""ToolFields: edit receipts from the working tree and [unchanged] for repeated outputs."""
import subprocess
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import ToolFields
from tests.test_readless import code_mode_pair, head, sent, shell_pair

LOG = "\n".join(f"ok  \tpkg/m{i}\t0.{i}s" for i in range(30))


def repo(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (tmp_path / "m.py").write_text("".join(f"line {i}\n" for i in range(50)))
    git("add", "."); git("commit", "-qm", "init")
    return tmp_path


def test_receipt_shows_the_changed_lines_of_this_step_only(tmp_path):
    w = repo(tmp_path)
    rw = Rewriter(lambda: ToolFields(workdir=str(w)))
    items = head()
    rw.rewrite_body(dict(input=items), 's')                       # first request: baseline of the tree
    (w / "m.py").write_text("".join(("CHANGED 10\n" if i == 10 else f"line {i}\n") for i in range(50)))
    items += shell_pair('a', "python3 edit.py", "")
    out, _ = rw.rewrite_body(dict(input=items), 's')
    text = sent(out, 'a')
    assert "[workspace" in text and "m.py: modified" in text and "+CHANGED 10" in text and "-line 10" in text
    (w / "m.py").write_text("".join(("CHANGED 10\n" if i == 10 else "CHANGED 40\n" if i == 40 else f"line {i}\n")
                                    for i in range(50)))
    (w / "new.py").write_text("a\nb\n")
    items += shell_pair('b', "python3 edit2.py", "")
    out, _ = rw.rewrite_body(dict(input=items), 's')
    text = sent(out, 'b')
    assert "+CHANGED 40" in text and "+CHANGED 10" not in text     # only the hunk this step made
    assert "new.py: new file, 2 lines" in text
    assert "+CHANGED 10" in sent(out, 'a')                        # earlier output stays as it was
    items += shell_pair('c', "ls", "m.py new.py")
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert sent(out, 'c') == "m.py new.py"                        # nothing changed: no field


def test_repeated_output_becomes_unchanged_and_a_new_output_stays(tmp_path):
    w = repo(tmp_path)
    rw = Rewriter(lambda: ToolFields(workdir=str(w)))
    items = head() + code_mode_pair('a', [('tail -30 /tmp/r.txt', LOG)])
    rw.rewrite_body(dict(input=items), 's')
    items += code_mode_pair('b', [('tail -30 /tmp/r.txt', LOG), ('git diff --check', '')])
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert "[unchanged" in sent(out, 'b') and "ok  \\tpkg/m3" not in sent(out, 'b')
    items += code_mode_pair('c', [('tail -30 /tmp/r.txt', LOG + "\nFAIL pkg/x")])
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert "FAIL pkg/x" in sent(out, 'c')


def test_history_present_at_start_is_never_changed(tmp_path):
    w = repo(tmp_path)
    rw = Rewriter(lambda: ToolFields(workdir=str(w)))
    items = head() + code_mode_pair('a', [('tail -30 /tmp/r.txt', LOG)]) + code_mode_pair('b', [('tail -30 /tmp/r.txt', LOG)])
    out, _ = rw.rewrite_body(dict(input=items), 's')              # a resumed session: both already there
    assert "[unchanged" not in sent(out, 'b')
    items += code_mode_pair('c', [('tail -30 /tmp/r.txt', LOG)])
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert "[unchanged" in sent(out, 'c')


def test_instructions_carry_the_guidance():
    assert "[workspace]" in ToolFields().instructions


def test_ledger_reports_passing_items_and_files_changed_since(tmp_path):
    w = repo(tmp_path)
    m = ToolFields(receipts=False, repeats=False, ledger=True, workdir=str(w))
    assert [t["name"] for t in m.agent_tools] == ["task_ledger"] and "task_ledger" in m.instructions
    text, _ = m.call_tool("task_ledger", dict(items=[dict(id="r1", requirement="add X", files=["m.py"]),
                                                     dict(id="r2", requirement="add Y", files=["m.py"])]))
    assert "0/2" in text and "not verified yet" in text
    text, _ = m.call_tool("task_ledger", dict(items=[dict(id="r1", check="pytest -k x", result="pass"),
                                                     dict(id="r2", check="pytest -k y", result="pass")]))
    assert text.startswith("ALL 2 items verified")
    (w / "m.py").write_text("changed\n")
    text, _ = m.call_tool("task_ledger", {})
    assert "0/2" in text and "changed since: m.py" in text
