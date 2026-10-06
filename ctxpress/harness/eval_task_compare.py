"""Read-only descriptive task-start pairs; never run or recompute official grades.

Audited readers preserve each family's native metric and official evidence chain.
A repeat is a pairing index, not evidence of a shared random seed. Quality and
API accounting completeness are independent; neither implies significance.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from ctxpress.harness import eval_inputs, eval_mechanism, eval_plan, task as task_api, task_resources
from ctxpress.live import usage as eval_usage
from ctxpress.harness import eval_code_compare, eval_harbor_compare, eval_poly_compare

SUPPORTED = ('swe-bench-verified', 'swe-milestone', 'bigcodebench', 'swe-polybench', *eval_harbor_compare.BENCHMARKS)
MILESTONE_SCORES = ('score_1000', 'score_full', 'score_reliable', 'precision', 'recall', 'resolve_pct')
UNSUPPORTED = {}


def _object(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError('expected an object')
    return value


def _digest(value):
    return hashlib.sha256(eval_plan.canonical(value).encode()).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _path(path, root):
    """Only regular artifacts inside this attempt; reject credentials before read."""
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    if path.name.lower() == 'auth.json':
        raise ValueError('credential files are not analysis artifacts')
    resolved = path.resolve()
    if resolved.name.lower() == 'auth.json' or root not in resolved.parents:
        raise ValueError('artifact escapes its attempt')
    if any(p.is_symlink() for p in (path, *path.parents) if p == root or root in p.parents):
        raise ValueError('artifact is a symbolic link')
    return resolved


def _read(path, root, evidence, expected=None, *, document=True):
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)):
        raise ValueError('invalid recorded SHA-256')
    path = _path(path, root)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected is not None and digest != expected:
        raise ValueError('artifact hash changed')
    if eval_plan.file_sha256(path) != digest:
        raise ValueError('artifact changed while reading')
    evidence.append(dict(path=str(path), sha256=digest, recorded_sha256=expected))
    return _object(json.loads(raw)) if document else raw


def _records(records, folder, evidence):
    if not isinstance(records, dict) or not records:
        raise ValueError('recorded artifact hashes missing')
    documents = {}
    for name, record in records.items():
        if not isinstance(record, dict) or not isinstance(record.get('sha256'), str) or len(record['sha256']) != 64:
            raise ValueError('recorded artifact hash missing')
        # Map keys are paths relative to the producing attempt/trial. Callers
        # additionally bind their base directory below.
        documents[name] = _read(record['path'], folder, evidence, record['sha256'], document=False)
    return documents


def _expected_method(entry, plan, folder, evidence):
    """Mirror only the driver's path substitution, without invoking its copier."""
    entry = copy.deepcopy(entry)
    def visit(value):
        if not isinstance(value, dict):
            return
        args = value.get('args') or {}
        key = {'CostModel': 'profile', 'AutoCostModel': 'policy'}.get(value.get('class'))
        if key and args.get(key) and not isinstance(args[key], dict):
            digest = plan['artifacts'][args[key]]
            _read(folder / 'method-inputs' / (digest + '.json'), folder, evidence, digest, document=False)
            args[key] = '/ctxpress-method/' + digest + '.json'
        visit(args.get('inner'))
        for child in args.get('methods', []):
            visit(child)
    visit(entry)
    return entry


def _binding(plan, spec, result, folder, directory, paths, evidence):
    expected_task = task_api.remap(spec['task'], paths)
    if (result.get('task') != expected_task or result.get('method') != eval_inputs.method(spec['method'], paths) or
            result.get('model') != plan['config']['model'] or result.get('reasoning') != plan['config']['reasoning'] or
            (result.get('benchmark') or {}).get('name') != plan['config']['benchmark'] or
            result.get('protocol') != 'ctxpress_comparison' or
            result.get('start_mode', 'task_start') != 'task_start'):
        raise ValueError('result task/method/model/reasoning/protocol differs from frozen plan')
    model_rows = [row for row in result.get('rewrites', []) if 'request' in row or row.get('type') in ('summary', 'native_compaction')]
    main_rows = [row for row in model_rows if 'request' in row]
    if (type(result.get('requests')) is not int or result['requests'] <= 0 or not any(
            type(row.get('status')) is int and 200 <= row['status'] < 300 and not row.get('stream_error') for row in main_rows)):
        raise ValueError('no observed successful model request')
    if any(row.get('model') != plan['config']['model'] for row in main_rows):
        raise ValueError('observed main request model differs or is unrecorded')
    logs = _read(result['proxy_log'], folder, evidence, document=False)
    logged = [json.loads(line) for line in logs.decode('utf-8').splitlines() if line.strip()]
    if logged != result.get('rewrites'):
        raise ValueError('request log differs from recorded result')
    from ctxpress.harness import eval_host_compare
    eval_host_compare.binding(plan, folder, directory, paths, evidence)
    if plan['config']['benchmark'] in eval_harbor_compare.BENCHMARKS:
        eval_harbor_compare.binding(plan, spec, result, folder, directory, paths, evidence)
        return
    if plan['config']['benchmark'] == 'bigcodebench':
        eval_code_compare.binding(plan, spec, result, folder, directory, paths, evidence)
        return
    if plan['config']['benchmark'] == 'swe-polybench':
        eval_poly_compare.binding(plan, spec, result, folder, directory, paths, evidence)
        return
    # Do not infer reasoning settings from a returned model name. The dispatch
    # request below binds reasoning, budget, resources and the frozen package.
    milestone = plan['config']['benchmark'] == 'swe-milestone'
    request = _read(folder / ('milestone-execute-request.json' if milestone else 'swe-request.json'), folder, evidence)
    settings = request['execution'] if milestone else request
    expected = dict(model=plan['config']['model'], reasoning=plan['config']['reasoning'],
                    method=_expected_method(spec['method'], plan, folder, evidence), run=plan['config']['run'],
                    compact_limit=spec['compact_limit'], label=plan['sha256'][:12] + '-' + spec['id'])
    if (any(settings.get(key) != value for key, value in expected.items()) or request.get('task') != expected_task or
            request.get('folder') != str(folder) or request.get('package') != str(directory / 'runtime')):
        raise ValueError('actual dispatch differs from frozen specification')
    images = spec['resources']['images']
    if milestone:
        if request.get('original_task') != spec['task'] or request.get('resources') != paths[plan['config']['environment']['resources']]:
            raise ValueError('actual native task/resources differ')
        lock = _object(json.loads(Path(request['resources']).read_bytes()))
        if lock != plan['task_resources']:
            raise ValueError('actual resource manifest differs')
        image_record = _read(folder / 'native-images.json', folder, evidence)
        if (image_record.get('task_id') != spec['task']['id'] or
                set(image_record.get('grading', {})) != set(images['grading']) or any(
                image_record['grading'][name].get('id') != image['id'] for name, image in images['grading'].items())):
            raise ValueError('observed native grading images differ')
    elif (request.get('agent_image') != images['agent']['id'] or
          request.get('grading_image') != images['grading']['verifier']['id']):
        raise ValueError('actual Agent/verifier image differs')


def _verified(spec, result, folder, evidence):
    grade = _object(result.get('grade'))
    if grade.get('infra_invalid') is not False or grade.get('error') or grade.get('failure_kind') or type(grade.get('resolved')) is not bool:
        raise ValueError('missing or invalid official boolean grade')
    if grade.get('instance_id') != spec['task']['id']:
        raise ValueError('grade instance differs')
    if not isinstance(grade.get('report_sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', grade['report_sha256']):
        raise ValueError('official report hash missing')
    report = _read(grade['report'], folder, evidence, grade['report_sha256'])
    row = report.get(spec['task']['id'])
    if (not isinstance(row, dict) or type(row.get('resolved')) is not bool or
            row['resolved'] != grade['resolved'] or row.get('infra_failure') or row.get('test_only') or
            row != grade.get('raw_instance_report')):
        raise ValueError('official report differs from recorded verdict')
    records = result.get('official_artifacts')
    _records(records, folder, evidence)
    paths = {str(_path(record['path'], folder)) for record in records.values()}
    if str(_path(grade['report'], folder)) not in paths:
        raise ValueError('official report not included in recorded artifact hashes')
    for name in ('model.patch', 'predictions.jsonl'):
        if name not in records or _path(records[name]['path'], folder) != folder / name:
            raise ValueError('submission artifacts missing or unbound')
    for name, record in records.items():
        if _path(record['path'], folder) != _path(name, folder):
            raise ValueError('official artifact name/path differs')
    predictions = [json.loads(line) for line in (folder / 'predictions.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    # Bind the captured patch exactly; universal-newline decoding changes CR bytes.
    patch = (folder / 'model.patch').read_bytes().decode('utf-8')
    if (len(predictions) != 1 or predictions[0].get('instance_id') != spec['task']['id'] or
            predictions[0].get('model_name_or_path') != result['model'] or predictions[0].get('model_patch') != patch):
        raise ValueError('official prediction does not bind selected instance/model/patch')
    state = _read(folder / 'swe-worker-result.json', folder, evidence)
    images = spec['resources']['images']
    separation = result.get('separate_verifier') or {}
    if (state.get('official_artifacts') != records or state.get('official_report') != result.get('official_report') or
            _path(result['official_report'], folder) != _path(grade['report'], folder) or
            state.get('separate_verifier') != separation or
            separation.get('agent_image') != images['agent']['id'] or
            separation.get('verifier_image') != images['grading']['verifier']['id'] or
            separation.get('author_grading') is not True or separation.get('author_patch_application') is not True or
            separation.get('checked_cleanup') is not True):
        raise ValueError('official independent verifier evidence differs or is missing')
    return {'resolved': grade['resolved']}, dict(kind='boolean_issue_outcome')


def _milestone(spec, result, folder, evidence):
    grade = _object(result.get('grade'))
    if (grade.get('quality_kind') != 'milestone' or grade.get('official_metrics_valid') is not True or
            grade.get('infra_invalid') is not False or grade.get('error') or grade.get('failure_kind') or
            (grade.get('official_metrics') or {}).get('error') is not False or
            grade.get('collector') != 'harness.e2e.collect_results'):
        raise ValueError('missing or invalid official milestone metrics')
    state = _read(folder / 'native-execution.json', folder, evidence)
    if state.get('task_id') != spec['task']['id'] or state.get('cleanup_complete') is not True:
        raise ValueError('native execution/task evidence missing')
    trial = _path(state['trial'], folder)
    metadata = _read(trial / 'trial_metadata.json', folder, evidence)
    if (metadata.get('repo_name') != spec['task']['id'] or metadata.get('model') != result['model'] or
            metadata.get('reasoning_effort') != result['reasoning'] or
            metadata.get('image') != spec['resources']['images']['agent']['id']):
        raise ValueError('actual native Agent image/model/reasoning differs')
    sibling = spec['task']['initial_state'].get('sibling_repo_config_sha256')
    if sibling and (metadata.get('repo_config_binding') or {}).get('sha256') != sibling:
        raise ValueError('actual native repository config differs')
    if _path(result['official_report'], folder) != folder / 'native-grade.json':
        raise ValueError('native grade report is not from this attempt')
    if _read(result['official_report'], folder, evidence) != grade:
        raise ValueError('native grade report differs from recorded grade')
    records = grade.get('reports')
    documents = _records(records, folder, evidence)
    if any(_object(json.loads(raw)).get('test_only') for name, raw in documents.items() if name.startswith('evaluation/')):
        raise ValueError('test_only official report')
    for name, record in records.items():
        if _path(record['path'], folder) != _path(trial / name, folder):
            raise ValueError('native report name/path differs')
    summary = _object(json.loads(documents['evaluation/summary.json']))
    active = spec['task']['evaluation']['active_milestones']
    graded = spec['task']['evaluation']['graded_milestones']
    if (summary != grade.get('raw_summary') or summary.get('repo_name') != spec['task']['id'] or
            summary.get('agent_name') != 'codex' or summary.get('total_milestones') != len(active) or summary.get('error')):
        raise ValueError('native summary differs from selected itinerary')
    # Reject newly appearing files/attempts as well as edits to recorded files.
    actual = {'evaluation/' + p.relative_to(trial / 'evaluation').as_posix() for p in (trial / 'evaluation').rglob('*')
              if p.name in ('summary.json', 'evaluation_result.json', 'evaluation_result_filtered.json')}
    expected = {name for name in records if name.startswith('evaluation/')}
    if actual != expected:
        raise ValueError('native report coverage changed')
    outcomes = grade.get('milestones') or {}
    coverage = grade.get('coverage') or {}
    metrics = grade['official_metrics']
    if (set(outcomes) != set(active) or coverage.get('grading_errors') != {} or coverage.get('infra_invalid') != [] or
            coverage.get('graded') != len(graded) or metrics.get('graded') != len(graded) or
            metrics.get('total_milestones') != len(active) or metrics.get('infra_invalid') != 0):
        raise ValueError('native denominator/coverage evidence differs')
    served = set()
    for mid, outcome in outcomes.items():
        cell = outcome.get('served_attempt')
        if cell is None:
            if outcome.get('resolved') is not None or mid not in coverage.get('unsubmitted', []):
                raise ValueError('unserved milestone is not a documented unsubmitted node')
            continue
        if cell != mid and not (cell.startswith(mid + '-retry') and cell[len(mid + '-retry'):].isdigit()):
            raise ValueError('served milestone identity differs')
        key = 'evaluation/' + cell + '/evaluation_result_filtered.json'
        if key not in documents:
            key = 'evaluation/' + cell + '/evaluation_result.json'
        row = _object(json.loads(documents[key]))
        if (row.get('milestone_id') != mid or type(row.get('resolved')) is not bool or
                row['resolved'] != outcome.get('resolved') or row.get('infra_invalid') or row.get('error') or row.get('failure_kind')):
            raise ValueError('served official milestone report is invalid or differs')
        served.add(mid)
    for row in (summary.get('results') or {}).values():
        if row.get('error') or row.get('failure_kind') or row.get('eval_status') == 'error':
            raise ValueError('native summary contains grading errors')
    counts = ('graded', 'submitted', 'evaluated', 'scoreable', 'infra_invalid', 'resolved', 'total_milestones')
    if any(type(metrics.get(name)) is not int or metrics[name] < 0 for name in counts):
        raise ValueError('official milestone denominators/counters missing or invalid')
    unsubmitted = coverage.get('unsubmitted')
    if (not isinstance(unsubmitted, list) or len(unsubmitted) != len(set(unsubmitted)) or
            set(unsubmitted) != set(active) - served or type(coverage.get('served_graded')) is not int or
            coverage.get('served_graded') != len(served & set(graded)) or
            type(coverage.get('graded')) is not int or
            grade.get('scoring_complete') is not (set(graded) <= served) or
            grade.get('submission_complete') is not (not (set(graded) & set(unsubmitted)))):
        raise ValueError('served/submitted coverage differs')
    values = {}
    for name in MILESTONE_SCORES:
        value = metrics.get(name)
        if not _number(value):
            raise ValueError('official score missing or nonnumeric: ' + name)
        values['milestone.' + name] = value
    return values, dict(kind='author_milestone_scores', official_metrics=metrics,
                        official_metrics_valid=True, coverage=coverage,
                        submission_complete=grade.get('submission_complete'), scoring_complete=grade.get('scoring_complete'))


def _assessment(plan, spec, members, directory, paths, common_reasons, supported):
    reasons = list(common_reasons)
    quality_reasons = [] if supported else ['unsupported benchmark comparison: ' + UNSUPPORTED.get(plan['config']['benchmark'], 'no audited reader')]
    evidence = []
    output = dict(job_id=spec['id'], status=None, attempt=None, reasons=reasons, quality_reasons=quality_reasons,
                  quality_admissible=False, metrics={}, grade_details=None, artifacts=evidence,
                  usage=None, api_usage_complete=False, api_cost_complete=False, api_cost_usd=None, bill=None,
                  cost_reasons=[])
    if not members:
        reasons.append('missing planned job')
        return output
    if len(members) != 1:
        reasons.append('duplicate cohort member')
        return output
    job = members[0]
    output.update(status=job.get('status'), attempt=job.get('attempt'))
    try:
        if _object(job.get('spec')) != spec or job.get('id') != spec['id']:
            reasons.append('changed job specification')
    except (ValueError, TypeError):
        reasons.append('missing or invalid job specification')
    if job.get('status') != 'completed':
        reasons.append('job status: ' + str(job.get('status')))
    if type(job.get('attempt')) is not int or job['attempt'] != 1:
        reasons.append('retry or unrecorded attempt; automatic attempt selection unsupported')
    folder = directory / 'jobs' / spec['id'] / 'attempt-1'
    if set(p.name for p in folder.parent.glob('attempt-*')) != {'attempt-1'}:
        reasons.append('retry or missing attempt directory')
    if reasons:
        return output
    if not supported:
        output['cost_reasons'].append('execution/accounting evidence reader unsupported for this benchmark')
        return output
    try:
        result = _object(job.get('result'))
        if result.get('test_only') or (result.get('grade') or {}).get('test_only'):
            raise ValueError('test_only execution or grade')
        if _read(folder / 'result.json', folder, evidence) != result:
            raise ValueError('recorded result differs from attempt artifact')
        _binding(plan, spec, result, folder, directory, paths, evidence)
        output['mechanism_observation'] = eval_mechanism.observe(result)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError) as error:
        if isinstance(error, eval_harbor_compare.UnsupportedVariant):
            output.update(comparison_variant_supported=False, unsupported_reason=str(error))
        reasons.append('unverified execution artifacts: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__))
        return output
    try:
        usage = eval_usage.analyze(result, plan['config']['model'])
        bill = eval_usage.bill(usage, plan['config'].get('prices'))
        complete = usage['complete'] and usage['model_usage_complete'] and not usage['missing_api_cache_write_usage']
        # The generic bill supports limited legacy static estimates. This
        # comparison requires observed cache writes, so do not expose a legacy
        # zero-write assumption as a complete cost, even inside model buckets.
        for bucket in bill['usage_by_model'].values():
            if bucket['missing_api_cache_write_usage']:
                bucket['api_cost_at_declared_rates_usd'] = None
        if not complete:
            bill['api_cost_complete'] = False
            bill['api_cost_at_declared_rates_usd'] = None
        # Partial token sums are recorded observations, never complete totals.
        totals = {key: usage[key] if complete else None for key in eval_usage.TOKEN_FIELDS + (eval_usage.CACHE_WRITE_FIELD,)}
        output.update(usage=dict(totals=totals, summary_calls=usage['summary_calls'],
                                 native_compaction_calls=usage['native_compaction_calls'],
                                 passthrough_calls=usage.get('passthrough_calls', 0),
                                 missing_api_usage=usage['missing_api_usage'], usage_conflicts=usage['usage_conflicts']),
                      api_usage_complete=complete, bill=bill,
                      api_cost_complete=complete and bill['api_cost_complete'])
        if output['api_cost_complete']:
            output['api_cost_usd'] = bill['api_cost_at_declared_rates_usd']
        if not complete:
            output['cost_reasons'].append('missing/conflicting token usage, model identity or cache-write observation')
        if not bill['api_cost_complete']:
            output['cost_reasons'].append('missing usage or declared model prices')
    except (ValueError, TypeError, KeyError, AttributeError):
        output['cost_reasons'].append('invalid usage or declared prices')
    try:
        from ctxpress.harness import execution_health
        health = execution_health.observe(result)
        output['execution_health'] = health
        if health['execution_invalid']:
            if any(row.get('kind') == 'method_tool_route_failure' for row in health['tool_runtime_failures']):
                raise ValueError('method control routing is invalid or unverified; official grade is diagnostic only')
            raise ValueError('tool runtime failed to start; official grade is diagnostic only')
        if health.get('evidence_errors'):
            raise ValueError('tool runtime evidence is unreadable or invalid')
        from ctxpress.harness import eval_host_compare
        output['host_observation'] = eval_host_compare.observe(result, folder, evidence)
        if output['host_observation']['model_metadata_fallbacks']:
            quality_reasons.append('incompatible Codex host: model metadata fell back; instructions/tools differ')
            return output
        reader = (_milestone if plan['config']['benchmark'] == 'swe-milestone' else
                  eval_harbor_compare.quality if plan['config']['benchmark'] in eval_harbor_compare.BENCHMARKS else
                  eval_code_compare.quality if plan['config']['benchmark'] == 'bigcodebench' else
                  eval_poly_compare.quality if plan['config']['benchmark'] == 'swe-polybench' else _verified)
        output['metrics'], output['grade_details'] = reader(spec, result, folder, evidence)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError) as error:
        if isinstance(error, eval_harbor_compare.UnsupportedVariant):
            output.update(comparison_variant_supported=False, unsupported_reason=str(error))
        quality_reasons.append('unverified official grade: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__))
    output['quality_admissible'] = not quality_reasons
    return output


def _metric_summary(pairs, name):
    valid = [p['metrics'][name] for p in pairs if p['metrics'][name]['comparable']]
    complete = bool(pairs) and len(valid) == len(pairs)
    def mean(field):
        return sum(row[field] for row in valid) / len(valid) if valid else None
    return dict(planned_pairs=len(pairs), comparable_pairs=len(valid), complete=complete,
                reference_mean=mean('reference') if complete else None,
                candidate_mean=mean('candidate') if complete else None,
                delta_mean=mean('delta') if complete else None,
                observed_subset=dict(reference_mean=mean('reference'), candidate_mean=mean('candidate'), delta_mean=mean('delta')))


def _author_code_cohorts(plan, jobs, labels, assessments, directory, paths, common_reasons):
    """Read stored author cohorts without changing sample or accounting gates."""
    cohorts = {}
    for label in labels:
        artifacts = []
        reasons = list(common_reasons)
        block = dict(available=False, reasons=reasons, cohort=None, artifacts=artifacts)
        cohorts[label] = block
        if not reasons:
            for key, assessment in assessments.items():
                if key[2] == label and not assessment['quality_admissible']:
                    reasons.extend(assessment['job_id'] + ': ' + reason for reason in
                                   assessment['reasons'] + assessment['quality_reasons'])
        if reasons:
            continue
        try:
            cohort = eval_code_compare.author_cohort(plan, jobs, directory, paths, artifacts, label=label)
            if cohort is None:
                reasons.append('stored author cohort report is missing')
            else:
                block.update(available=True, cohort=cohort)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError) as error:
            reasons.append('unverified author code cohort: ' +
                           (str(error) if isinstance(error, ValueError) else type(error).__name__))
    return cohorts


def compare(plan, jobs, *, reference, directory):
    """Return JSON-serializable evidence for one frozen plan and explicit reference.

    jobs accepts database-style dictionaries (spec/result may be JSON strings).
    Invalid plan/scope/reference raises ValueError. Missing/failed/changed run
    evidence stays in coverage with null quality/cost. Read no auth, launch no
    subprocess, mutate no artifacts, and never call the author's score collector.
    """
    config = plan.get('config') or {}
    if config.get('scope') != 'benchmark' or config.get('start_mode') != 'task_start':
        raise ValueError('task comparison requires benchmark scope and task_start; checkpoint/formal/mechanism are excluded')
    eval_plan.verify(plan, check_inputs=False)
    if (plan.get('benchmark') or {}).get('name') != config['benchmark']:
        raise ValueError('benchmark metadata differs from frozen configuration')
    specs = plan['jobs']
    labels = list(dict.fromkeys(spec['label'] for spec in specs))
    if not isinstance(reference, str) or reference not in labels:
        raise ValueError('reference must explicitly name a frozen method label')
    declared_methods = {entry.get('label') or entry['class']: entry for entry in config['methods']}
    frozen, ids, task_bindings = {}, set(), {}
    for spec in specs:
        key = (spec['task']['id'], spec['repeat'], spec['label'])
        if key in frozen or spec['id'] in ids or 'boundary' in spec or spec['task']['start_mode'] != 'task_start':
            raise ValueError('duplicate or boundary job in task-start plan')
        # IDs are used for artifact directories, never allow traversal.
        if Path(spec['id']).name != spec['id'] or spec['id'] in ('.', '..') or '\\' in spec['id']:
            raise ValueError('invalid planned job ID')
        if type(spec['repeat']) is not int or not 0 <= spec['repeat'] < config['repeats']:
            raise ValueError('planned repeat is outside frozen repeat range')
        if spec.get('resources') and spec['resources'].get('task_sha256') != task_resources.task_digest(spec['task']):
            raise ValueError('job resource task digest differs')
        if spec['method'] != declared_methods.get(spec['label']):
            raise ValueError('job method differs from frozen configuration')
        binding = (spec['task'], spec.get('resources'))
        if spec['task']['id'] in task_bindings and task_bindings[spec['task']['id']] != binding:
            raise ValueError('frozen methods do not share identical task/resource specifications')
        task_bindings[spec['task']['id']] = binding
        ids.add(spec['id'])
        frozen[key] = spec
    directory = Path(directory).resolve()
    common_reasons, paths = [], {}
    runtime_digest = None
    try:
        if _read(directory / 'plan.json', directory, []) != plan:
            raise ValueError('directory plan differs')
        runtime = directory / 'runtime' / 'ctxpress'
        if not runtime.is_dir():
            raise ValueError('frozen runtime missing')
        _path(runtime, directory)
        for source in runtime.rglob('*.py'):
            _path(source, directory)
        runtime_digest = eval_plan.fingerprint(runtime)
        if runtime_digest != plan['code_sha256']:
            raise ValueError('frozen execution code changed')
        if plan.get('input_snapshot_version') != 1:
            raise ValueError('frozen task inputs missing')
        _read(directory / 'inputs' / 'manifest.json', directory, [])
        paths = eval_inputs.verify(plan, directory / 'inputs')
        if not plan.get('task_resources') or plan.get('missing_environment_files'):
            raise ValueError('official resources incomplete or undeclared')
    except (OSError, ValueError, TypeError, KeyError) as error:
        common_reasons.append('unverified frozen run: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__))
    # BigCode's optional stored cohort reader consumes the same observations
    # after per-sample assessment, including when the caller supplies an iterator.
    if config['benchmark'] == 'bigcodebench':
        jobs = list(jobs)
    members = defaultdict(list)
    unplanned = []
    by_id = {spec['id']: key for key, spec in frozen.items()}
    for source in jobs:
        job = dict(source)
        try:
            spec = _object(job.get('spec'))
            key = (spec['task']['id'], spec['repeat'], spec['label'])
        except (ValueError, TypeError, KeyError):
            key = None
        assigned = by_id.get(job.get('id'), key)
        if assigned in frozen:
            members[assigned].append(job)
            # A changed ID/spec cannot conceal a duplicate of another member.
            if key in frozen and key != assigned:
                members[key].append(job)
        else:
            unplanned.append(dict(id=job.get('id'), reason='unplanned or malformed job'))
    supported = config['benchmark'] in SUPPORTED
    assessments = {key: _assessment(plan, spec, members[key], directory, paths, common_reasons, supported)
                   for key, spec in frozen.items()}
    cohort_evidence = {}
    if config['benchmark'] == 'bigcodebench':
        cohort_evidence['author_code_cohorts'] = _author_code_cohorts(
            plan, jobs, labels, assessments, directory, paths, common_reasons)
    names = ['resolved'] if config['benchmark'] == 'swe-bench-verified' else (
        ['milestone.' + name for name in MILESTONE_SCORES] if config['benchmark'] == 'swe-milestone' else
        sorted({name for assessment in assessments.values() for name in assessment['metrics']}))
    candidates = []
    for label in labels:
        if label == reference:
            continue
        coordinates = sorted({(task, repeat) for task, repeat, method in frozen if method in (reference, label)})
        pairs = []
        for identity, repeat in coordinates:
            missing = dict(reasons=['missing planned method/task/repeat'], quality_reasons=[], metrics={}, quality_admissible=False,
                           api_cost_complete=False, api_cost_usd=None)
            base = assessments.get((identity, repeat, reference), missing)
            candidate = assessments.get((identity, repeat, label), missing)
            reasons = [side + ': ' + reason for side, value in (('reference', base), ('candidate', candidate))
                       for reason in value['reasons'] + value['quality_reasons']]
            comparable = base['quality_admissible'] and candidate['quality_admissible'] and supported
            if comparable and set(base['metrics']) != set(candidate['metrics']):
                comparable = False
                reasons.append('paired author metric names differ; no missing metric imputation')
            host_base = (base.get('host_observation') or {}).get('base_instruction_sha256')
            host_candidate = (candidate.get('host_observation') or {}).get('base_instruction_sha256')
            host_comparable = not any((value.get('host_observation') or {}).get('model_metadata_fallbacks')
                                      for value in (base, candidate))
            if host_base and host_candidate and host_base != host_candidate:
                comparable = False
                host_comparable = False
                reasons.append('paired Codex base instructions differ; host equivalence is not established')
            metric_pairs = {}
            for name in names:
                a, b = base['metrics'].get(name), candidate['metrics'].get(name)
                valid = comparable and a is not None and b is not None
                metric_pairs[name] = dict(reference=a, candidate=b, comparable=valid, delta=(b - a) if valid else None)
            cost_complete = base['api_cost_complete'] and candidate['api_cost_complete']
            cost_comparable = cost_complete and host_comparable
            pairs.append(dict(task_id=identity, repeat=repeat, quality_admissible=comparable, reasons=reasons,
                              metrics=metric_pairs, reference=base, candidate=candidate, api_cost_complete=cost_complete,
                              api_cost_comparable=cost_comparable,
                              reference_api_cost_usd=base['api_cost_usd'], candidate_api_cost_usd=candidate['api_cost_usd'],
                              api_cost_delta_usd=candidate['api_cost_usd'] - base['api_cost_usd'] if cost_comparable else None))
        quality_complete = bool(pairs) and all(p['quality_admissible'] for p in pairs)
        cost_complete = bool(pairs) and all(p['api_cost_complete'] for p in pairs)
        cost_comparable = bool(pairs) and all(p['api_cost_comparable'] for p in pairs)
        candidates.append(dict(method=label, quality_complete=quality_complete, api_cost_complete=cost_complete,
            api_cost_comparable=cost_comparable,
            coverage=dict(planned_pairs=len(pairs), comparable_pairs=sum(p['quality_admissible'] for p in pairs),
                          cost_comparable_pairs=sum(p['api_cost_comparable'] for p in pairs),
                          cost_pairs=sum(p['api_cost_complete'] for p in pairs), reasons=dict(Counter(r for p in pairs for r in p['reasons']))),
            metrics={name: _metric_summary(pairs, name) for name in names},
            tasks=[dict(task_id=identity, metrics={name: _metric_summary(
                [p for p in pairs if p['task_id'] == identity], name) for name in names})
                for identity in sorted({p['task_id'] for p in pairs})],
            costs=dict(reference_api_cost_usd=sum(p['reference_api_cost_usd'] for p in pairs) if cost_complete else None,
                       candidate_api_cost_usd=sum(p['candidate_api_cost_usd'] for p in pairs) if cost_complete else None,
                       api_cost_delta_usd=sum(p['api_cost_delta_usd'] for p in pairs) if cost_comparable else None), pairs=pairs))
    return dict(schema='ctxpress.eval.task_comparison', version=1, reference=reference, benchmark=config['benchmark'],
        scope='benchmark', start_mode='task_start', supported=supported, directory=str(directory),
        model=config['model'], reasoning=config['reasoning'], backend=config['backend'],
        benchmark_release=(plan.get('task_resources') or {}).get('release'),
        metric_names=names, delta_convention='candidate minus reference; no utility or quality constraint verdict',
        unsupported_reason=None if supported else UNSUPPORTED.get(config['benchmark'], 'no audited reader'),
        plan_sha256=plan['sha256'], code_sha256=plan['code_sha256'], observed_runtime_code_sha256=runtime_digest,
        analysis_code_sha256=eval_plan.fingerprint(), analysis_module_sha256=eval_plan.file_sha256(__file__),
        task_resource_manifest_sha256=(plan.get('task_resources') or {}).get('sha256'), prices=config.get('prices'),
        pairing='frozen (task_id, repeat, method label); repeat index does not establish shared random seeds',
        quality_scope='descriptive official metrics within this benchmark only; no cross-family quality aggregate, significance or noninferiority inference',
        cost_scope='observed main, summary/reflection, native compaction and passthrough API tokens at declared per-model cache-write/long-context rates; unknowns stay null; not an invoice; dedicated pruner and host compute excluded',
        coverage=dict(planned_jobs=len(specs), admissible_jobs=sum(v['quality_admissible'] for v in assessments.values()),
                      cost_jobs=sum(v['api_cost_complete'] for v in assessments.values()), unplanned_jobs=unplanned,
                      reasons=dict(Counter(r for v in assessments.values() for r in v['reasons'] + v['quality_reasons']))),
        jobs=[dict(task_id=key[0], repeat=key[1], method=key[2], **value) for key, value in assessments.items()],
        candidates=candidates, **cohort_evidence)
