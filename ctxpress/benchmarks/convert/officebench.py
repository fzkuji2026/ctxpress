"""OfficeBench tasks as Harbor tasks, graded by OfficeBench's own evaluation functions.

Source: a checkout of zlwang-cs/OfficeBench. Each item is tasks/<task_id>/subtasks/<subtask_id>.json with `task`
(the instruction) and `evaluation` (a list of {function, args}); its starting files are tasks/<task_id>/testbed/ and
the office applications are the scripts under apps/. The container layout is the official one (/apps, /testbed).
The original agent emits JSON actions that OfficeBench turns into `python3 /apps/<app>/<script>.py --arg ...` in
that container; here the agent runs the same scripts from its shell. The verifier imports utils/evaluate.py, as
evaluation.py does, and the item passes only if every function returns true on the final /testbed.

ACON's setting (2510.00615): pass `tasks` = ACON's refined experiments/officebench/tasks, `task_list` = its
test_tasks.txt (text-only tasks, its 1:1 split) and `tasks_source` = microsoft/acon@<commit>.
"""
from __future__ import annotations
import json
from pathlib import Path

from ctxpress.benchmarks.convert.common import new_output, write_manifest, write_task

INSTRUCTION = """{task}

Working files are in /testbed. The office applications are the OfficeBench application scripts under /apps:
{apps}
Run them from the shell (for example `python3 /apps/excel_app/excel_read_file.py --file_path /testbed/data/x.xlsx`;
`--help` lists a script's arguments). Leave your results in /testbed.
"""

VERIFIER = """import json, os, sys, traceback
from pathlib import Path
sys.path.insert(0, "/tests/officebench")
os.chdir("/")
from utils.evaluate import *          # the official evaluate_* functions, as evaluation.py imports them

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


def items(tasks, selected=None):
    found = []
    for config in sorted(Path(tasks).glob("*/subtasks/*.json")):
        task = config.parent.parent.name
        if selected is not None and task not in selected:
            continue
        data = json.loads(config.read_text(encoding="utf-8"))
        if not isinstance(data.get("task"), str) or not isinstance(data.get("evaluation"), list) or not data["evaluation"]:
            raise ValueError("not an OfficeBench subtask: " + str(config))
        for item in data["evaluation"]:
            if not isinstance(item, dict) or not isinstance(item.get("function"), str) or "args" not in item:
                raise ValueError("OfficeBench evaluation items need function and args: " + str(config))
        found.append((task, config.stem, config, data))
    missing = sorted(selected - {t for t, *_ in found}) if selected is not None else []
    if missing:
        raise ValueError("listed OfficeBench tasks not found: " + ", ".join(missing))
    if not found:
        raise ValueError("no OfficeBench subtasks under " + str(tasks))
    return found


def files(folder, prefix):
    return {f"{prefix}/{path.relative_to(folder).as_posix()}": path for path in sorted(folder.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts}


def convert(source, output, *, image, revision, tasks=None, task_list=None, tasks_source=None, agent_timeout=1800):
    """`tasks_source` names where `tasks` came from (for example microsoft/acon@<commit>); required with `tasks`."""
    root = Path(source).expanduser().resolve()
    if not (root / "utils" / "evaluate.py").is_file() or not (root / "apps").is_dir():
        raise ValueError("OfficeBench checkout needs utils/evaluate.py and apps/")
    if tasks and not tasks_source:
        raise ValueError("declare where the task directory comes from (tasks_source)")
    task_root = Path(tasks).expanduser().resolve() if tasks else root / "tasks"
    selected = None
    if task_list:
        selected = {line.strip() for line in Path(task_list).read_text(encoding="utf-8").splitlines() if line.strip()}
    found = items(task_root, selected)
    apps, support = files(root / "apps", "apps"), files(root / "utils", "officebench/utils")
    support.update({"officebench/" + k: v for k, v in apps.items()})       # utils/evaluate.py imports apps.*
    scripts = sorted(p for p in apps if p.endswith(".py") and not p.endswith("__init__.py"))
    listing = "\n".join("  /" + p for p in scripts)
    output = new_output(output)
    for task, subtask, config, data in found:
        testbed = task_root / task / "testbed"
        environment = {"Dockerfile": f"FROM {image}\nCOPY apps /apps\nCOPY testbed /testbed\n", **apps}
        environment.update(files(testbed, "testbed") if testbed.is_dir() else {"testbed/.keep": ""})
        write_task(output, f"officebench-{task}-{subtask}", instruction=INSTRUCTION.format(task=data["task"].strip(), apps=listing),
                   config=dict(agent=dict(timeout_sec=agent_timeout), verifier=dict(timeout_sec=300), environment={}),
                   environment=environment,
                   tests={"test.sh": "#!/bin/bash\npython3 /tests/officebench_verify.py\n", "officebench_verify.py": VERIFIER,
                          "subtask.json": config, **support})
    write_manifest(output, "officebench", revision, source="zlwang-cs/OfficeBench", tasks=len(found),
                   task_source=tasks_source or "zlwang-cs/OfficeBench", task_list=sorted(selected) if selected else None,
                   converter="ctxpress.benchmarks.convert.officebench",
                   protocol_note="agent runs the official app scripts from a shell; the original agent's JSON actions call the same scripts")
    return output
