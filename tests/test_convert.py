"""Official releases converted to Harbor tasks, then read by their benchmark families. Synthetic stand-in releases."""
import json, os, subprocess, sys

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
    for level, name in ((1, "1_Square_matrix_multiplication_.py"), (2, "7_Matmul_ReLU.py"), (3, "4_LeNet5.py")):
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


KERNELBENCH_STUB = {
    "kernelbench/__init__.py": "",
    "kernelbench/kernel_static_checker.py": (
        "STRICT_CHECKS = ['code_bypass']\nWARNING_CHECKS = ['torch_computation_ops', 'pytorch_wrap']\n"
        "def validate_kernel_static(code, backend='cuda', precision='fp16', forbidden=None, warnings=None):\n"
        "    bad = 'torch_computation_ops' in forbidden and 'torch.matmul' in code\n"
        "    return (not bad, ['torch op'] if bad else [], [])\n"),
    "kernelbench/eval.py": (
        "class R:\n    def __init__(self, **k): self.__dict__.update(k)\n"
        "def get_tolerance_for_precision(precision):\n    return 1e-4\n"
        "def eval_kernel_against_ref(ref, src, num_correct_trials=1, num_perf_trials=10, measure_performance=False,"
        " backend='cuda', **k):\n"
        "    assert get_tolerance_for_precision('fp32') == 1e-2 and num_perf_trials == 10 and num_correct_trials == 5\n"
        "    runtime = float(src.split('RUNTIME=')[1].split()[0]) if 'RUNTIME=' in src else -1.0\n"
        "    return R(compiled=runtime > 0, correctness=runtime > 0, runtime=runtime, ref_runtime=100.0, metadata={})\n"),
}


def test_kernelbench_continual_follows_cliffcompaction(tmp_path):
    out = kernelbench.convert(kernel_source(tmp_path), tmp_path / "kb", image="kb:1", revision="v0.1", protocol="continual")
    tasks = benchmarks.get("kernelbench").task_instances(out)
    assert [t["id"] for t in tasks] == ["kernelbench-l3-004-LeNet5"]               # Level 3 only by default
    task_dir = out / tasks[0]["id"]
    instruction = (task_dir / "instruction.md").read_text()
    assert "evaluate_kernel.py" in instruction and "PyTorch compute operators may not be used" in instruction
    manifest = json.loads((out / "dataset_manifest.json").read_text())
    assert manifest["protocol"] == "continual" and manifest["tolerance"] == 1e-2 and manifest["perf_trials"] == 10
    assert (task_dir / "environment" / "kb_protocol.py").read_text() == (task_dir / "tests" / "kb_protocol.py").read_text()
    for name in ("environment/evaluate_kernel.py", "environment/kb_protocol.py", "tests/kernelbench_verify.py"):
        compile((task_dir / name).read_text(), name, "exec")
    work, logs, stub = tmp_path / "workspace", tmp_path / "logs", tmp_path / "stub"
    for relative, text in KERNELBENCH_STUB.items():
        (stub / relative).parent.mkdir(parents=True, exist_ok=True); (stub / relative).write_text(text)
    (work / "candidates").mkdir(parents=True)
    tests = task_dir / "tests"
    script = (tests / "kernelbench_verify.py").read_text().replace("/workspace", work.as_posix()) \
        .replace("/logs", logs.as_posix()).replace("/tests", tests.as_posix())
    env = dict(os.environ, PYTHONPATH=str(stub))

    def score():
        subprocess.run([sys.executable, "-c", script], check=True, env=env)
        return json.loads((logs / "verifier" / "reward.json").read_text())

    assert score() == dict(compiled=0, correct=0, speedup=0.0, fast_1=0, score=0.1, candidates=0)
    (work / "candidates" / "1-a.py").write_text("# RUNTIME=50 \n")                    # 2x
    (work / "candidates" / "2-b.py").write_text("# RUNTIME=5 \ntorch.matmul(a, b)\n")  # fast but a PyTorch fallback
    (work / "candidates" / "3-c.py").write_text("# RUNTIME=50 \n")                    # duplicate of the first
    (work / "model_new.py").write_text("# RUNTIME=200 \n")                           # the final kernel is slower
    result = score()
    assert result["speedup"] == 2.0 and result["score"] == 2.0 and result["candidates"] == 3 and result["fast_1"] == 1
    (work / "candidates" / "4-d.py").write_text("# RUNTIME=1 \n")                     # 100x is clamped to 10
    assert score()["score"] == 10.0


def test_longbench_answers_stay_in_tests_and_selection_is_honoured(tmp_path):
    out = longbench.convert(longbench_source(tmp_path), tmp_path / "lb", image="python:3.12-slim", revision="hf-rev", difficulty="hard")
    tasks = benchmarks.get("longbench-v2").task_instances(out)
    assert [t["id"] for t in tasks] == ["longbench-v2-66f36490821e116aacb2cc22"]
    task_dir = out / tasks[0]["id"]
    assert (task_dir / "tests" / "expected.txt").read_text().strip() == "C"
    assert "(C) 2003" in (task_dir / "instruction.md").read_text() and "answer" not in json.loads(
        (out / "dataset_manifest.json").read_text())
    assert (task_dir / "environment" / "context.txt").read_text().startswith("A long document.")
    assert plan_for(tmp_path, "longbench-v2", out, tasks[0]["id"])["run_count"] == 1


def test_longbench_verifier_is_the_official_answer_extraction(tmp_path):
    out = longbench.convert(longbench_source(tmp_path), tmp_path / "lb", image="python:3.12-slim", revision="hf-rev")
    tests = out / "longbench-v2-66f36490821e116aacb2cc22" / "tests"
    instruction = (tests.parent / "instruction.md").read_text()
    assert "What is the correct answer to this question: Which year?" in instruction and "(C) 2003" in instruction
    assert 'Format your response as follows: "The correct answer is (insert answer here)".' in instruction
    work, logs = tmp_path / "workspace", tmp_path / "logs"
    work.mkdir()
    script = (tests / "longbench_verify.py").read_text().replace("/workspace", work.as_posix()) \
        .replace("/logs", logs.as_posix()).replace("/tests", tests.as_posix())
    for given, reward in (("The correct answer is (C)", "1"), ("**The correct answer is C**", "1"),
                          ("C", "0"), ("The correct answer is (B)", "0")):
        (work / "answer.txt").write_text(given)
        subprocess.run([sys.executable, "-c", script], check=True)
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


def officebench_source(tmp_path, root_name="OfficeBench"):
    root = tmp_path / root_name
    for task, keyword in (("1-3", "3pm"), ("2-7", "noon")):
        sub = root / "tasks" / task / "subtasks"; sub.mkdir(parents=True)
        (root / "tasks" / task / "testbed" / "data").mkdir(parents=True)
        (root / "tasks" / task / "testbed" / "data" / "notes.txt").write_text("meeting at " + keyword, encoding="utf-8")
        (sub / "0.json").write_text(json.dumps(dict(task="Write the meeting time to data/time.txt.", evaluation=[
            dict(function="evaluate_contain", args=dict(file="data/time.txt", keywords=[keyword]))])), encoding="utf-8")
    (root / "apps" / "word_app").mkdir(parents=True)
    (root / "apps" / "__init__.py").write_text("")
    (root / "apps" / "word_app" / "__init__.py").write_text("")
    (root / "apps" / "word_app" / "word_read_file.py").write_text(
        "def read_file(path):\n    return open(path).read()\n", encoding="utf-8")
    (root / "utils").mkdir()
    # Like the official utils/evaluate.py: imports apps.* through the parent directory and reads /testbed paths.
    (root / "utils" / "evaluate.py").write_text(
        "import os, sys\nsys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))\n"
        "from apps.word_app import word_read_file\n\n"
        "def evaluate_contain(testbed_dir, args):\n"
        "    path = os.path.join(testbed_dir, args['file'])\n"
        "    return os.path.exists(path) and all(k in word_read_file.read_file(path) for k in args['keywords'])\n",
        encoding="utf-8")
    (root / "evaluation.py").write_text("import fire\nfrom utils.evaluate import evaluate_contain\n", encoding="utf-8")
    return root


def test_officebench_uses_the_official_checks_on_the_final_testbed(tmp_path):
    from ctxpress.benchmarks.convert import officebench
    out = officebench.convert(officebench_source(tmp_path), tmp_path / "ob", image="officebench:fixture", revision="abc123")
    tasks = benchmarks.get("officebench").task_instances(out)
    assert [t["id"] for t in tasks] == ["officebench-1-3-0", "officebench-2-7-0"]
    task_dir = out / "officebench-1-3-0"
    assert (task_dir / "environment" / "testbed" / "data" / "notes.txt").is_file()
    assert (task_dir / "environment" / "apps" / "word_app" / "word_read_file.py").is_file()
    assert (task_dir / "tests" / "officebench" / "apps" / "word_app" / "word_read_file.py").is_file()
    assert "evaluate_contain" in (task_dir / "tests" / "subtask.json").read_text()
    instruction = (task_dir / "instruction.md").read_text()
    assert "3pm" not in instruction and "/apps/word_app/word_read_file.py" in instruction
    compile((task_dir / "tests" / "officebench_verify.py").read_text(), "verify", "exec")
    assert plan_for(tmp_path, "officebench", out, "officebench-1-3-0")["run_count"] == 1


def test_officebench_takes_acon_tasks_and_split(tmp_path):
    from ctxpress.benchmarks.convert import officebench
    acon = officebench_source(tmp_path, "acon-officebench")
    (tmp_path / "test_tasks.txt").write_text("2-7\n")
    with pytest.raises(ValueError, match="tasks_source"):
        officebench.convert(officebench_source(tmp_path), tmp_path / "x", image="i", revision="r", tasks=acon / "tasks")
    out = officebench.convert(tmp_path / "OfficeBench", tmp_path / "ob", image="i", revision="r", tasks=acon / "tasks",
                              task_list=tmp_path / "test_tasks.txt", tasks_source="microsoft/acon@d63f9ae")
    assert [t["id"] for t in benchmarks.get("officebench").task_instances(out)] == ["officebench-2-7-0"]
    manifest = json.loads((out / "dataset_manifest.json").read_text())
    assert manifest["task_source"] == "microsoft/acon@d63f9ae" and manifest["task_list"] == ["2-7"]
    (tmp_path / "missing.txt").write_text("9-9\n")
    with pytest.raises(ValueError, match="not found: 9-9"):
        officebench.convert(tmp_path / "OfficeBench", tmp_path / "ob2", image="i", revision="r", tasks=acon / "tasks",
                            task_list=tmp_path / "missing.txt", tasks_source="acon")


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


# Stand-ins with the signatures and behaviour of letta-ai/recovery-bench (replay.py, prompts.py, utils.py, pipeline.py).
RECOVERY = {
    "replay.py": '''import json
from dataclasses import dataclass
from pathlib import Path

@dataclass
class ReplayCommand:
    command: str
    keystrokes: str
    timeout_sec: float = 15.0

def _find_trajectory_file(folder):
    for path in (Path(folder) / "agent" / "trajectory.json", Path(folder) / "trajectory.json"):
        if path.exists():
            return path

def _load_trajectory(path):
    data = json.loads(Path(path).read_text())
    return data.get("steps", data) if isinstance(data, dict) else data

def _extract_from_step(step):
    source, content = step.get("source", ""), step.get("message", "")
    commands = []
    if source == "agent":
        for call in step.get("tool_calls", []):
            ks = call.get("arguments", {}).get("keystrokes", "")
            if ks:
                cmd = ks.rstrip("\\n")
                commands.append(ReplayCommand(cmd if not cmd.startswith("C-") else "", ks))
    role = "assistant" if source == "agent" else source
    return commands, ({"role": role, "content": content} if role in ("user", "assistant", "system") else None)

def extract_commands(folder):
    return [c for step in _load_trajectory(_find_trajectory_file(folder)) for c in _extract_from_step(step)[0]]

def extract_messages(folder):
    return [m for step in _load_trajectory(_find_trajectory_file(folder)) for m in [_extract_from_step(step)[1]] if m]

def _find_interrupted_commands(commands):
    return {i for i in range(len(commands) - 1) if commands[i + 1].keystrokes.strip().startswith("C-") and commands[i].command}
''',
    "prompts.py": '''RECOVERY_PREAMBLE = "RECOVERY MODE: The previous attempt to complete this task failed."

def build_recovery_instruction(instruction, message_context=None):
    parts = [RECOVERY_PREAMBLE]
    if message_context:
        parts.append(f"--- PREVIOUS ATTEMPT CONTEXT ---\\n{message_context}")
    parts.append(f"--- ORIGINAL TASK ---\\n{instruction}")
    return "\\n\\n".join(parts)

def format_messages_as_text(messages):
    return "\\n\\n".join(f"[{m.get('role', 'unknown').upper()}]: {m.get('content', '')}" for m in messages)
''',
    "utils.py": '''import json, os
from pathlib import Path

def get_unsolved_tasks(logs_dir, print_output=False):
    found = []
    for task_id in os.listdir(logs_dir):
        result = Path(logs_dir) / task_id / "result.json"
        if not result.exists():
            continue
        data = json.loads(result.read_text())
        if ((data.get("verifier_result") or {}).get("rewards") or {}).get("reward", 0.0) > 0:
            continue
        found.append(data.get("task_name", task_id))
    return found

def find_trajectory_by_name(task_name, base_folder):
    for item in Path(base_folder).iterdir():
        name = item.name[9:] if len(item.name) > 9 and item.name[8] == "-" else item.name
        if "__" in name:
            name = name.rsplit("__", 1)[0]
        if name == task_name and ((item / "agent" / "trajectory.json").exists() or (item / "trajectory.json").exists()):
            return item
''',
    "pipeline.py": '''from .prompts import build_recovery_instruction, format_messages_as_text
from .replay import extract_messages
from .utils import find_trajectory_by_name

MAX_INSTRUCTION_BYTES = 64_000

def filter_oversized_tasks(task_ids, traces_folder, max_bytes=MAX_INSTRUCTION_BYTES):
    kept = []
    for task_id in task_ids:
        folder = find_trajectory_by_name(task_id, traces_folder)
        messages = extract_messages(folder) if folder else []
        size = len(build_recovery_instruction("(task instruction)", format_messages_as_text(messages)).encode()) if messages else 0
        if size <= max_bytes:
            kept.append(task_id)
    return kept
''',
}


def recovery_sources(tmp_path):
    checkout = tmp_path / "recovery-bench"; (checkout / "recovery_bench").mkdir(parents=True)
    (checkout / "recovery_bench" / "__init__.py").write_text("")
    for name, text in RECOVERY.items():
        (checkout / "recovery_bench" / name).write_text(text)
    tasks = tmp_path / "tb2"
    for name in ("fix-git", "build-tcc", "huge-log"):
        task = tasks / name; (task / "environment").mkdir(parents=True); (task / "tests").mkdir(); (task / "solution").mkdir()
        (task / "task.toml").write_text(f'version = "1.0"\n[task]\nname = "terminal-bench/{name}"\n[verifier]\ntimeout_sec = 300\n')
        (task / "instruction.md").write_text(f"Do {name}.")
        (task / "environment" / "Dockerfile").write_text("FROM tb2/" + name + ":2.0\n")
        (task / "tests" / "test.sh").write_text("#!/bin/bash\npytest /tests/test_outputs.py\n")
        (task / "tests" / "test_outputs.py").write_text("def test_ok():\n    assert True\n")
        (task / "solution" / "solve.sh").write_text("#!/bin/bash\n")
    traces = tmp_path / "initial"

    def calls(*keys):
        return [dict(arguments=dict(keystrokes=k)) for k in keys]

    for folder, task, reward, steps in (
            ("1a2b3c4d-fix-git__abc", "fix-git", 0.0, [dict(source="user", message="start"),
                dict(source="agent", message="try", tool_calls=calls("git reset --hard HEAD~3\n", "sleep 999\n", "C-c", "ls\n"))]),
            ("build-tcc__x", "build-tcc", 1.0, [dict(source="agent", message="ok", tool_calls=calls("make\n"))]),
            ("huge-log__y", "huge-log", 0.0, [dict(source="agent", message="x" * 70_000, tool_calls=calls("cat big\n"))])):
        trace = traces / folder; (trace / "agent").mkdir(parents=True)
        (trace / "agent" / "trajectory.json").write_text(json.dumps(dict(steps=steps)))
        (trace / "result.json").write_text(json.dumps(dict(task_name=task, verifier_result=dict(rewards=dict(reward=reward)))))
    return tasks, traces, checkout


def test_recovery_bench_uses_the_official_selection_instruction_and_replay(tmp_path):
    from ctxpress.benchmarks.convert import recovery
    tasks, traces, checkout = recovery_sources(tmp_path)
    out = recovery.convert(tasks, traces, checkout, tmp_path / "rb", revision="lfs-abc")
    found = benchmarks.get("recovery-bench").task_instances(out)
    assert [t["id"] for t in found] == ["recovery-fix-git"]           # solved and oversized initial attempts are left out
    task_dir = out / "recovery-fix-git"
    instruction = (task_dir / "instruction.md").read_text()
    assert instruction.startswith("RECOVERY MODE") and "[ASSISTANT]: try" in instruction
    assert instruction.endswith("--- ORIGINAL TASK ---\nDo fix-git.")
    assert (task_dir / "environment" / "Dockerfile").read_text() == "FROM tb2/fix-git:2.0\n"   # the image is unchanged
    replay = json.loads((task_dir / "recovery" / "replay.json").read_text())
    assert replay["commands"] == ["git reset --hard HEAD~3", "ls"]        # the interrupted `sleep 999` is skipped
    assert replay["timeout_sec"] == 15 and replay["setup_timeout_multiplier"] == 3.0
    assert (task_dir / "tests" / "test_outputs.py").is_file() and (task_dir / "solution" / "solve.sh").is_file()
    none = recovery.convert(tasks, traces, checkout, tmp_path / "rb-none", revision="lfs-abc", message_mode="none")
    text = (none / "recovery-fix-git" / "instruction.md").read_text()
    assert text.startswith("RECOVERY MODE") and "PREVIOUS ATTEMPT" not in text
    with pytest.raises(ValueError, match="summary"):
        recovery.convert(tasks, traces, checkout, tmp_path / "rb-s", revision="lfs-abc", message_mode="summary")
    assert plan_for(tmp_path, "recovery-bench", out, "recovery-fix-git")["run_count"] == 1


def test_recovery_replay_runs_in_the_container_before_the_agent(tmp_path):
    import asyncio, shlex
    from ctxpress.benchmarks.convert import recovery
    from ctxpress.benchmarks.harbor import modern, worker
    from ctxpress.harness.runtime import codex_agent
    tasks, traces, checkout = recovery_sources(tmp_path)
    out = recovery.convert(tasks, traces, checkout, tmp_path / "rb", revision="lfs-abc")
    request = dict(task=str(out / "recovery-fix-git"))
    assert codex_agent.replay_settings(request) == {"replay": dict(commands=["git reset --hard HEAD~3", "ls"], timeout_sec=15)}
    assert worker.setup_timeout(request) == modern.setup_timeout(request) == {"agent_setup_timeout_multiplier": 3.0}
    plain = dict(task=str(tasks / "fix-git"))
    assert codex_agent.replay_settings(plain) == {} and worker.setup_timeout(plain) == {}

    class Environment:
        def __init__(self):
            self.commands = []

        async def exec(self, command, timeout_sec=None, **kwargs):
            self.commands.append(command)
            if "hang" in command:
                await asyncio.sleep(5)
            if "fail" in command:
                raise RuntimeError("non-zero exit")

    environment = Environment()
    asyncio.run(codex_agent.replay(environment, dict(commands=["fail now", "hang", "echo 'ok'"], timeout_sec=0.05)))
    assert environment.commands == ["bash -lc " + shlex.quote(c) for c in ("fail now", "hang", "echo 'ok'")]
