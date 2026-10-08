"""Common task-start planner; task semantics and executors remain in adapters."""
from __future__ import annotations
import copy, hashlib, os
from pathlib import Path
from ctxpress import benchmarks
from ctxpress.methods import build
from ctxpress.harness.jobs import plan as eval_plan, trees as eval_trees, resources as task_resources


def compile_plan(config, base_dir=None):
    config = copy.deepcopy(config)
    allowed = {'schema', 'version', 'name', 'scope', 'model', 'reasoning', 'backend', 'benchmark',
               'start_mode', 'environment', 'tasks', 'methods', 'repeats', 'workers', 'run', 'prices'}
    if (config.get('schema') != 'ctxpress.eval' or type(config.get('version')) is not int or config['version'] != 1 or
            set(config) - allowed):
        raise ValueError('unsupported task-start evaluation configuration')
    if config.get('start_mode') != 'task_start':
        raise ValueError('this benchmark requires start_mode=task_start')
    # A task beginning with a short issue is not a measured >=128k checkpoint.
    if config.get('scope') not in ('mechanism', 'benchmark'):
        raise ValueError('task-start plans require mechanism or benchmark scope; formal long-boundary scope does not apply')
    adapter = benchmarks.get(config.get('benchmark', benchmarks.DEFAULT))
    description = adapter.task_start_description()
    config['benchmark'] = description['name']
    if config.get('backend') not in description['backends']:
        raise ValueError('choose a supported benchmark backend')
    for key in ('model', 'reasoning'):
        if key == 'reasoning':
            config.setdefault(key, 'low')
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(key + ' must be explicit')
    base_dir = Path(base_dir or os.getcwd()).resolve()
    from ctxpress.live import usage as eval_usage
    eval_usage.validate_prices(config.get('prices'))
    environment = config.get('environment')
    if not isinstance(environment, dict) or set(environment) - {'data', 'bindir', 'resources', 'upstream', 'via', 'model_catalog'}:
        raise ValueError('invalid task-start environment')
    for key in ('data', 'bindir'):
        if not isinstance(environment.get(key), str) or not environment[key]:
            raise ValueError('select the local ' + key + ' explicitly')
    for key in ('data', 'bindir', 'resources'):
        if environment.get(key):
            environment[key] = str((base_dir / Path(environment[key]).expanduser()).resolve())
    model_catalog = None
    if 'model_catalog' in environment:
        from ctxpress.harness.runtime import codex_catalog
        if not isinstance(environment['model_catalog'], str) or not environment['model_catalog']:
            raise ValueError('model_catalog must be an explicit local file')
        model_catalog = codex_catalog.inspect(base_dir / Path(environment['model_catalog']).expanduser(),
                                              config['model'], config['reasoning'])
        environment['model_catalog'] = model_catalog['path']
    identifiers = config.get('tasks')
    if (not isinstance(identifiers, list) or not identifiers or any(not isinstance(value, str) or not value for value in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise ValueError('provide a nonempty list of unique task IDs')
    found = adapter.task_instances(environment['data'])
    catalog = {task['id']:task for task in found}
    if len(catalog) != len(found):
        raise ValueError('benchmark catalog has duplicate task IDs')
    unknown = set(identifiers) - set(catalog)
    if unknown:
        raise ValueError('unknown benchmark tasks: ' + ', '.join(sorted(unknown)))
    selected = [catalog[key] for key in identifiers]
    repeats = eval_plan._positive(config.get('repeats', 1), 'repeats')
    config['repeats'] = repeats
    config['workers'] = eval_plan._positive(config.get('workers', 1), 'workers')
    settings = config.get('run') or {}
    run_fields={'max_calls','timeout','compact_limit','grade','grading_timeout'}
    if hasattr(adapter,'run_fields'):run_fields|=adapter.run_fields()
    if not isinstance(settings, dict) or set(settings) - run_fields:
        raise ValueError('invalid task-start run settings')
    settings = dict(max_calls=100, timeout=1800, compact_limit=230000, grade=True) | settings
    eval_plan._positive(settings['max_calls'], 'max_calls')
    if settings['compact_limit'] is not None:           # None: every method keeps the host's default threshold
        eval_plan._positive(settings['compact_limit'], 'compact_limit')
    eval_plan._positive(settings['timeout'], 'timeout', integer=False)
    if 'grading_timeout' in settings:
        if (config['benchmark'] not in ('swe-bench','swe-bench-lite','swe-bench-verified','swe-milestone','swe-polybench','swe-bench-pro','bigcodebench') or
                config['benchmark']=='swe-bench-pro' and any(task['initial_state'].get('pro_version')!='v1' for task in selected)):
            raise ValueError('grading_timeout override applies to SWE-bench, PolyBench, Pro V1, BigCodeBench or native SWE-Milestone')
        eval_plan._positive(settings['grading_timeout'], 'grading_timeout', integer=False)
    if type(settings['grade']) is not bool or config['scope'] == 'benchmark' and not settings['grade']:
        raise ValueError('benchmark scope requires official grading')
    if hasattr(adapter,'normalize_run'):settings=adapter.normalize_run(settings,repeats)
    config['run'] = settings
    artifacts, trees, missing = {}, {}, []
    if model_catalog is not None:
        artifacts[model_catalog['path']] = model_catalog['sha256']
    data = Path(environment['data'])
    data_root = data if data.is_dir() else data.parent
    # The copied data root replaces the original root while retaining relative paths.
    environment['data'] = str(data_root)
    sources = {item['path'] for task in selected for item in task['inputs']}
    extra = adapter.extra_input_trees(selected, data_root) if hasattr(adapter, 'extra_input_trees') else {}
    extra_sources = {str(Path(descriptor['tree']['root']) / name) for descriptor in extra.values() for name in descriptor['tree']['files']}
    if not extra_sources <= sources:raise ValueError('extra task input trees must bind selected task files only')
    trees.update(extra)
    names = []
    for source in sources:
        if source in extra_sources:continue
        try:
            names.append(Path(source).relative_to(data_root).as_posix())
        except ValueError:
            raise ValueError('task input escapes the declared dataset root') from None
    directories = adapter.data_directories(selected, data_root) if hasattr(adapter, 'data_directories') else []
    trees['data'] = eval_trees.capture(data_root, sorted(names), folder='task-data', environment_key='data', directories=directories)
    for task in selected:
        for item in task['inputs']:
            if eval_plan.file_sha256(item['path']) != item['sha256']:
                raise ValueError('task data changed while compiling the plan')
            artifacts[item['path']] = item['sha256']
    lock = None
    if environment.get('resources'):
        lock, digest = task_resources.read(environment['resources'], config['benchmark'], selected)
        artifacts[environment['resources']] = digest
        for key, tree in lock['trees'].items():
            trees['official:' + key] = dict(tree=copy.deepcopy(tree), folder='official-inputs/' + key, environment_key=None)
            for name, expected in tree['files'].items():
                source = str(Path(tree['root']) / name)
                if eval_plan.file_sha256(source) != expected:
                    raise ValueError('declared official input changed')
                artifacts[source] = expected
    else:
        missing.append('task resource manifest: pin official inputs, benchmark release and agent/grading images')
    from ctxpress.harness.runtime import codex_binary
    binary_artifacts, binary_missing = codex_binary.capture(environment['bindir'])
    artifacts.update(binary_artifacts); missing.extend(binary_missing)
    methods = config.get('methods')
    if not isinstance(methods, list) or not methods:
        raise ValueError('provide context methods')
    labels, jobs = set(), []
    def profiles(entry):
        args = entry.get('args') or {}
        field = {'CostModel':'profile', 'AutoCostModel':'policy'}.get(entry.get('class'))
        if field and args.get(field) and not isinstance(args[field], dict):
            path = (base_dir / Path(args[field]).expanduser()).resolve()
            args[field] = str(path); artifacts[str(path)] = eval_plan.file_sha256(path)
        if isinstance(args.get('inner'), dict):
            profiles(args['inner'])
        for child in args.get('methods', []):
            profiles(child)
    for index, entry in enumerate(methods):
        if not isinstance(entry, dict) or set(entry) - {'class', 'args', 'label'}:
            raise ValueError('invalid method entry')
        entry.setdefault('args', {})
        label = entry.get('label') or entry.get('class')
        if not isinstance(label, str) or not label or label in labels:
            raise ValueError('method labels must be unique')
        labels.add(label); profiles(entry)
        method = build(entry); method.validate_live()
        # HostDefault leaves the host's own threshold in place: no override is passed (limit None).
        limit = None if getattr(method, 'host_compaction', None) == 'default' else \
            method.codex_config.get('model_auto_compact_token_limit', settings['compact_limit'])
        if limit is not None:
            eval_plan._positive(limit, 'native compact limit')
        for number, task in enumerate(selected):
            for repeat in range(repeats):
                job = dict(id=f'm{index:03d}-t{number:03d}-r{repeat:03d}', task=task,
                           method=entry, label=label, repeat=repeat, compact_limit=limit)
                if lock is not None:
                    job['resources'] = copy.deepcopy(lock['tasks'][task['id']])
                jobs.append(job)
    if not description['execution_supported']:
        missing.append(config['benchmark'] + ': task-start executor integration pending')
    if hasattr(adapter, 'plan_requirements'):
        missing.extend(adapter.plan_requirements(config, selected, lock))
    plan = dict(schema='ctxpress.eval.plan', version=1, input_snapshot_version=1,
                config=config, jobs=jobs, artifacts=artifacts, input_trees=trees,
                benchmark=description, code_sha256=eval_plan.fingerprint(),
                context_evidence='fresh task; initial context is not a recorded long-boundary measurement',
                run_count=len(jobs), max_parallel=min(config['workers'], len(jobs)),
                agent_timeout_seconds_upper_bound=len(jobs) * settings['timeout'],
                cost_evidence='report actual observed token usage; no total bill estimate',
                missing_environment_files=missing)
    if lock is not None:
        plan['task_resources'] = lock
    if model_catalog is not None:
        plan['codex_model_catalog'] = model_catalog
    eval_trees.verify(plan)
    plan['sha256'] = hashlib.sha256(eval_plan.canonical(plan).encode()).hexdigest()
    return plan
