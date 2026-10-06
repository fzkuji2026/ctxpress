"""Generate task-start configurations from a fixed local experiment protocol."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from ctxpress import benchmarks
from ctxpress.core import policy as policies
from ctxpress.harness.jobs import plan as eval_plan, resources as task_resources
from ctxpress.live import usage as eval_usage


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + ' must be explicit')
    return value


def _ids(value, name, *, empty=False):
    if (not isinstance(value, list) or not empty and not value or
            any(not isinstance(item, str) or not item.strip() for item in value) or
            len(set(value)) != len(value)):
        raise ValueError(name + ' requires unique explicit IDs')
    return value


def _read(path, bindings):
    path = Path(path).expanduser().resolve()
    digest = eval_plan.file_sha256(path)
    value = json.loads(path.read_text(encoding='utf-8'))
    if eval_plan.file_sha256(path) != digest:
        raise ValueError('protocol input changed while reading: ' + str(path))
    bindings[str(path)] = digest
    return value


def _protocol(value):
    if (not isinstance(value, dict) or value.get('schema') != 'ctxpress.experiment.protocol' or
            type(value.get('version')) is not int or value['version'] != 1 or
            type(value.get('family_count')) is not int or value['family_count'] != len(benchmarks.FAMILIES) or
            value.get('start_mode') != 'task_start' or value.get('scope') != 'benchmark'):
        raise ValueError('require a task-start protocol for the fixed eight benchmark families')
    families = value.get('families')
    if (not isinstance(families, list) or len(families) != len(benchmarks.FAMILIES) or
            any(not isinstance(row, dict) or row.get('family') not in benchmarks.FAMILIES for row in families) or
            {row['family'] for row in families} != set(benchmarks.FAMILIES)):
        raise ValueError('protocol must declare each of the eight families exactly once')
    selection = value.get('selection')
    if not isinstance(selection, dict):
        raise ValueError('protocol must declare task selection')
    if type(selection.get('seed')) is not int or selection['seed'] < 0:
        raise ValueError('selection seed must be a nonnegative integer')
    for key in ('pilot_tasks_per_family', 'comparison_tasks_per_family'):
        eval_plan._positive(selection.get(key), key)
    excluded = set(_ids(selection.get('exclude_task_ids'), 'training exclusions', empty=True))
    for row in families:
        if row.get('benchmark') not in benchmarks.FAMILIES[row['family']]:
            raise ValueError('benchmark does not belong to its declared family')
        if 'comparison_repeats' in row:
            eval_plan._positive(row['comparison_repeats'], row['family'] + '.comparison_repeats')
        for key in ('pilot_tasks', 'comparison_tasks'):
            if row.get(key) is not None:
                identifiers = _ids(row[key], row['family'] + '.' + key)
                if set(identifiers) & excluded:
                    raise ValueError('evaluation task IDs overlap excluded training tasks')
                expected = selection['pilot_tasks_per_family'] if key == 'pilot_tasks' else row.get(
                    'comparison_task_count', selection['comparison_tasks_per_family'])
                eval_plan._positive(expected, row['family'] + ' task count')
                if len(identifiers) != expected:
                    raise ValueError('frozen task count differs from the protocol: ' + row['family'])
        if row.get('pilot_tasks') is not None and row.get('comparison_tasks') is not None:
            if not set(row['pilot_tasks']) <= set(row['comparison_tasks']):
                raise ValueError('pilot task IDs must belong to the comparison cohort')
    return value


def _candidate_canonical(value):
    """JSON equality with numeric spelling normalized, but bool distinct from int."""
    def normalize(item):
        if isinstance(item, float):
            policies.numbers(item)
            return int(item) if item.is_integer() else item
        if isinstance(item, dict):
            return {key: normalize(child) for key, child in item.items()}
        if isinstance(item, list):
            return [normalize(child) for child in item]
        return item
    return policies.canonical(normalize(value))


def _candidate_method_args(args):
    """Resolve the same defaults and harm aliases used by offline tuning."""
    import inspect
    from ctxpress.methods.cost_model import ALL_TYPES, CostModel
    from ctxpress.core import metrics

    reserved = {'lam', 'profile', 'traces_for_params', 'type_fn', 'item_model', 'cov_rates'}
    if not isinstance(args, dict) or set(args) & reserved:
        raise ValueError('candidate training args must declare only tunable method settings')
    try:
        bound = inspect.signature(CostModel).bind(**copy.deepcopy(args))
        bound.apply_defaults()
    except TypeError as error:
        raise ValueError('invalid candidate training args') from error
    result = {key: value for key, value in bound.arguments.items() if key not in reserved}
    if isinstance(result['harm'], str):
        if result['harm'] not in metrics.HARM:
            raise ValueError('invalid candidate training args.harm')
        result['harm'] = copy.deepcopy(metrics.HARM[result['harm']])
    # These constructor defaults are resolved during training, before the
    # statistics are frozen. Explicit defaults have the same meaning as omission.
    result['ops'] = result['ops'] or {key: ['placeholder'] for key in ALL_TYPES}
    if result['truncate_budget'] is None:
        result['truncate_budget'] = 2000
    return result


def _candidate_training(training, bundle):
    """Bind experiment declarations to an already internally validated policy.

    Params are DEFAULT plus the declared overrides and the reference window,
    as in tune; provenance notes are not parameter values. Return the effective
    compared fields without modifying either the declaration or frozen policy.
    """
    from ctxpress.core import calibration
    from ctxpress.core.params import DEFAULT
    from ctxpress.core import metrics

    fields = ('lambda_grid', 'constraint', 'reference_limit', 'args', 'params')
    for field in fields:
        if field not in training:
            raise ValueError('candidate training must declare ' + field)
    grid = training['lambda_grid']
    if not isinstance(grid, list) or not grid:
        raise ValueError('candidate training lambda_grid must be a nonempty numeric list')
    try:
        grid = sorted(set(calibration._number(value) for value in grid))
    except ValueError as error:
        raise ValueError('invalid candidate training lambda_grid') from error
    aliases = {alias: kind for kind in metrics.KINDS for alias in (kind, kind + ' simulated proxy')}
    constraint = training['constraint']
    if not isinstance(constraint, str) or constraint.strip() not in aliases:
        raise ValueError('candidate training constraint must name a supported simulated proxy')
    limit = eval_plan._positive(training['reference_limit'], 'candidate training reference_limit')
    declared_params = training['params']
    if not isinstance(declared_params, dict):
        raise ValueError('candidate training params must be explicit overrides')
    if 'window' in declared_params and _candidate_canonical(declared_params['window']) != _candidate_canonical(limit):
        raise ValueError('candidate training params.window conflicts with reference_limit')
    try:
        parameters = DEFAULT.override(declared_params).override(window=limit)
        parameters = policies.parameters(policies.parameter_rows(parameters))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError('invalid candidate training params') from error
    expected = dict(lambda_grid=grid, constraint=aliases[constraint.strip()], reference_limit=limit,
                    args=_candidate_method_args(training['args']),
                    params={name: value for name, value, *_ in parameters.table()})
    actual = dict(lambda_grid=[row['lam'] for row in bundle['screening']['candidates']],
                  constraint=bundle['screening']['kind'],
                  reference_limit=bundle['reference'].get('args', {}).get('t', 230000),
                  args=_candidate_method_args(bundle['method_args']),
                  params={row['name']: row['value'] for row in bundle['parameters']})
    for field in fields:
        if _candidate_canonical(actual[field]) != _candidate_canonical(expected[field]):
            raise ValueError('candidate policy differs from declared candidate_training.' + field)
    return json.loads(_candidate_canonical(expected))


def _candidate(protocol, artifact_root, bindings):
    comparison = protocol['comparison']
    candidate = comparison.get('required_candidate')
    if (not isinstance(candidate, dict) or candidate.get('class') != 'AutoCostModel' or
            not isinstance(candidate.get('args'), dict)):
        raise ValueError('comparison requires its frozen AutoCostModel candidate')
    entry = {key: copy.deepcopy(candidate[key]) for key in ('class', 'args', 'label') if key in candidate}
    path = (artifact_root / _text(entry['args'].get('policy'), 'candidate policy')).resolve()
    proof_path = (artifact_root / _text(candidate.get('provenance'), 'candidate provenance')).resolve()
    bundle = policies.validate(_read(path, bindings))
    proof = _read(proof_path, bindings)
    if (not isinstance(proof, dict) or proof.get('schema') != 'ctxpress.cost-policy.provenance' or
            type(proof.get('version')) is not int or proof['version'] != 1 or
            proof.get('synthetic_inputs') is not False or proof.get('policy_sha256') != bundle['sha256'] or
            proof.get('policy_file_sha256', bindings[str(path)]) != bindings[str(path)]):
        raise ValueError('candidate provenance must bind the non-synthetic policy content and declared file hash')
    training = comparison.get('candidate_training')
    if not isinstance(training, dict):
        raise ValueError('comparison must declare candidate training sessions and exclusions')
    names = _ids(training.get('sessions'), 'candidate training sessions')
    if bundle['screening']['sessions'] != names:
        raise ValueError('candidate policy uses different training sessions')
    _candidate_training(training, bundle)
    excluded = set(_ids(protocol['selection']['exclude_task_ids'], 'training exclusions', empty=True))
    declared = set(_ids(training.get('excluded_task_ids'), 'candidate excluded task IDs'))
    if not declared <= excluded or set(proof.get('task_ids_excluded_from_evaluation', [])) != declared:
        raise ValueError('candidate training exclusions differ from the experiment protocol')
    if training.get('exclude_all_navidrome_histories') is True and proof.get('all_navidrome_histories_excluded') is not True:
        raise ValueError('candidate provenance must declare Navidrome history exclusion')
    sources = proof.get('sources')
    if (not isinstance(sources, list) or len(sources) != len(names) or
            any(not isinstance(row, dict) or not isinstance(row.get('session'), str) for row in sources) or
            {row['session'] for row in sources} != set(names)):
        raise ValueError('candidate provenance must bind every training source')
    evaluation_ids = {task for row in protocol['families'] for key in ('pilot_tasks', 'comparison_tasks')
                      for task in row.get(key) or []}
    for row in sources:
        task_id = _text(row.get('task_id'), 'training source task ID')
        if task_id not in declared or task_id in evaluation_ids:
            raise ValueError('candidate training source overlaps the evaluation cohort or lacks an exclusion')
        source = (proof_path.parent / _text(row.get('file'), 'training source file')).resolve()
        digest = eval_plan.file_sha256(source)
        if digest != row.get('sha256'):
            raise ValueError('candidate training source has changed')
        bindings[str(source)] = digest
    entry['args']['policy'] = str(path)
    return entry


def configure(source, *, family, phase, data, bindir, resources=None, prices=None, model_catalog=None):
    """Read local inputs and validate a proposed configuration; start no work."""
    if phase not in ('pilot', 'method-pilot', 'comparison'):
        raise ValueError('phase must be pilot, method-pilot or comparison')
    bindings = {}
    source = Path(source).expanduser().resolve()
    protocol = _protocol(_read(source, bindings))
    rows = {row['family']: row for row in protocol['families']}
    if family not in rows:
        raise ValueError('choose one of the eight protocol families')
    row = rows[family]
    cohort_phase = 'pilot' if phase == 'method-pilot' else phase
    identifiers = row.get(cohort_phase + '_tasks')
    if identifiers is None:
        raise ValueError(f'{family}: freeze exact {phase} task IDs after preparing the original dataset; no implicit selection')
    stage = protocol.get('method_pilot' if phase == 'method-pilot' else phase)
    if phase == 'method-pilot':
        if not isinstance(stage, dict):
            raise ValueError('protocol must declare its method_pilot stage')
        classes = _ids(stage.get('method_classes'), 'method_pilot.method_classes')
        comparison = protocol.get('comparison')
        baselines = comparison.get('methods') if isinstance(comparison, dict) else None
        if (not isinstance(baselines, list) or not baselines or
                any(not isinstance(entry, dict) or not isinstance(entry.get('class'), str) for entry in baselines) or
                len({entry['class'] for entry in baselines}) != len(baselines)):
            raise ValueError('method pilot requires unique declared comparison baseline methods')
        indexed = {entry['class']: entry for entry in baselines}
        if 'NoCompaction' not in classes or any(name not in indexed for name in classes):
            raise ValueError('method pilot requires NoCompaction and declared comparison baselines only')
        methods = [copy.deepcopy(indexed[name]) for name in classes]
    elif isinstance(stage, dict) and isinstance(stage.get('methods'), list) and stage['methods']:
        methods = copy.deepcopy(stage['methods'])
    else:
        raise ValueError('protocol phase must declare methods, repeats and workers')
    models = protocol.get('models')
    if not isinstance(models, dict):
        raise ValueError('protocol must declare models')
    artifact_root = (source.parent / _text(protocol.get('artifact_root'), 'protocol artifact_root')).resolve()
    template_path = (source.parent / _text(row.get('template'), 'family template')).resolve()
    template = _read(template_path, bindings)
    if (not isinstance(template, dict) or template.get('schema') != 'ctxpress.eval' or
            template.get('version') != 1 or template.get('benchmark') != row['benchmark'] or
            template.get('start_mode') != 'task_start' or template.get('backend') != 'codex_docker'):
        raise ValueError('family template must use its task-start Codex adapter')
    # Retain family-specific official grading options while applying the shared
    # Agent budget. Templates cannot override the protocol's methods or cohort.
    settings = copy.deepcopy(template.get('run', {}))
    if not isinstance(settings, dict) or not isinstance(protocol.get('run'), dict):
        raise ValueError('protocol and template must declare run settings')
    settings.update(copy.deepcopy(protocol['run']))
    if phase == 'comparison':
        methods.append(_candidate(protocol, artifact_root, bindings))
    for entry in methods:
        if not isinstance(entry, dict):
            raise ValueError('invalid protocol method')
        if entry.get('class') == 'AgentDiet':
            reflection = _text(models.get('agentdiet_reflection'), 'AgentDiet reflection model')
            if (entry.get('args') or {}).get('reflect_model') != reflection:
                raise ValueError('AgentDiet reflection model differs from protocol.models')
    environment = {key: str(Path(path).expanduser().resolve()) for key, path in dict(data=data, bindir=bindir).items()}
    if resources is not None:
        environment['resources'] = str(Path(resources).expanduser().resolve())
    if model_catalog is not None:
        environment['model_catalog'] = str(Path(model_catalog).expanduser().absolute())
    config = dict(schema='ctxpress.eval', version=1, name=f'{family}-{phase}', benchmark=row['benchmark'],
                  start_mode='task_start', scope='benchmark', backend=template['backend'],
                  model=_text(models.get('agent'), 'agent model'),
                  reasoning=_text(models.get('agent_reasoning'), 'agent reasoning'),
                  tasks=copy.deepcopy(identifiers), methods=methods, environment=environment,
                  repeats=eval_plan._positive(row.get('comparison_repeats', stage.get('repeats'))
                                             if phase == 'comparison' else stage.get('repeats'), 'repeats'),
                  workers=eval_plan._positive(stage.get('workers'), 'workers'), run=settings)
    if prices is not None:
        config['prices'] = _read(prices, bindings)
        eval_usage.validate_prices(config['prices'])
    plan = eval_plan.compile_plan(config, source.parent)
    for path, digest in bindings.items():
        if eval_plan.file_sha256(path) != digest:
            raise ValueError('protocol input changed while configuring: ' + path)
    # Use normalized settings and absolute policy paths from the actual planner.
    config = plan['config']
    receipt = dict(schema='ctxpress.eval.configuration', version=1, family=family, phase=phase,
                   config_sha256=hashlib.sha256(eval_plan.canonical(config).encode()).hexdigest(),
                   sources=bindings, task_hashes={job['task']['id']: task_resources.task_digest(job['task']) for job in plan['jobs']},
                   run_count=plan['run_count'], max_parallel=plan['max_parallel'],
                   agent_timeout_seconds_upper_bound=plan['agent_timeout_seconds_upper_bound'],
                   missing_environment_files=plan['missing_environment_files'],
                   resources_complete=not plan['missing_environment_files'], experiments_started=0,
                   downloads_started=0, spending_authorized=False,
                   scope='local configuration proposal; hashes bind inputs, not task quality or permission to run')
    if plan.get('codex_model_catalog'):
        receipt['codex_model_catalog'] = copy.deepcopy(plan['codex_model_catalog'])
    return config, receipt, plan


def write(source, output, **kwargs):
    config, receipt, plan = configure(source, **kwargs)
    destination = Path(output).expanduser().resolve()
    provenance = destination.with_name(destination.name + '.provenance.json')
    # Writing a proposal must not change its protocol, training inputs, dataset,
    # source/dependency trees, policy, binaries or declared resource manifests.
    inputs = set(receipt['sources']) | set(plan['artifacts'])
    roots = [Path(tree['tree']['root']).resolve() for tree in plan['input_trees'].values()]
    for path in (destination, provenance):
        if str(path) in inputs or any(path == root or root in path.parents for root in roots):
            raise ValueError('configuration outputs must be outside their input files and trees')
        if path.exists():
            raise ValueError('configuration output already exists; choose a new output path')
    eval_plan.atomic_json(destination, config)
    eval_plan.atomic_json(provenance, receipt)
    return dict(config=str(destination), provenance=str(provenance), **{
        key: receipt[key] for key in ('family', 'phase', 'config_sha256', 'run_count', 'max_parallel',
                                      'missing_environment_files', 'resources_complete', 'experiments_started', 'downloads_started')})
