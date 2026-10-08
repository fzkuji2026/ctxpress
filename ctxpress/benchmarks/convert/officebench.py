"""OfficeBench tasks as Harbor tasks, graded by OfficeBench's own evaluation functions.

Source: a checkout of zlwang-cs/OfficeBench. Each item is tasks/<task_id>/subtasks/<subtask_id>.json with `task`
(the instruction) and `evaluation` (a list of {function, args}); its starting files are tasks/<task_id>/testbed/ and
the office applications are the scripts under apps/. The verifier calls the official functions on the final
testbed exactly as evaluation.py does: the item passes only if every function returns true. One change of
interface: the original agent emits OfficeBench actions; here the agent runs the same app scripts from a shell.
"""
from __future__ import annotations
import json, shutil
from pathlib import Path

from ctxpress.benchmarks.convert.common import new_output, write_manifest, write_task

INSTRUCTION = """{task}

Working files are in /testbed. The office applications (documents, spreadsheets, calendar, email and others) are
available as the official OfficeBench application scripts under /apps; run them from the shell to read and change
files. Leave your results in /testbed.
"""

VERIFIER = """import json, sys, traceback
from pathlib import Path
sys.path.insert(0, "/tests/officebench")
from evaluation import *          # the official evaluate_* functions, as evaluation.py imports them

out = Path("/logs/verifier"); out.mkdir(parents=True, exist_ok=True)
config = json.loads(Path("/tests/subtask.json").read_text())
results = []
passed = True
for item in config["evaluation"]:
    try:
        ok = bool(eval(f"{item['function']}(output_dir, args)", globals(), dict(output_dir="/testbed", args=item["args"])))
    except Exception:
        ok = False
        results.append(dict(function=item["function"], error=traceback.format_exc(limit=3)))
    results.append(dict(function=item["function"], passed=ok))
    passed = passed and ok
(out / "reward.txt").write_text("1\\n" if passed else "0\\n")
(out / "officebench-checks.json").write_text(json.dumps(results))
"""


def items(source):
    root = Path(source).expanduser().resolve()
    found = []
    for config in sorted((root / "tasks").glob("*/subtasks/*.json")):
        data = json.loads(config.read_text(encoding="utf-8"))
        if not isinstance(data.get("task"), str) or not isinstance(data.get("evaluation"), list) or not data["evaluation"]:
            raise ValueError("not an OfficeBench subtask: " + str(config))
        for item in data["evaluation"]:
            if not isinstance(item, dict) or not isinstance(item.get("function"), str) or "args" not in item:
                raise ValueError("OfficeBench evaluation items need function and args: " + str(config))
        found.append((config.parent.parent.name, config.stem, config, data))
    if not found:
        raise ValueError("no OfficeBench subtasks under " + str(root / "tasks"))
    return root, found


def files(folder, prefix):
    return {f"{prefix}/{path.relative_to(folder).as_posix()}": path for path in sorted(folder.rglob("*")) if path.is_file()}


def convert(source, output, *, image, revision, agent_timeout=1800):
    root, found = items(source)
    evaluation = root / "evaluation.py"
    if not evaluation.is_file() or not (root / "apps").is_dir():
        raise ValueError("OfficeBench checkout needs evaluation.py and apps/")
    support = {}
    for folder in ("utils",):
        if (root / folder).is_dir():
            support.update(files(root / folder, f"officebench/{folder}"))
    output = new_output(output)
    for task, subtask, config, data in found:
        testbed = root / "tasks" / task / "testbed"
        environment = {"Dockerfile": f"FROM {image}\nCOPY apps /apps\nCOPY testbed /testbed\n", **files(root / "apps", "apps")}
        if testbed.is_dir():
            environment.update(files(testbed, "testbed"))
        write_task(output, f"officebench-{task}-{subtask}", instruction=INSTRUCTION.format(task=data["task"].strip()),
                   config=dict(agent=dict(timeout_sec=agent_timeout), verifier=dict(timeout_sec=300), environment={}),
                   environment=environment,
                   tests={"test.sh": "#!/bin/bash\npython3 /tests/officebench_verify.py\n", "officebench_verify.py": VERIFIER,
                          "subtask.json": config, "officebench/evaluation.py": evaluation, **support})
    write_manifest(output, "officebench", revision, source="zlwang-cs/OfficeBench", tasks=len(found),
                   converter="ctxpress.benchmarks.convert.officebench",
                   protocol_note="agent runs the official app scripts from a shell instead of emitting OfficeBench actions")
    return output
