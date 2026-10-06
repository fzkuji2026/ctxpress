"""Reviewable experiment plans. Compiling a plan never starts Docker or a model."""
from __future__ import annotations
import hashlib, json, math
from pathlib import Path
from ctxpress import benchmarks


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def file_sha256(path):
    if Path(path).resolve().name.lower() == "auth.json":
        raise ValueError("credential files cannot be evaluation inputs")
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def _positive(value, name, integer=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or (integer and type(value) is not int):
        raise ValueError(f"{name} must be a positive {'integer' if integer else 'number'}")
    return value


def compile_plan(config, base_dir=None):
    """Delegate task preparation to the benchmark without imposing coordinates."""
    if not isinstance(config, dict):
        raise ValueError("evaluation configuration must be an object")
    benchmark = benchmarks.get(config.get('benchmark', benchmarks.DEFAULT))
    return benchmark.compile_plan(config, base_dir)


def verify(plan, check_inputs=True, code_root=None, input_root=None):
    if plan.get("schema") != "ctxpress.eval.plan" or plan.get("version") != 1:
        raise ValueError("unsupported evaluation plan")
    snapshot_version = plan.get("input_snapshot_version")
    if snapshot_version is not None and (type(snapshot_version) is not int or snapshot_version != 1):
        raise ValueError("unsupported evaluation input snapshot version")
    content = {key: value for key, value in plan.items() if key != "sha256"}
    if hashlib.sha256(canonical(content).encode()).hexdigest() != plan.get("sha256"):
        raise ValueError("evaluation plan has changed")
    if (plan.get('environment_snapshot') or plan.get('grading_snapshot')) and snapshot_version != 1:
        raise ValueError('environment and grading snapshots require frozen evaluation inputs')
    benchmark = benchmarks.get(plan['config'].get('benchmark', benchmarks.DEFAULT))
    benchmark.verify_bindings(plan)
    if plan.get('input_trees'):
        from ctxpress.harness.jobs import trees as eval_trees
        if snapshot_version != 1:
            raise ValueError('input trees require frozen evaluation inputs')
        eval_trees.verify(plan)
    from ctxpress.harness.jobs import task as tasks
    for job in plan['jobs']:
        task = tasks.from_job(job, plan['config'].get('benchmark', benchmarks.DEFAULT))
        if task['start_mode'] != plan['config'].get('start_mode', 'checkpoint'):
            raise ValueError('task start mode differs from plan')
        for item in task['inputs']:
            if plan['artifacts'].get(item['path']) != item['sha256']:
                raise ValueError('task input is not bound to the plan artifacts')
    if check_inputs:
        if plan["code_sha256"] != fingerprint(code_root):
            raise ValueError("framework code changed; compile a new plan")
        if input_root is not None:
            from ctxpress.harness.jobs import inputs as eval_inputs
            eval_inputs.verify(plan, input_root)
        else:
            for filename, expected in plan['artifacts'].items():
                if file_sha256(filename) != expected:
                    raise ValueError('evaluation input changed; compile a new plan')
    return plan


def load(path, check_inputs=True, code_root=None, input_root=None):
    with open(path, encoding="utf-8") as stream:
        return verify(json.load(stream), check_inputs, code_root, input_root)
