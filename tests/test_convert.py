"""Official releases converted to Harbor tasks, then read by their benchmark families. Synthetic stand-in releases."""
import json, subprocess, sys

import pytest

from ctxpress import benchmarks
from ctxpress.benchmarks.convert import kernelbench, longbench, sweqa
from ctxpress.harness.jobs import plan as eval_plan
from test_model_accounting import rates

REFERENCE = '''import torch
import torch.nn as nn

class Model(nn.Module):
    def __init__(self):
        super().__init__()
    def forward(self, a, b):
        return torch.matmul(a, b)

def get_inputs():
    return [torch.randn(64, 64), torch.randn(64, 64)]

def get_init_inputs():
    return []
'''


def kernel_source(tmp_path):
    root = tmp_path / "KernelBench-checkout"
    for level, name in ((1, "1_Square_matrix_multiplication_.py"), (2, "7_Matmul_ReLU.py")):
        folder = root / "KernelBench" / f"level{level}"; folder.mkdir(parents=True)
        (folder / name).write_text(REFERENCE, encoding="utf-8")
    return root


def longbench_source(tmp_path):
    rows = [dict(_id="66f36490821e116aacb2cc22", domain="Single-Document QA", sub_domain="Financial", difficulty="hard",
                 length="short", question="Which year?", choice_A="2001", choice_B="2002", choice_C="2003", choice_D="2004",
                 answer="C", context="A long document. " * 2000),
            dict(_id="easy-one", domain="Code", sub_domain="x", difficulty="easy", length="long", question="q?",
                 choice_A="a", choice_B="b", choice_C="c", choice_D="d", answer="A", context="ctx")]
    path = tmp_path / "data.json"; path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def sweqa_source(tmp_path):
    root = tmp_path / "SWE-QA-Bench"; (root / "Benchmark").mkdir(parents=True)
    (root / "repo_commit.txt").write_text("https://github.com/pallets/flask.git " + "a" * 40 + "\n", encoding="utf-8")
    (root / "Benchmark" / "flask.jsonl").write_text(
        json.dumps(dict(question="How does Flask resolve the app context?", answer="Through LocalStack ...")) + "\n", encoding="utf-8")
    return root


def plan_for(tmp_path, name, root, task):
    binary = tmp_path / "bin"; binary.mkdir(exist_ok=True); (binary / "codex").write_bytes(b"synthetic fixture")
    cfg = dict(schema="ctxpress.eval", version=1, benchmark=name, start_mode="task_start", scope="benchmark", model="main",
               tasks=[task], backend="codex_docker", methods=[{"class": "HostDefault"}], repeats=1, workers=1,
               prices={"models": {"main": rates()}}, environment={"data": str(root), "bindir": str(binary)})
    return eval_plan.compile_plan(cfg)


def test_kernelbench_problems_become_gpu_tasks_with_the_official_check(tmp_path):
    out = kernelbench.convert(kernel_source(tmp_path), tmp_path / "kb", image="kb-cuda:fixture", revision="v0.1",
                              levels=(1, 2))
    tasks = benchmarks.get("kernelbench").task_instances(out)
    ids = sorted(t["id"] for t in tasks)
    assert ids == ["kernelbench-l1-001-Square_matrix_multiplication_", "kernelbench-l2-007-Matmul_ReLU"]
    task = next(t for t in tasks if t["id"] == ids[0])
    assert task["initial_state"]["environment"]["gpus"] == 1
    instruction = benchmarks.get("kernelbench").agent_instruction(task)
    assert "class Model" in instruction and "/workspace/model_new.py" in instruction
    verifier = (out / ids[0] / "tests" / "kernelbench_verify.py").read_text()
    assert "from kernelbench.eval import eval_kernel_against_ref" in verifier and "ref_runtime / result.runtime" in verifier
    compile(verifier, "kernelbench_verify.py", "exec")
    assert plan_for(tmp_path, "kernelbench", out, ids[0])["run_count"] == 1


def test_longbench_answers_stay_in_tests_and_selection_is_honoured(tmp_path):
    out = longbench.convert(longbench_source(tmp_path), tmp_path / "lb", image="python:3.12-slim", revision="hf-rev", difficulty="hard")
    tasks = benchmarks.get("longbench-v2").task_instances(out)
    assert [t["id"] for t in tasks] == ["longbench-v2-66f36490821e116aacb2cc22"]
    task_dir = out / tasks[0]["id"]
    assert (task_dir / "tests" / "expected.txt").read_text().strip() == "C"
    assert "C. 2003" in (task_dir / "instruction.md").read_text() and "answer" not in json.loads(
        (out / "dataset_manifest.json").read_text())
    assert (task_dir / "environment" / "context.txt").read_text().startswith("A long document.")
    assert plan_for(tmp_path, "longbench-v2", out, tasks[0]["id"])["run_count"] == 1


@pytest.mark.linux_only
def test_longbench_verifier_is_the_official_letter_match(tmp_path):
    out = longbench.convert(longbench_source(tmp_path), tmp_path / "lb", image="python:3.12-slim", revision="hf-rev")
    script = (out / "longbench-v2-66f36490821e116aacb2cc22" / "tests" / "test.sh").read_text()
    work, logs, tests = tmp_path / "workspace", tmp_path / "logs", out / "longbench-v2-66f36490821e116aacb2cc22" / "tests"
    work.mkdir()
    for given, reward in (("c\n", "1"), ("B", "0")):
        (work / "answer.txt").write_text(given)
        text = script.replace("/workspace", str(work)).replace("/logs", str(logs)).replace("/tests", str(tests))
        subprocess.run(["bash", "-c", text], check=True)
        assert (logs / "verifier" / "reward.txt").read_text().strip() == reward


def test_swe_qa_pins_the_commit_and_collects_answers_for_the_official_judge(tmp_path):
    out = sweqa.convert(sweqa_source(tmp_path), tmp_path / "qa", image="python:3.12", revision="main@abc")
    tasks = benchmarks.get("swe-qa").task_instances(out)
    assert [t["id"] for t in tasks] == ["swe-qa-flask-0000"]
    dockerfile = (out / "swe-qa-flask-0000" / "environment" / "Dockerfile").read_text()
    assert "git -C /workspace/repo checkout " + "a" * 40 in dockerfile
    reference = json.loads((out / "swe-qa-flask-0000" / "tests" / "reference.json").read_text())
    assert reference["answer"].startswith("Through LocalStack")
    assert "LocalStack" not in (out / "swe-qa-flask-0000" / "instruction.md").read_text()
    assert plan_for(tmp_path, "swe-qa", out, "swe-qa-flask-0000")["run_count"] == 1


def test_unknown_layouts_fail_loudly(tmp_path):
    bad = sweqa_source(tmp_path)
    (bad / "Benchmark" / "flask.jsonl").write_text(json.dumps(dict(prompt="?", gold="!")) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="question field"):
        sweqa.convert(bad, tmp_path / "qa", image="x", revision="r")
    with pytest.raises(FileNotFoundError):
        kernelbench.convert(tmp_path / "missing", tmp_path / "kb", image="x", revision="r", levels=(1,))
    (tmp_path / "bad.json").write_text(json.dumps([{"_id": "x"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="not a LongBench"):
        longbench.convert(tmp_path / "bad.json", tmp_path / "lb", image="x", revision="r")


def test_command_line(tmp_path):
    out = tmp_path / "kb-cli"
    result = subprocess.run([sys.executable, "-m", "ctxpress.benchmarks.convert", "kernelbench", "--source", str(kernel_source(tmp_path)),
                             "--output", str(out), "--image", "kb:1", "--revision", "v0.1", "--levels", "1"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((out / "dataset_manifest.json").read_text())["dataset"] == "kernelbench"


def test_appworld_reads_migrated_official_tasks(tmp_path):
    from test_official_harbor import adapter_output
    root = adapter_output(tmp_path, "appworld")
    assert benchmarks.get("appworld").task_instances(root)[0]["evaluation"]["dataset"]["name"] == "appworld"


def officebench_source(tmp_path):
    root = tmp_path / "OfficeBench"
    sub = root / "tasks" / "1-3" / "subtasks"; sub.mkdir(parents=True)
    (root / "tasks" / "1-3" / "testbed" / "data").mkdir(parents=True)
    (root / "tasks" / "1-3" / "testbed" / "data" / "notes.txt").write_text("meeting at 3pm", encoding="utf-8")
    (sub / "0.json").write_text(json.dumps(dict(task="Write the meeting time to data/time.txt.", evaluation=[
        dict(function="evaluate_contain", args=dict(file="data/time.txt", keywords=["3pm"]))])), encoding="utf-8")
    (root / "apps" / "word_app").mkdir(parents=True)
    (root / "apps" / "word_app" / "read_file.py").write_text("print('official app')\n", encoding="utf-8")
    (root / "evaluation.py").write_text(
        "import os\n\ndef evaluate_contain(output_dir, args):\n"
        "    path = os.path.join(output_dir, args['file'])\n"
        "    return os.path.exists(path) and all(k in open(path).read() for k in args['keywords'])\n", encoding="utf-8")
    return root


def test_officebench_uses_the_official_checks_on_the_final_testbed(tmp_path):
    from ctxpress.benchmarks.convert import officebench
    out = officebench.convert(officebench_source(tmp_path), tmp_path / "ob", image="officebench:fixture", revision="abc123")
    tasks = benchmarks.get("officebench").task_instances(out)
    assert [t["id"] for t in tasks] == ["officebench-1-3-0"]
    task_dir = out / "officebench-1-3-0"
    assert (task_dir / "environment" / "testbed" / "data" / "notes.txt").is_file()
    assert (task_dir / "environment" / "apps" / "word_app" / "read_file.py").is_file()
    assert "evaluate_contain" in (task_dir / "tests" / "subtask.json").read_text()
    assert "3pm" not in (task_dir / "instruction.md").read_text()
    compile((task_dir / "tests" / "officebench_verify.py").read_text(), "verify", "exec")
    assert plan_for(tmp_path, "officebench", out, "officebench-1-3-0")["run_count"] == 1


@pytest.mark.linux_only
def test_officebench_verifier_runs_the_official_functions(tmp_path):
    from ctxpress.benchmarks.convert import officebench
    out = officebench.convert(officebench_source(tmp_path), tmp_path / "ob", image="x", revision="r")
    tests, testbed, logs = out / "officebench-1-3-0" / "tests", tmp_path / "testbed", tmp_path / "logs"
    (testbed / "data").mkdir(parents=True)
    script = (tests / "officebench_verify.py").read_text().replace("/tests", str(tests)).replace("/testbed", str(testbed)) \
        .replace("/logs", str(logs))
    for content, expected in (("no time here", "0"), ("at 3pm", "1")):
        (testbed / "data" / "time.txt").write_text(content)
        subprocess.run([sys.executable, "-c", script], check=True)
        assert (logs / "verifier" / "reward.txt").read_text().strip() == expected


RECOVERY_REPLAY = '''import json
from dataclasses import dataclass
from pathlib import Path

@dataclass
class ReplayCommand:
    command: str
    keystrokes: str
    timeout_sec: float = 15

def _find_trajectory_file(folder):
    for path in (Path(folder) / "agent" / "trajectory.json", Path(folder) / "trajectory.json"):
        if path.is_file():
            return path

def _load_trajectory(path):
    return json.loads(Path(path).read_text())["steps"]

def _extract_from_step(step):
    if step.get("source") != "agent":
        return []
    return [ReplayCommand(c["arguments"]["keystrokes"].strip(), c["arguments"]["keystrokes"], 7) for c in step.get("tool_calls", [])]
'''

RECOVERY_PIPELINE = r'''from pathlib import Path
import json

def extract_messages(folder):
    return "previous transcript: " + " | ".join(s.get("message", "") for s in json.loads((Path(folder) / "trajectory.json").read_text())["steps"])

def build_recovery_instruction(instruction, message_context):
    return instruction + "\n\nA previous attempt failed. " + message_context
'''


def recovery_sources(tmp_path):
    checkout = tmp_path / "recovery-bench"; (checkout / "recovery_bench").mkdir(parents=True)
    (checkout / "recovery_bench" / "__init__.py").write_text("")
    (checkout / "recovery_bench" / "replay.py").write_text(RECOVERY_REPLAY)
    (checkout / "recovery_bench" / "pipeline.py").write_text(RECOVERY_PIPELINE)
    tasks = tmp_path / "tb2"
    for name in ("fix-git", "build-tcc"):
        task = tasks / name; (task / "environment").mkdir(parents=True); (task / "tests").mkdir()
        (task / "task.toml").write_text(f'version = "1.0"\n[task]\nname = "terminal-bench/{name}"\n[verifier]\ntimeout_sec = 300\n')
        (task / "instruction.md").write_text(f"Do {name}.")
        (task / "environment" / "Dockerfile").write_text("FROM tb2/" + name + ":2.0\n")
        (task / "tests" / "test.sh").write_text("#!/bin/bash\npytest /tests/test_outputs.py\n")
        (task / "tests" / "test_outputs.py").write_text("def test_ok():\n    assert True\n")
    traces = tmp_path / "initial"
    for name, reward in (("fix-git", 0.0), ("build-tcc", 1.0)):
        trace = traces / f"{name}__abc"; trace.mkdir(parents=True)
        steps = [dict(source="user", message="start"),
                 dict(source="agent", message="try", tool_calls=[dict(arguments=dict(keystrokes="git reset --hard HEAD~3\n"))])]
        (trace / "trajectory.json").write_text(json.dumps(dict(steps=steps)))
        (trace / "result.json").write_text(json.dumps(dict(verifier_result=dict(rewards=dict(reward=reward)))))
    return tasks, traces, checkout


def test_recovery_bench_uses_failed_attempts_the_official_replay_and_instruction(tmp_path):
    from ctxpress.benchmarks.convert import recovery
    tasks, traces, checkout = recovery_sources(tmp_path)
    out = recovery.convert(tasks, traces, checkout, tmp_path / "rb", revision="lfs-abc")
    found = benchmarks.get("recovery-bench").task_instances(out)
    assert [t["id"] for t in found] == ["recovery-fix-git"]                     # the successful initial attempt is not used
    task_dir = out / "recovery-fix-git"
    instruction = (task_dir / "instruction.md").read_text()
    assert instruction.startswith("Do fix-git.") and "A previous attempt failed. previous transcript" in instruction
    dockerfile = (task_dir / "environment" / "Dockerfile").read_text()
    assert dockerfile.startswith("FROM tb2/fix-git:2.0") and "RUN bash /tmp/recovery-replay.sh" in dockerfile
    assert "git reset --hard HEAD~3" in (task_dir / "environment" / "replay.sh").read_text()
    assert (task_dir / "tests" / "test_outputs.py").is_file()
    none = recovery.convert(tasks, traces, checkout, tmp_path / "rb-none", revision="lfs-abc", message_mode="none")
    assert (none / "recovery-fix-git" / "instruction.md").read_text() == "Do fix-git."
    assert plan_for(tmp_path, "recovery-bench", out, "recovery-fix-git")["run_count"] == 1
