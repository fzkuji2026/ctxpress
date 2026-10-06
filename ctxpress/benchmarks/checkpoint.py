"""Compatibility planner for recorded-boundary benchmark tasks.

Coordinate and catalog rules belong to this adapter, not the common scheduler.
"""
from __future__ import annotations
import copy, hashlib, math, os
from pathlib import Path
from ctxpress.methods import build
from ctxpress import benchmarks
from ctxpress.harness.jobs.plan import canonical, file_sha256, fingerprint, _positive
from ctxpress.harness.jobs import task as tasks


def compile_plan(config, base_dir=None):
    config = copy.deepcopy(config)
    if config.get("schema") != "ctxpress.eval" or config.get("version") != 1:
        raise ValueError("unsupported evaluation configuration")
    allowed = {"schema", "version", "name", "scope", "model", "reasoning", "backend", "benchmark", "start_mode", "environment", "boundaries", "tasks", "methods", "repeats", "workers", "run", "prices", "comparison"}
    if set(config) - allowed:
        raise ValueError("unknown evaluation fields: " + ", ".join(sorted(set(config) - allowed)))
    benchmark = benchmarks.get(config.get('benchmark', benchmarks.DEFAULT))
    config['benchmark'] = benchmark.describe()['name']
    if config.get('start_mode', 'checkpoint') != 'checkpoint':
        raise ValueError('checkpoint adapter requires start_mode=checkpoint')
    config['start_mode'] = 'checkpoint'
    if config.get("backend") not in benchmark.describe()['backends'] or config.get("scope") not in ("mechanism", "formal"):
        raise ValueError("choose a supported benchmark backend and an explicit mechanism/formal scope")
    if not isinstance(config.get("model"), str) or not config["model"].strip():
        raise ValueError("the model must be explicit")
    if not isinstance(config.get("reasoning", "low"), str) or not config.get("reasoning", "low").strip():
        raise ValueError("invalid reasoning setting")
    base_dir = Path(base_dir or os.getcwd()).resolve()
    env = config.get("environment") or {}
    if set(env) - {"scripts", "bindir", "e2e", "upstream", "via", "snapshot", "grading"}:
        raise ValueError("unknown evaluation environment setting")
    if not env.get("bindir"):
        raise ValueError("select the Codex binary directory explicitly; historical defaults are not the latest binary")
    if not env.get("scripts"):
        env["scripts"] = benchmark.default_scripts()
    for key in ("scripts", "bindir", "e2e", "snapshot", "grading"):
        if env.get(key):
            path = Path(env[key]).expanduser()
            env[key] = str((base_dir / path).resolve() if not path.is_absolute() else path.resolve())
    config["environment"] = env
    repeats = _positive(config.get("repeats", 1), "repeats")
    config["workers"] = _positive(config.get("workers", 1), "workers")
    settings = config.get("run") or {}
    if set(settings) - {"max_calls", "timeout", "compact_limit", "prompt", "submit", "installed"}:
        raise ValueError("unknown evaluation run setting")
    settings = {**dict(max_calls=100, timeout=1800, compact_limit=230000, prompt="Continue with the task.", submit=False, installed=True), **settings}
    for key in ("max_calls", "compact_limit"):
        _positive(settings[key], key)
    _positive(settings["timeout"], "timeout", integer=False)
    if type(settings["submit"]) is not bool or type(settings["installed"]) is not bool or not isinstance(settings["prompt"], str):
        raise ValueError("invalid run flags or prompt")
    if config["scope"] == "formal" and (not settings["submit"] or settings["compact_limit"] < 128000):
        raise ValueError("formal evaluation requires official grading and a native compact limit of at least 128k")
    config["run"] = settings
    if 'tasks' in config:
        if 'boundaries' in config:
            raise ValueError("choose tasks or explicit boundaries, not both")
        config['boundaries'] = benchmark.select_tasks(env['scripts'], config.pop('tasks'))
    points, methods = config.get("boundaries"), config.get("methods")
    if not isinstance(points, list) or not points or not isinstance(methods, list) or not methods:
        raise ValueError("provide boundaries and methods")
    point_ids, coordinates, method_ids, artifacts, jobs = set(), set(), set(), {}, []
    from ctxpress.harness.runtime import codex_binary
    binary_artifacts, missing = codex_binary.capture(env['bindir'])
    artifacts.update(binary_artifacts)
    expected = benchmark.required_files(env)
    for path in expected:
        if path.is_file():
            artifacts[str(path)] = file_sha256(path)
        else:
            missing.append(str(path))
    for point in points:
        if set(point) - {"id", "n", "j", "context_tokens"} or not isinstance(point.get("id"), str) or not point["id"]:
            raise ValueError("invalid boundary declaration")
        if point["id"] in point_ids or type(point.get("n")) is not int or point["n"] < 0:
            raise ValueError("invalid or duplicate boundary")
        point_ids.add(point["id"])
        _positive(point.get("j"), "boundary j")
        coordinate = point["n"], point["j"]
        if coordinate in coordinates:
            raise ValueError("duplicate boundary coordinates")
        coordinates.add(coordinate)
        _positive(point.get("context_tokens"), "declared boundary context tokens")
        if config["scope"] == "formal" and point["context_tokens"] < 128000:
            raise ValueError("formal boundaries require at least 128k declared context tokens")
    environment_snapshot = None
    if env.get('snapshot'):
        from ctxpress.harness.jobs import environment as eval_environment
        environment_snapshot, manifest_digest = eval_environment.read(env['snapshot'], coordinates)
        artifacts[env['snapshot']] = manifest_digest
        tree = environment_snapshot['workspace']
        workspace = Path(tree['root'])
        for key in ('scripts','bindir','snapshot'):
            path = Path(env[key])
            if path == workspace or path in workspace.parents or workspace in path.parents:
                raise ValueError('workspace snapshot must be separate from scripts, binary and manifest paths')
        # Bind every declared workspace file without importing benchmark helpers.
        eval_environment.verify_workspace(environment_snapshot, tree['root'])
        for relative, digest in tree['files'].items():
            artifacts[str(Path(tree['root']) / relative)] = digest
    grading_snapshot = None
    if env.get('grading'):
        from ctxpress.harness.jobs import grading_inputs
        grading_snapshot, manifest_digest = grading_inputs.read(env['grading'], coordinates)
        artifacts[env['grading']] = manifest_digest
        for descriptor in grading_snapshot['trees'].values():
            for relative,digest in descriptor['files'].items():
                source = Path(descriptor['root'])/relative
                if file_sha256(source) != digest:
                    raise ValueError('official grading input changed; capture a new manifest')
                artifacts[str(source)] = digest
    task_inputs = {}
    catalog = Path(env['scripts']) / 'valid_points.json'
    if catalog.is_file():
        try:
            records = benchmark.boundaries(env['scripts'])
            for point in points:
                row = records.get((point['n'],point['j']))
                if row is None:
                    missing.append(str(catalog) + f"#boundary-{point['n']}-{point['j']}")
                    continue
                source = Path(row['src']).expanduser()
                source = source.resolve() if source.is_absolute() else (catalog.parent / source).resolve()
                if source.suffix.lower() != '.jsonl':
                    raise ValueError('boundary sources must be recorded JSONL histories')
                if source.is_file():
                    artifacts[str(source)] = file_sha256(source)
                    task_inputs[point['id']] = [dict(role='task', path=str(source), sha256=artifacts[str(source)])]
                else:
                    missing.append(str(source))
        except (ValueError, KeyError, TypeError):
            missing.append(str(catalog) + '#valid-boundary-catalog')
    def profiles(entry):
        args = entry.get("args") or {}
        key = {"CostModel": "profile", "AutoCostModel": "policy"}.get(entry.get("class"))
        if key and args.get(key) and not isinstance(args[key], dict):
            path = Path(args[key]).expanduser()
            path = (base_dir / path).resolve() if not path.is_absolute() else path.resolve()
            args[key] = str(path)
            artifacts[str(path)] = file_sha256(path)
        if isinstance(args.get("inner"), dict):
            profiles(args["inner"])
        for method in args.get("methods", []):
            profiles(method)
    for index, entry in enumerate(methods):
        if not isinstance(entry, dict) or set(entry) - {"class", "args", "label"}:
            raise ValueError("invalid method entry")
        label = entry.get("label") or entry.get("class")
        if not isinstance(label, str) or not label or label in method_ids:
            raise ValueError("method labels must be unique")
        method_ids.add(label)
        entry.setdefault("args", {})
        profiles(entry)
        method = build(entry)
        method.validate_live()
        limit = method.codex_config.get("model_auto_compact_token_limit", settings["compact_limit"])
        _positive(limit, "effective native compact limit")
        if config["scope"] == "formal" and limit < 128000:
            raise ValueError("formal native compaction thresholds must be at least 128k")
        for point in points:
            for repeat in range(repeats):
                job_id = f"m{index:03d}-b{points.index(point):03d}-r{repeat:03d}"
                jobs.append(dict(id=job_id, boundary=point,
                    task=tasks.checkpoint(point, config['benchmark'], task_inputs.get(point['id'], [])),
                    method=entry, label=label, repeat=repeat, compact_limit=limit))
    prices = config.get("prices")
    comparison = config.get('comparison')
    if comparison is not None:
        if (not isinstance(comparison,dict) or set(comparison)!={'reference','quality'} or
            not isinstance(comparison.get('reference'),str) or comparison['reference'] not in method_ids or
            comparison.get('quality')!='per_boundary_no_regression' or len(method_ids)<2):
            raise ValueError('comparison requires an existing reference label, another method and per_boundary_no_regression quality')
    from ctxpress.live import usage as eval_usage
    eval_usage.validate_prices(prices)
    result = dict(schema="ctxpress.eval.plan", version=1, input_snapshot_version=1, config=config, jobs=jobs, artifacts=artifacts,
        benchmark=benchmark.describe(),
        code_sha256=fingerprint(), context_evidence="declared; checked against boundary catalog at execution",
        run_count=len(jobs), max_parallel=min(config["workers"], len(jobs)),
        agent_timeout_seconds_upper_bound=len(jobs) * settings["timeout"],
        cost_evidence="user-supplied prices; no total bill estimate without a token estimate" if prices else "prices not supplied; report actual token usage only",
        missing_environment_files=missing)
    if environment_snapshot is not None:
        result['environment_snapshot'] = environment_snapshot
    if grading_snapshot is not None:
        result['grading_snapshot'] = grading_snapshot
    result["sha256"] = hashlib.sha256(canonical(result).encode()).hexdigest()
    return result
