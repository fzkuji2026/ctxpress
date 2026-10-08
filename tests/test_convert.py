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
