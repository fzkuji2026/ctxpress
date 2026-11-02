from ctxpress.evolve import actions, archive, evaluate, policy, runtime


GOOD = '''from __future__ import annotations
import re
from ctxpress.evolve.runtime import cli

TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
GUIDANCE = "Use " + TOOL + " head FILE to print the first lines of a file."


def view(cmd, out, ctx):
    lines = out.split("\\n")
    if not re.match(r"^\\s*cat\\s", cmd) or len(lines) < 200:
        return None
    return "\\n".join(lines[:20] + ["[%d more lines]" % (len(lines) - 20)])


def head(argv):
    with open(argv[0]) as f:
        return "".join(f.readlines()[:5])


TOOLS = {"head": head}

if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
'''


def test_static_check_accepts_the_format_and_rejects_writes_and_processes():
    assert policy.static_check(GOOD) == []
    bad = policy.static_check(GOOD.replace("import re", "import re, subprocess"))
    assert any("subprocess" in b for b in bad)
    bad = policy.static_check(GOOD.replace("with open(argv[0]) as f:", "with open(argv[0], 'w') as f:"))
    assert any("only for reading" in b for b in bad)
    bad = policy.static_check(GOOD.replace("raise SystemExit(cli(TOOLS))", "pass"))
    assert any("cli(TOOLS)" in b for b in bad)
    assert any("3.8" in b for b in policy.static_check("x = (y := 1)\nmatch x:\n    case 1: pass\n"))


def test_dynamic_check_runs_the_module_and_its_tools():
    try:
        problems, details = policy.check("t_test_good", GOOD)
        assert problems == []
        assert details["views"] == 1                      # only the long cat is reshaped
        assert {r["tool"] for r in details["runs"]} == {"head"}
        problems, _ = policy.check("t_test_crash", GOOD.replace('return "".join(f.readlines()[:5])', "return 1 / 0"))
        assert any("crashed" in p for p in problems)
    finally:
        import os
        for c in ("t_test_good", "t_test_crash"):
            if os.path.exists(policy.path_of(c)):
                os.remove(policy.path_of(c))


def test_seeds_pass_the_checks():
    for cid in ("s000_outline", "s001_definition"):
        with open(policy.path_of(cid)) as f:
            assert policy.static_check(f.read()) == []
        assert policy.dynamic_check(cid)[0] == []


def test_cli_dispatch(capsys):
    assert runtime.cli({"a": lambda argv: "got " + ",".join(argv)}, ["a", "x", "y"]) == 0
    assert capsys.readouterr().out == "got x,y\n"
    assert runtime.cli({"a": lambda argv: ""}, ["b"]) == 2


def test_reads_of_chained_commands():
    got = actions.reads("cd /app && sed -n '10,40p' src/a.py && cat /app/b.go | head -5; grep -n x c.py")
    assert got == [("src/a.py", (10, 40)), ("b.go", None)]


def test_archive_choice_prefers_fit_and_novel(tmp_path):
    a = archive.Archive(str(tmp_path))
    a.add(id="s0", parent=None, generation=0, status="full", fitness=0.05, descriptor=dict(tool_rate=0.1, read_cut=0.0, unused=0.2))
    a.add(id="s1", parent=None, generation=0, status="full", fitness=0.15, descriptor=dict(tool_rate=0.1, read_cut=0.1, unused=0.2))
    a.add(id="c0", parent="s1", generation=1, status="rejected")
    assert a.get("s1")["children"] == 1 and a.next_id(1) == "g001_c01"
    picks = a.choose(200)
    assert picks.count("s1") > picks.count("s0") > 0
    assert archive.Archive(str(tmp_path)).get("c0")["status"] == "rejected"      # saved


def test_compare_is_paired_over_common_tasks():
    def r(task, manage, reward):
        return dict(task=task, manage=manage, total=manage + 1, reward=reward, unused_share=0.1, read_tokens=100,
                    tool_calls=1, calls=10)
    base = [r("a", 1.0, 1.0), r("a", 1.0, 1.0), r("b", 2.0, 0.5), r("c", 9.0, 1.0)]
    cand = [r("a", 0.8, 1.0), r("b", 1.7, 0.4)]
    c = evaluate.compare(cand, base)
    assert c["tasks"] == 2 and abs(c["saving"] - (1 - 2.5 / 3.0)) < 1e-9
    assert abs(c["reward_diff"] + 0.05) < 1e-9 and not c["feasible"]
    assert evaluate.fitness(c) < c["saving"]


def test_screen_config_forks_with_a_window():
    base = dict(name="x", run=dict(max_calls=600, timeout=7200, grade=True), methods=[], tasks=[], repeats=3, workers=6)
    c = evaluate.config(base, "screen-x", "g001_c00", ["t1"], 2, 2, checkpoints={"t1": "/ck/10"})
    assert c["run"]["max_calls"] == evaluate.CHECKPOINT + evaluate.WINDOW
    assert c["run"]["fork"]["t1"]["checkpoint"] == "/ck/10"
    rl = c["methods"][0]["args"]["methods"][0]
    assert rl["args"] == {"policy": policy.module_name("g001_c00"), "ranged": False, "fresh_only": True}
    assert evaluate.config(base, "ours", None, ["t1"], 1, 1)["methods"][0]["class"] == "infinite-context"
