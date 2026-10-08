"""Recovery-Bench as Harbor tasks: a Terminal-Bench 2.0 task resumed from a failed attempt.

Sources, all on disk: the Terminal-Bench 2.0 Harbor tasks, a recovery-bench checkout (letta-ai/recovery-bench, run
with its dependencies installed) and its initial traces (runs/initial-...). Every benchmark-specific step is the
official code, as `recovery_bench.generate_traces --recovery-agent recovery-codex` runs it:
  - task selection: utils.get_unsolved_tasks (reward 0) and pipeline.filter_oversized_tasks (64 KB instruction cap);
  - the failed agent's commands: replay.extract_commands, minus replay._find_interrupted_commands;
  - the instruction: prompts.build_recovery_instruction over prompts.format_messages_as_text (message mode full),
    or with no context (none). Summary mode asks the recovery model for a summary at run time and is not offered.
The commands go to recovery/replay.json, which Harbor does not copy into the container. As in the official
RecoveryCodex.setup, ctxpress executes them one by one in the running container after the agent is installed and
before it starts (bash -lc, 15 s each, failures ignored); the agent setup timeout is tripled as the official
pipeline does. Grading is the original task's tests.
"""
from __future__ import annotations
import importlib, json, shutil, sys
from pathlib import Path

from ctxpress.benchmarks.convert.common import new_output, write_manifest
from ctxpress.core import toml

REPLAY = "recovery/replay.json"
REPLAY_TIMEOUT = 15            # recovery_bench.replay.replay_via_exec default
SETUP_MULTIPLIER = 3.0         # recovery_bench.pipeline.RECOVERY_SETUP_TIMEOUT_MULTIPLIER


def official(checkout):
    sys.path.insert(0, str(Path(checkout).expanduser().resolve()))
    try:
        return {name: importlib.import_module("recovery_bench." + name) for name in ("replay", "prompts", "utils", "pipeline")}
    finally:
        sys.path.pop(0)


def replay_commands(replay, folder):
    commands = replay.extract_commands(folder)
    skipped = replay._find_interrupted_commands(commands)
    return [c.command for i, c in enumerate(commands) if c.command and i not in skipped]


def convert(tasks, traces, checkout, output, *, revision, message_mode="full"):
    if message_mode not in ("full", "none"):
        raise ValueError("message_mode is full or none (summary needs the recovery model at run time)")
    code = official(checkout)
    tasks, traces = Path(tasks).expanduser().resolve(), Path(traces).expanduser().resolve()
    candidates = code["pipeline"].filter_oversized_tasks(code["utils"].get_unsolved_tasks(str(traces)), str(traces))
    output = new_output(output)
    used = []
    for name in candidates:
        short = name.split("/")[-1]
        source = tasks / short
        folder = code["utils"].find_trajectory_by_name(name, str(traces))
        if not (source / "task.toml").is_file() or folder is None:
            continue
        messages = code["replay"].extract_messages(folder) if message_mode == "full" else []
        context = code["prompts"].format_messages_as_text(messages) if messages else None
        instruction = code["prompts"].build_recovery_instruction((source / "instruction.md").read_text(encoding="utf-8"), context)
        directory = output / ("recovery-" + short)
        shutil.copytree(source, directory)
        (directory / "instruction.md").write_text(instruction, encoding="utf-8")
        (directory / "recovery").mkdir()
        (directory / REPLAY).write_text(json.dumps(dict(schema="ctxpress.recovery_replay", version=1,
            timeout_sec=REPLAY_TIMEOUT, setup_timeout_multiplier=SETUP_MULTIPLIER,
            trace=folder.name, commands=replay_commands(code["replay"], folder)), indent=1), encoding="utf-8")
        config = toml.load(source / "task.toml")
        config.setdefault("task", {})["name"] = directory.name     # the task directory is the recovery variant
        (directory / "task.toml").write_text(toml.dumps(config), encoding="utf-8")
        used.append(short)
    if not used:
        raise ValueError("no failed initial traces matched the Terminal-Bench tasks")
    write_manifest(output, "recovery-bench", revision, source="letta-ai/recovery-bench", message_mode=message_mode,
                   tasks=used, converter="ctxpress.benchmarks.convert.recovery",
                   protocol_note="official RecoveryCodex flow: replay in the running container before the agent starts")
    return output

