"""Recovery-Bench as Harbor tasks: a Terminal-Bench 2.0 task resumed from a failed attempt.

Sources, all on disk: the Terminal-Bench 2.0 Harbor tasks, the recovery-bench checkout (letta-ai/recovery-bench) and
its initial traces (runs/initial-...; one folder per task with trajectory.json and the trial result). Only failed
initial attempts (reward 0) are used, as in the official pipeline. The official code does the two
benchmark-specific steps: recovery_bench.replay extracts the failed agent's commands, which the task image
re-executes to rebuild the corrupted state, and recovery_bench.pipeline builds the recovery instruction from
the task instruction and the previous transcript (message mode full / summary / none). Grading is the original
task's tests.
"""
from __future__ import annotations
import importlib, json, shlex, shutil, sys
from pathlib import Path

from ctxpress.benchmarks.convert.common import new_output, write_manifest, write_task
from ctxpress.core import toml


def official(checkout):
    sys.path.insert(0, str(Path(checkout).expanduser().resolve()))
    try:
        return importlib.import_module("recovery_bench.replay"), importlib.import_module("recovery_bench.pipeline")
    finally:
        sys.path.pop(0)


def reward(trace):
    for name in ("result.json", "trial_result.json"):
        path = trace / name
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            rewards = (data.get("verifier_result") or {}).get("rewards") or {}
            if rewards:
                return max(float(v) for v in rewards.values())
    return None


def commands(replay, trace):
    """Shell commands of the failed attempt, through the official parser."""
    steps = replay._load_trajectory(replay._find_trajectory_file(trace))
    found = []
    for step in steps:
        for command in replay._extract_from_step(step) or []:
            found.append((command.command, getattr(command, "timeout_sec", 15) or 15))
    skipped = set(replay._find_interrupted_commands(steps)) if hasattr(replay, "_find_interrupted_commands") else set()
    return [c for i, c in enumerate(found) if i not in skipped]


def convert(tasks, traces, checkout, output, *, revision, message_mode="full"):
    if message_mode not in ("full", "summary", "none"):
        raise ValueError("message_mode is full, summary or none")
    replay, pipeline = official(checkout)
    tasks, traces = Path(tasks).expanduser().resolve(), Path(traces).expanduser().resolve()
    output = new_output(output)
    used = []
    for trace in sorted(p for p in traces.iterdir() if p.is_dir()):
        name = trace.name.split("__")[0]
        source = tasks / name
        if not (source / "task.toml").is_file() or reward(trace) != 0:
            continue                                         # unknown task, or the initial attempt did not fail
        instruction = (source / "instruction.md").read_text(encoding="utf-8")
        context = pipeline.extract_messages(trace) if message_mode != "none" else None
        recovery = pipeline.build_recovery_instruction(instruction, context) if context else instruction
        script = "#!/bin/bash\n" + "".join(f"timeout {int(t)} bash -lc {shlex.quote(c)} || true\n" for c, t in commands(replay, trace))
        directory = write_task(output, f"recovery-{name}", instruction=recovery, config={},
                               environment={"replay.sh": script}, tests={"test.sh": source / "tests" / "test.sh"})
        shutil.rmtree(directory / "tests"); shutil.copytree(source / "tests", directory / "tests")
        for path in sorted((source / "environment").rglob("*")):
            if path.is_file():
                target = directory / "environment" / path.relative_to(source / "environment")
                target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(path, target)
        dockerfile = directory / "environment" / "Dockerfile"
        if not dockerfile.is_file():
            raise ValueError("Recovery-Bench conversion needs the task's environment/Dockerfile: " + name)
        dockerfile.write_text(dockerfile.read_text(encoding="utf-8").rstrip() +
                              "\n# Recovery-Bench: rebuild the failed attempt's state\nCOPY replay.sh /tmp/recovery-replay.sh\n"
                              "RUN bash /tmp/recovery-replay.sh && rm /tmp/recovery-replay.sh\n", encoding="utf-8")
        config = toml.load(source / "task.toml")
        config.setdefault("task", {})["name"] = directory.name     # the task directory is the recovery variant
        (directory / "task.toml").write_text(toml.dumps(config), encoding="utf-8")
        used.append(name)
    if not used:
        raise ValueError("no failed initial traces matched the Terminal-Bench tasks")
    write_manifest(output, "recovery-bench", revision, source="letta-ai/recovery-bench", message_mode=message_mode,
                   tasks=used, converter="ctxpress.benchmarks.convert.recovery",
                   protocol_note="the failed attempt's commands are replayed at image build")
    return output
