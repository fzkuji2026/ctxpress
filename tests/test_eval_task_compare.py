"""Offline artifact fixtures exercise analysis gates, never benchmark performance."""
import copy
import json
from pathlib import Path

import pytest

from ctxpress import benchmarks
from ctxpress.benchmarks.swe import adapter as swe_bench
from ctxpress.harness.jobs import inputs as eval_inputs, plan as eval_plan, trees as eval_trees, resources as task_resources
from ctxpress.core import artifacts as artifact_io
from ctxpress.harness.results import task_compare as eval_task_compare
from test_eval_families import fixture_data


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def rates(multiplier=1):
    return dict(input=2 * multiplier, cached=.1 * multiplier, cache_write=2.5 * multiplier,
                output=10 * multiplier, unit='USD_per_million_tokens', source='offline test fixture', as_of='2026-10-05',
                long_context=dict(input_tokens_threshold=100, input=4 * multiplier,
                                  cached=.2 * multiplier, cache_write=5 * multiplier, output=15 * multiplier))


def fixture(tmp_path, name='swe-bench-verified', repeats=1, tasks=1, prices=True):
    root = fixture_data(tmp_path, name)
    if tasks == 2:
        dataset = root / 'instances.jsonl'
        if not dataset.exists():
            dataset = root / 'instances.json'
        rows, _ = swe_bench.rows(dataset)
        second = copy.deepcopy(rows[0]); second['instance_id'] = 'fixture__second-2'
        rows = [rows[0], second]
        dataset.write_text('\n'.join(json.dumps(row) for row in rows) if dataset.suffix == '.jsonl' else json.dumps(rows), encoding='utf-8')
    selected = benchmarks.get(name).task_instances(root)[:tasks]
    official = tmp_path / 'official'; official.mkdir()
    (official / 'collector.py').write_text('# offline official fixture\n', encoding='utf-8')
    tree = eval_trees.capture(official, ['collector.py'], folder='unused')['tree']
    images = dict(agent={'id': 'sha256:' + '1' * 64}, grading={
        ('M2' if name == 'swe-milestone' else 'verifier'): {'id': 'sha256:' + '2' * 64}})
    lock = task_resources.seal(dict(schema=task_resources.SCHEMA, version=1, benchmark=name, release='offline-fixture-v1',
        trees={'code': tree}, tasks={task['id']: dict(task_sha256=task_resources.task_digest(task), images=images) for task in selected}))
    resource = write(tmp_path / 'resources.json', lock)
    binary = tmp_path / 'bin'; binary.mkdir(); (binary / 'codex').write_bytes(b'offline binary fixture')
    config = dict(schema='ctxpress.eval', version=1, scope='benchmark', start_mode='task_start', benchmark=name,
        model='main', reasoning='medium', backend='codex_docker', repeats=repeats, tasks=[task['id'] for task in selected],
        methods=[dict(label='reference', **{'class': 'NoCompaction'}), dict(label='candidate', **{'class': 'NoCompaction'})],
        environment=dict(data=str(root), bindir=str(binary), resources=str(resource)),
        prices={'models': {'main': rates(), 'reflect': rates(.1)}})
    if not prices:
        config.pop('prices')
    plan = eval_plan.compile_plan(config)
    # This small frozen runtime deliberately models recorded files, never an
    # executable prepared benchmark. The fixtures cannot produce real scores.
    directory = tmp_path / 'run'
    runtime = directory / 'runtime' / 'ctxpress'; runtime.mkdir(parents=True)
    (runtime / 'fixture.py').write_text('# frozen offline run fixture\n', encoding='utf-8')
    plan['code_sha256'] = eval_plan.fingerprint(runtime)
    plan['missing_environment_files'] = []
    plan['sha256'] = eval_task_compare._digest({k: v for k, v in plan.items() if k != 'sha256'})
    write(directory / 'plan.json', plan)
    paths = eval_inputs.prepare(plan, directory / 'inputs')
    jobs = []
    for spec in plan['jobs']:
        folder = directory / 'jobs' / spec['id'] / 'attempt-1'
        folder.mkdir(parents=True)
        moved = eval_task_compare.task_api.remap(spec['task'], paths)
        rows = [dict(request=1, status=200, model='main', response_model='main',
                     usage=dict(input_tokens=120, cached_tokens=20, cache_write_tokens=10, output_tokens=5))]
        logs = folder / 'requests.jsonl'
        logs.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        result = dict(task=moved, method=spec['method'], model='main', reasoning='medium',
            benchmark={'name': name}, protocol='ctxpress_comparison', start_mode='task_start', requests=1,
            rewrites=rows, proxy_log=str(logs), usage=dict(summary_calls=0, native_compaction_calls=0))
        settings = dict(model='main', reasoning='medium', method=spec['method'], run=plan['config']['run'],
                        compact_limit=spec['compact_limit'], label=plan['sha256'][:12] + '-' + spec['id'])
        common = dict(task=moved, package=str(directory / 'runtime'), folder=str(folder))
        if name == 'swe-milestone':
            request = dict(common, execution=settings, original_task=spec['task'], resources=paths[str(resource)])
            write(folder / 'milestone-execute-request.json', request)
            write(folder / 'native-images.json', dict(task_id=spec['task']['id'], grading={'M2': images['grading']['M2']}))
            trial = folder / 'trial'
            write(trial / 'trial_metadata.json', dict(repo_name=spec['task']['id'], model='main',
                reasoning_effort='medium', image=images['agent']['id']))
            raw = dict(repo_name=spec['task']['id'], agent_name='codex', total_milestones=2,
                       results={'M2': dict(eval_status='passed', attempt=0, test_summary={'total': 1})})
            summary = write(trial / 'evaluation' / 'summary.json', raw)
            cell = write(trial / 'evaluation' / 'M2' / 'evaluation_result.json',
                         dict(milestone_id='M2', resolved=True, test_summary={'total': 1}))
            metrics = dict(error=False, graded=1, submitted=1, evaluated=1, scoreable=1, infra_invalid=0,
                           resolved=1, resolve_pct=100., score_1000=700., score_full=.7, score_reliable=.9,
                           precision=.8, recall=.6, total_milestones=2, cost=None)
            reports = {str(p.relative_to(trial)): dict(path=str(p), sha256=eval_plan.file_sha256(p)) for p in (summary, cell)}
            grade = dict(quality_kind='milestone', official_metrics_valid=True, infra_invalid=False,
                collector='harness.e2e.collect_results', official_metrics=metrics, raw_summary=raw, reports=reports,
                scoring_complete=True, submission_complete=True, resolved=True,
                coverage=dict(graded=1, served_graded=1, unsubmitted=['M1'], grading_errors={}, infra_invalid=[]),
                milestones=dict(M1=dict(served_attempt=None, resolved=None), M2=dict(served_attempt='M2', resolved=True)))
            write(folder / 'native-execution.json', dict(task_id=spec['task']['id'], cleanup_complete=True, trial=str(trial)))
            report = write(folder / 'native-grade.json', grade)
            result.update(grade=grade, official_report=str(report))
        else:
            request = dict(common, **settings, agent_image=images['agent']['id'], grading_image=images['grading']['verifier']['id'])
            write(folder / 'swe-request.json', request)
            resolved = spec['label'] == 'reference'
            raw = {'resolved': resolved}
            report = write(folder / 'official-logs' / 'report.json', {spec['task']['id']: raw})
            (folder / 'model.patch').write_text('test patch\n', encoding='utf-8', newline='')   # bytes are bound exactly
            (folder / 'predictions.jsonl').write_text(json.dumps(dict(instance_id=spec['task']['id'],
                model_name_or_path='main', model_patch='test patch\n')) + '\n', encoding='utf-8')
            artifacts = {str(p.relative_to(folder)): dict(path=str(p), sha256=eval_plan.file_sha256(p))
                         for p in (report, folder / 'model.patch', folder / 'predictions.jsonl')}
            separation = dict(agent_image=images['agent']['id'], verifier_image=images['grading']['verifier']['id'],
                              checked_cleanup=True, author_grading=True, author_patch_application=True)
            state = dict(official_report=str(report), official_artifacts=artifacts, separate_verifier=separation)
            write(folder / 'swe-worker-result.json', state)
            result.update(state, grade=dict(resolved=resolved, infra_invalid=False, report=str(report),
                report_sha256=eval_plan.file_sha256(report), instance_id=spec['task']['id'], raw_instance_report=raw))
        write(folder / 'result.json', result)
        jobs.append(dict(id=spec['id'], spec=json.dumps(spec), status='completed', attempt=1, result=json.dumps(result)))
    return plan, jobs, directory


def run(fixture):
    plan, jobs, directory = fixture
    return eval_task_compare.compare(plan, jobs, reference='reference', directory=directory)


def change_result(job, directory, modify):
    value = json.loads(job['result']); modify(value)
    job['result'] = json.dumps(value)
    write(directory / 'jobs' / job['id'] / 'attempt-1' / 'result.json', value)
    return value


def test_tool_runtime_failure_excludes_quality_but_retains_observed_cost(tmp_path):
    plan, jobs, directory = fixture(tmp_path)
    folder = directory / 'jobs' / jobs[1]['id'] / 'attempt-1'
    session = folder / 'sessions' / 'rollout-fixture.jsonl'
    session.parent.mkdir()
    events = [
        dict(type='response_item', payload=dict(type='custom_tool_call', name='exec', call_id='fixture-call')),
        dict(type='response_item', payload=dict(type='custom_tool_call_output', call_id='fixture-call',
             output='failed to spawn code-mode host /cxbin/codex-code-mode-host: No such file or directory (os error 2)')),
    ]
    session.write_text(''.join(json.dumps(row) + '\n' for row in events), encoding='utf-8')
    result = run((plan, jobs, directory))
    candidate = result['candidates'][0]
    assessment = candidate['pairs'][0]['candidate']
    assert not candidate['quality_complete']
    assert candidate['api_cost_complete'] and candidate['costs']['candidate_api_cost_usd'] > 0
    assert assessment['execution_health']['execution_invalid']
    assert any('tool runtime failed to start' in reason for reason in assessment['quality_reasons'])
    assert candidate['metrics']['resolved']['delta_mean'] is None


def test_failed_reflection_is_visible_separately_from_official_task_quality(tmp_path):
    plan, jobs, directory = fixture(tmp_path)
    def failed_reflection(result):
        result['rewrites'].append(dict(type='summary', purpose='reflect', model='reflect', status=400,
                                       completed=False, usage=None))
        result['usage']['summary_calls'] = 1
        Path(result['proxy_log']).write_text(''.join(json.dumps(row) + '\n' for row in result['rewrites']), encoding='utf-8')
    change_result(jobs[1], directory, failed_reflection)
    candidate = run((plan, jobs, directory))['candidates'][0]
    observation = candidate['pairs'][0]['candidate']['mechanism_observation']
    assert candidate['quality_complete'] and not candidate['api_cost_complete']
    assert observation['failed_summaries'] == 1 and observation['completed_summary_purposes'] == {}
    assert 'no method-fidelity' in observation['scope']


def test_complete_verified_pairs_have_boolean_deltas_costs_and_separate_fingerprints(tmp_path):
    data = fixture(tmp_path); result = run(data)
    candidate = result['candidates'][0]
    assert candidate['quality_complete'] and candidate['api_cost_complete']
    assert candidate['pairs'][0]['metrics']['resolved'] == dict(reference=True, candidate=False, comparable=True, delta=-1)
    assert candidate['metrics']['resolved']['delta_mean'] == -1
    assert candidate['costs']['reference_api_cost_usd'] == pytest.approx(.000489)
    assert result['plan_sha256'] == data[0]['sha256']
    assert result['observed_runtime_code_sha256'] == data[0]['code_sha256']
    assert result['analysis_code_sha256'] == eval_plan.fingerprint()
    assert result['analysis_module_sha256'] == eval_plan.file_sha256(eval_task_compare.__file__)
    assert result['analysis_code_sha256'] != result['code_sha256']
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('fault', ['missing', 'failed', 'running', 'pending', 'cancelled', 'retry', 'duplicate', 'changedspec'])
def test_incomplete_pairs_are_visible_and_never_zero_imputed(tmp_path, fault):
    data = fixture(tmp_path); plan, jobs, directory = data
    if fault == 'missing': jobs.pop()
    elif fault in ('failed', 'running', 'pending', 'cancelled'): jobs[-1]['status'] = fault
    elif fault == 'retry': jobs[-1]['attempt'] = 2
    elif fault == 'duplicate': jobs.append(copy.deepcopy(jobs[-1]))
    else:
        spec = json.loads(jobs[-1]['spec']); spec['method']['args'] = {'changed': True}; jobs[-1]['spec'] = json.dumps(spec)
    result = run(data)['candidates'][0]
    assert not result['quality_complete'] and not result['api_cost_complete']
    assert result['coverage']['comparable_pairs'] == 0
    assert result['pairs'][0]['metrics']['resolved']['candidate'] is None
    assert result['metrics']['resolved']['candidate_mean'] is None
    assert result['pairs'][0]['reasons']


def test_pairing_uses_task_repeat_and_label_even_if_jobs_arrive_in_reverse_order(tmp_path):
    data = fixture(tmp_path, repeats=2, tasks=2); plan, jobs, directory = data
    jobs.reverse()
    result = run(data)['candidates'][0]
    assert result['quality_complete'] and len(result['pairs']) == 4
    assert len({(p['task_id'], p['repeat']) for p in result['pairs']}) == 4
    for pair in result['pairs']:
        for label in ('reference', 'candidate'):
            assert pair[label]['job_id'] == next(s['id'] for s in plan['jobs'] if s['label'] == label and
                s['task']['id'] == pair['task_id'] and s['repeat'] == pair['repeat'])


def test_missing_one_repeat_does_not_borrow_another_repeat(tmp_path):
    data = fixture(tmp_path, repeats=2); data[1].pop()
    result = run(data)['candidates'][0]
    assert result['coverage']['comparable_pairs'] == 1
    assert result['metrics']['resolved']['delta_mean'] is None
    assert result['metrics']['resolved']['observed_subset']['delta_mean'] == -1
    assert result['pairs'][1]['metrics']['resolved']['delta'] is None


@pytest.mark.linux_only
def test_milestone_scores_keep_their_scales_denominators_and_never_mix_with_resolved(tmp_path):
    data = fixture(tmp_path, 'swe-milestone'); _, jobs, directory = data
    def modify(result):
        result['grade']['official_metrics']['score_1000'] = 650.
        result['grade']['official_metrics']['score_full'] = .65
        write(Path(result['official_report']), result['grade'])
    change_result(jobs[-1], directory, modify)
    result = run(data)['candidates'][0]
    assert result['quality_complete']
    assert set(result['metrics']) == {'milestone.' + name for name in eval_task_compare.MILESTONE_SCORES}
    assert result['metrics']['milestone.score_1000']['delta_mean'] == -50
    assert result['metrics']['milestone.score_full']['delta_mean'] == pytest.approx(-.05)
    assert result['metrics']['milestone.score_reliable']['delta_mean'] == 0
    assert result['pairs'][0]['reference']['grade_details']['official_metrics']['graded'] == 1


@pytest.mark.linux_only
@pytest.mark.parametrize('fault', ['invalid-flag', 'grade-report-edited', 'report-edited', 'new-report', 'missing-report', 'missing-score'])
def test_milestone_invalid_or_changed_reports_cannot_supply_quality(tmp_path, fault):
    data = fixture(tmp_path, 'swe-milestone'); _, jobs, directory = data
    job = jobs[-1]; value = json.loads(job['result']); grade = value['grade']
    report = Path(grade['reports']['evaluation/M2/evaluation_result.json']['path'])
    if fault == 'invalid-flag':
        change_result(job, directory, lambda v: v['grade'].update(official_metrics_valid=False))
    elif fault == 'grade-report-edited':write(Path(value['official_report']), {})
    elif fault == 'report-edited':write(report, dict(milestone_id='M2', resolved=False, test_summary={'total': 1}))
    elif fault == 'new-report':write(report.parent.parent / 'M2-retry1' / report.name, dict(milestone_id='M2', resolved=True))
    elif fault == 'missing-report':report.unlink()
    else:
        def missing(v):
            del v['grade']['official_metrics']['score_full'];write(Path(v['official_report']), v['grade'])
        change_result(job, directory, missing)
    result = run(data)['candidates'][0]
    assert not result['quality_complete']
    assert result['api_cost_complete']  # grading failure does not erase observed API costs
    assert result['metrics']['milestone.score_full']['candidate_mean'] is None


@pytest.mark.linux_only
def test_valid_unsubmitted_milestones_preserve_author_zero_and_incomplete_coverage(tmp_path):
    data = fixture(tmp_path, 'swe-milestone'); _, jobs, directory = data
    def unsubmitted(v):
        grade = v['grade']; cell = grade['reports'].pop('evaluation/M2/evaluation_result.json');Path(cell['path']).unlink()
        grade['milestones']['M2'] = dict(served_attempt=None, resolved=None)
        grade['raw_summary']['results'] = {}
        path = Path(grade['reports']['evaluation/summary.json']['path']);write(path, grade['raw_summary'])
        grade['reports']['evaluation/summary.json']['sha256'] = eval_plan.file_sha256(path)
        grade['coverage'].update(served_graded=0, unsubmitted=['M1', 'M2'])
        grade.update(scoring_complete=False, submission_complete=False, resolved=None)
        grade['official_metrics'].update({name: 0. for name in eval_task_compare.MILESTONE_SCORES})
        write(Path(v['official_report']), grade)
    change_result(jobs[-1], directory, unsubmitted)
    result = run(data)['candidates'][0]
    assert result['quality_complete']
    pair = result['pairs'][0]
    assert pair['candidate']['grade_details']['submission_complete'] is False
    assert pair['metrics']['milestone.score_1000']['candidate'] == 0  # existing author score, not missing imputation


@pytest.mark.parametrize('fault', ['no-usage', 'no-write', 'no-prices', 'summary-no-price', 'summary-no-usage'])
def test_cost_unknowns_do_not_change_valid_quality(tmp_path, fault):
    data = fixture(tmp_path, prices=fault != 'no-prices'); plan, jobs, directory = data
    def modify(v):
        if fault == 'no-prices':return
        if fault == 'no-usage':v['rewrites'][0].pop('usage')
        elif fault == 'no-write':v['rewrites'][0]['usage'].pop('cache_write_tokens')
        else:
            v['rewrites'].append(dict(type='summary', model='unpriced' if fault == 'summary-no-price' else 'reflect',
                usage=dict(input_tokens=10, cached_tokens=0, cache_write_tokens=0, output_tokens=5) if fault == 'summary-no-price' else None))
            v['usage']['summary_calls'] = 1
        Path(v['proxy_log']).write_text(''.join(json.dumps(row) + '\n' for row in v['rewrites']), encoding='utf-8')
    change_result(jobs[-1], directory, modify)
    result = run(data)['candidates'][0]
    assert result['quality_complete'] and not result['api_cost_complete']
    assert result['pairs'][0]['candidate_api_cost_usd'] is None
    assert result['costs']['candidate_api_cost_usd'] is None
    if fault in ('no-usage', 'no-write', 'summary-no-usage'):
        assert result['pairs'][0]['candidate']['usage']['totals']['api_input_tokens'] is None


def reseal(data):
    plan, jobs, directory = data
    plan['sha256'] = eval_task_compare._digest({k: v for k, v in plan.items() if k != 'sha256'})
    write(directory / 'plan.json', plan)
    manifest_path = directory / 'inputs' / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'));manifest['plan_sha256'] = plan['sha256']
    manifest['sha256'] = eval_task_compare._digest({k: v for k, v in manifest.items() if k != 'sha256'})
    write(manifest_path, manifest)
    for job in jobs:
        folder = directory / 'jobs' / job['id'] / 'attempt-1'
        path = folder / ('milestone-execute-request.json' if plan['config']['benchmark'] == 'swe-milestone' else 'swe-request.json')
        request = json.loads(path.read_text(encoding='utf-8'));settings = request.get('execution', request)
        settings['label'] = plan['sha256'][:12] + '-' + job['id'];write(path, request)


def test_summary_reflection_and_native_compaction_are_priced_by_model_and_request_tier(tmp_path):
    data = fixture(tmp_path); _, jobs, directory = data
    def modify(v):
        v['rewrites'] += [dict(type='summary', model='reflect', response_model='reflect',
                              usage=dict(input_tokens=50, cached_tokens=10, cache_write_tokens=5, output_tokens=8)),
                          dict(type='native_compaction', model='main', response_model='main',
                              usage=dict(input_tokens=80, cached_tokens=20, cache_write_tokens=10, output_tokens=4))]
        v['usage'].update(summary_calls=1, native_compaction_calls=1)
        Path(v['proxy_log']).write_text(''.join(json.dumps(row) + '\n' for row in v['rewrites']), encoding='utf-8')
    change_result(jobs[-1], directory, modify)
    pair = run(data)['candidates'][0]['pairs'][0]
    bill = pair['candidate']['bill']
    assert bill['usage_by_model']['main']['long_context_requests'] == 1
    assert bill['usage_by_model']['main']['native_compaction_calls'] == 1
    assert bill['usage_by_model']['reflect']['summary_calls'] == 1
    expected = .000489 + (50 * 2 + 20 * .1 + 10 * 2.5 + 4 * 10) / 1e6 + (35 * .2 + 10 * .01 + 5 * .25 + 8 * 1) / 1e6
    assert pair['candidate_api_cost_usd'] == pytest.approx(expected)


@pytest.mark.parametrize('fault', ['report', 'patch', 'predictions', 'runtime', 'input', 'model', 'reasoning', 'task',
                                   'dispatch-budget', 'dispatch-resource', 'result-copy', 'no-success', 'test-only', 'grade-test-only', 'no-report-hash'])
def test_real_evidence_binding_refuses_tampering_without_running_grader(tmp_path, fault):
    data = fixture(tmp_path); plan, jobs, directory = data
    job = jobs[-1];v = json.loads(job['result']);folder = directory / 'jobs' / job['id'] / 'attempt-1'
    if fault in ('report', 'patch', 'predictions'):
        path = Path(v['grade']['report']) if fault == 'report' else folder / ('model.patch' if fault == 'patch' else 'predictions.jsonl')
        path.write_text('{}', encoding='utf-8')
    elif fault == 'runtime':(directory / 'runtime' / 'ctxpress' / 'fixture.py').write_text('# modified\n', encoding='utf-8')
    elif fault == 'input':next((directory / 'inputs' / 'task-data').rglob('instances.json*')).write_text('[]', encoding='utf-8')
    elif fault.startswith('dispatch-'):
        path = folder / 'swe-request.json';request = json.loads(path.read_text(encoding='utf-8'))
        if fault == 'dispatch-budget':request['run']['timeout'] += 1
        else:request['grading_image'] = 'sha256:' + '3' * 64
        write(path, request)
    elif fault == 'result-copy':write(folder / 'result.json', {})
    else:
        def modify(value):
            if fault == 'model':value['model'] = 'other'
            elif fault == 'reasoning':value['reasoning'] = 'low'
            elif fault == 'task':value['task']['initial_state']['base_commit'] = 'other'
            elif fault == 'no-success':
                value['rewrites'][0]['status'] = 500
                Path(value['proxy_log']).write_text(json.dumps(value['rewrites'][0]) + '\n', encoding='utf-8')
            elif fault == 'test-only':value['test_only'] = True
            elif fault == 'no-report-hash':value['grade']['report_sha256'] = None
            else:value['grade']['test_only'] = True
        change_result(job, directory, modify)
    result = run(data)['candidates'][0]
    assert not result['quality_complete'] and result['pairs'][0]['reasons']


@pytest.mark.parametrize('scope,mode', [('formal', 'checkpoint'), ('mechanism', 'task_start'), ('benchmark', 'checkpoint')])
def test_boundary_and_mechanism_scope_cannot_enter_task_start_analysis(tmp_path, scope, mode):
    data = fixture(tmp_path);data[0]['config'].update(scope=scope, start_mode=mode)
    with pytest.raises(ValueError, match='benchmark scope and task_start'):run(data)


def test_explicit_reference_is_required_and_must_be_frozen_label(tmp_path):
    plan, jobs, directory = fixture(tmp_path)
    with pytest.raises(TypeError):eval_task_compare.compare(plan, jobs, directory=directory)
    with pytest.raises(ValueError, match='reference'):eval_task_compare.compare(plan, jobs, reference='NoCompaction', directory=directory)


def test_analysis_is_read_only_and_contains_no_original_prompts(tmp_path):
    data = fixture(tmp_path)
    before = {p: p.read_bytes() for p in data[2].rglob('*') if p.is_file()}
    result = run(data)
    assert before == {p: p.read_bytes() for p in data[2].rglob('*') if p.is_file()}
    encoded = json.dumps(result)
    assert 'problem_statement' not in encoded and 'rewrites' not in encoded
    assert result['pairing'] and result['quality_scope']


def test_fixed_eight_families_have_comparison_readers_without_pending_routes():
    from test_eval_families import NAMES
    extra = {name for names in benchmarks.ADDITIONAL_FAMILIES.values() for name in names}
    assert set(eval_task_compare.SUPPORTED) == set(NAMES) | extra             # official Harbor adapters reuse the Harbor reader
    assert set(eval_task_compare.UNSUPPORTED) == {'browsecomp-plus'}         # author-agent family, reader pending


@pytest.mark.parametrize('artifact', ['plan.json', 'inputs/manifest.json', 'runtime/ctxpress/fixture.py'])
def test_frozen_run_credential_alias_is_rejected_before_any_read(tmp_path, monkeypatch, artifact):
    data = fixture(tmp_path); directory = data[2]
    credentials = tmp_path / 'auth.json'; credentials.write_text('synthetic forbidden credential', encoding='utf-8')
    target = directory / artifact; target.unlink(); target.symlink_to(credentials)
    original = Path.read_bytes
    def guard(path):
        if path.resolve().name == 'auth.json':
            raise AssertionError('comparison attempted to read credentials')
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', guard)
    out = run(data)
    assert out['coverage']['admissible_jobs'] == 0 and out['coverage']['cost_jobs'] == 0
    assert all('unverified frozen run' in job['reasons'][0] for job in out['jobs'])


@pytest.mark.linux_only
def test_stable_bigcode_reader_is_routed_through_common_accounting_and_quality(tmp_path, monkeypatch):
    from test_eval_code_compare import fixture as code_fixture
    plan, jobs, directory, paths = code_fixture(tmp_path)
    plan['config']['prices'] = {'models': {'main': rates()}}
    def forbidden(*args):
        raise AssertionError('code samples were routed to repository Boolean grading')
    monkeypatch.setattr(eval_task_compare, '_verified', forbidden)
    row = eval_task_compare._assessment(plan, jobs[0]['spec'], [jobs[0]], directory, paths, [], True)
    assert row['quality_admissible'] and row['api_cost_complete']
    assert row['metrics'] == {'code.sample_passed': True}
    assert row['grade_details']['resolved'] is None and row['grade_details']['author_pass_at_k'] is None


def bigcode_comparison_fixture(tmp_path, statuses=('pass', 'fail'), labels=('reference', 'candidate')):
    """Complete frozen-run wrapper around the offline BigCode artifact fixture."""
    import shutil
    from ctxpress.harness.jobs import environment as eval_environment
    from test_eval_code_compare import fixture as code_fixture, refresh, store_cohort
    plan, jobs, directory, paths = code_fixture(tmp_path, statuses)
    original_jobs = list(jobs)
    for index, label in enumerate(labels[1:], 1):
        for original in original_jobs:
            job = copy.deepcopy(original)
            job['id'] = job['spec']['id'] = f'm{index:03}-t000-r{job["spec"]["repeat"]:03}'
            job['spec']['label'] = label
            old_folder = directory / 'jobs' / original['id'] / 'attempt-1'
            folder = directory / 'jobs' / job['id'] / 'attempt-1'
            shutil.copytree(old_folder, folder)
            # Relocate only this fixture attempt's recorded absolute paths.
            old = json.dumps(str(old_folder))[1:-1]; new = json.dumps(str(folder))[1:-1]
            for path in folder.rglob('*'):
                if path.is_file() and path.suffix in ('.json', '.jsonl'):
                    path.write_text(path.read_text(encoding='utf-8').replace(old, new), encoding='utf-8')
            job['result'] = json.loads(json.dumps(job['result']).replace(old, new))
            jobs.append(job); plan['jobs'].append(job['spec'])
    plan['config'].update(scope='benchmark', prices={'models': {'main': rates()}})
    for spec in plan['jobs']:
        spec['method'] = dict(spec['method'], label=spec['label'])
    plan['config']['methods'] = [next(s['method'] for s in plan['jobs'] if s['label'] == label) for label in labels]
    lock = plan['task_resources']
    trees = {}
    for key, tree in lock['trees'].items():
        trees[key] = dict(eval_environment.workspace(directory / 'inputs/official-inputs' / key), root=tree['root'])
    lock = task_resources.seal({k: v for k, v in lock.items() if k not in ('sha256', 'trees')} | {'trees': trees})
    plan['task_resources'] = lock
    resource = plan['config']['environment']['resources']
    copied_resource = write(Path(paths[resource]), lock)
    digest = eval_plan.file_sha256(copied_resource); plan['artifacts'][resource] = digest
    target = directory / 'inputs/artifacts' / (digest + '.json')
    copied_resource.rename(target); paths[resource] = str(target)
    plan['input_trees'] = {'official:' + key: dict(tree=tree, folder='official-inputs/' + key, environment_key=None)
                           for key, tree in trees.items()}
    data_root = str(Path(plan['jobs'][0]['task']['inputs'][0]['path']).parent)
    plan['input_trees']['task-data'] = dict(tree=dict(eval_environment.workspace(directory / 'inputs/task-data'),
        root=data_root), folder='task-data', environment_key=None)
    runtime = directory / 'runtime/ctxpress'; runtime.mkdir(parents=True)
    (runtime / 'fixture.py').write_text('# frozen offline runtime; never executed\n', encoding='utf-8')
    plan.update(schema='ctxpress.eval.plan', version=1, input_snapshot_version=1,
                benchmark=benchmarks.get('bigcodebench').describe(), missing_environment_files=[],
                code_sha256=eval_plan.fingerprint(runtime))
    plan['sha256'] = eval_task_compare._digest({k: v for k, v in plan.items() if k != 'sha256'})
    write(directory / 'plan.json', plan)
    files = {source: dict(path=Path(path).relative_to(directory / 'inputs').as_posix(), sha256=plan['artifacts'][source])
             for source, path in paths.items()}
    write(directory / 'inputs/manifest.json', eval_inputs._sealed(dict(schema=eval_inputs.SCHEMA, version=1,
                                                                     plan_sha256=plan['sha256'], files=files)))
    for job in jobs:
        spec = job['spec']; folder = directory / 'jobs' / job['id'] / 'attempt-1'
        request_path = folder / 'swe-request.json'; request = json.loads(request_path.read_text(encoding='utf-8'))
        label = plan['sha256'][:12] + '-' + job['id']
        project = 'ctxp-sw-' + eval_task_compare._digest(job['id'])[:24]
        request.update(method=spec['method'], label=label, project=project); write(request_path, request)
        job['result'].update(method=spec['method'], grading_run_id=project)
        state_path = folder / 'swe-worker-result.json'; state = json.loads(state_path.read_text(encoding='utf-8'))
        state['grading_run_id'] = project; write(state_path, state)
        for role in ('agent', 'verifier'):
            path = folder / ('resources-swe-' + role + '.json'); owner = json.loads(path.read_text(encoding='utf-8'))
            owner.update(label=label, project=project, container=project + ('-agent' if role == 'agent' else '-grade'),
                         container_id=eval_task_compare._digest(job['id'] + role))
            write(path, owner)
        path = folder / 'official-logs/outputs/code-resources.json'; proof = json.loads(path.read_text(encoding='utf-8'))
        proof['project'] = project; write(path, proof)
        refresh(job, directory)
    groups = []
    for label in labels:
        members = [job for job in jobs if job['spec']['label'] == label]
        report = store_cohort((plan, members, directory, paths))
        group = report['methods'][0]; group['method'] = label; groups.append(group)
    report['methods'] = groups
    write(directory / 'report.json', report)
    # These are actual verification calls, not mocks of the frozen-input gate.
    eval_plan.verify(plan, check_inputs=False)
    assert eval_inputs.verify(plan, directory / 'inputs') == paths
    return plan, jobs, directory


@pytest.mark.linux_only
def test_bigcode_stored_cohorts_are_exposed_per_label_without_scoring_or_writes(tmp_path, monkeypatch):
    import subprocess
    from ctxpress.benchmarks.bigcode import report as code_report
    data = bigcode_comparison_fixture(tmp_path); plan, jobs, directory = data
    report = json.loads((directory / 'report.json').read_text(encoding='utf-8'))
    before = {p.relative_to(directory): (eval_plan.file_sha256(p), p.stat().st_mtime_ns)
              for p in directory.rglob('*') if p.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError('analysis must not regenerate a report, score, write, or launch a process')
    monkeypatch.setattr(code_report, 'summarize', forbidden)
    monkeypatch.setattr(artifact_io, 'atomic_json', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    out = eval_task_compare.compare(plan, iter(jobs), reference='reference', directory=directory)
    assert set(out['author_code_cohorts']) == {'reference', 'candidate'}
    for group in report['methods']:
        block = out['author_code_cohorts'][group['method']]
        assert block['available'] is True and block['reasons'] == []
        assert block['cohort']['metrics'] == group['code_metrics']
        assert block['cohort']['metrics']['pass_at_k'] == {'1': .5, '5': None, '10': None}
        assert any(row['path'] == str(directory / 'report.json') and row['sha256'] ==
                   eval_plan.file_sha256(directory / 'report.json') for row in block['artifacts'])
    assert out['metric_names'] == ['code.sample_passed']
    assert out['candidates'][0]['quality_complete'] and out['candidates'][0]['api_cost_complete']
    assert all(row['grade_details']['author_pass_at_k'] is None for row in out['jobs'])
    assert before == {p.relative_to(directory): (eval_plan.file_sha256(p), p.stat().st_mtime_ns)
                      for p in directory.rglob('*') if p.is_file()}


@pytest.mark.linux_only
def test_bigcode_missing_report_preserves_single_sample_and_cost_without_inventing_pass_at_k(tmp_path):
    data = bigcode_comparison_fixture(tmp_path, statuses=('pass',), labels=('reference',))
    baseline = run(data)
    (data[2] / 'report.json').unlink()
    out = run(data); block = out['author_code_cohorts']['reference']
    assert block['available'] is False and block['cohort'] is None
    assert block['reasons'] == ['stored author cohort report is missing']
    assert block['artifacts'] == []
    assert out['jobs'] == baseline['jobs'] and out['candidates'] == baseline['candidates']
    assert out['coverage']['admissible_jobs'] == 1 and out['coverage']['cost_jobs'] == 1


@pytest.mark.linux_only
@pytest.mark.parametrize('fault', ['plan', 'model', 'invalid-json', 'incomplete', 'changed-score', 'missing-member', 'test-only'])
def test_bigcode_invalid_stored_cohort_cannot_change_sample_or_cost_admission(tmp_path, fault):
    data = bigcode_comparison_fixture(tmp_path); baseline = run(data)
    path = data[2] / 'report.json'; report = json.loads(path.read_text(encoding='utf-8')); group = report['methods'][1]
    if fault == 'plan': report['plan_sha256'] = '0' * 64
    elif fault == 'model': report['model'] = 'other'
    elif fault == 'incomplete': group['code_metrics']['complete'] = False
    elif fault == 'changed-score': group['code_metrics']['pass_at_k']['1'] = 1.
    elif fault == 'missing-member': group['jobs'].pop()
    elif fault == 'test-only': group['test_only'] = True
    write(path, report)
    if fault == 'invalid-json': path.write_text('{', encoding='utf-8')
    out = run(data); block = out['author_code_cohorts']['candidate']
    assert block['available'] is False and block['cohort'] is None and block['reasons']
    assert out['jobs'] == baseline['jobs'] and out['candidates'] == baseline['candidates']
    assert out['coverage'] == baseline['coverage']
    if fault not in ('plan', 'model', 'invalid-json'):
        assert out['author_code_cohorts']['reference']['available'] is True


@pytest.mark.linux_only
@pytest.mark.parametrize('fault', ['runtime', 'input', 'missing-job', 'tool-failure', 'report-hash'])
def test_bigcode_cohort_requires_common_frozen_and_sample_assessment_gates(tmp_path, monkeypatch, fault):
    data = bigcode_comparison_fixture(tmp_path); plan, jobs, directory = data
    job = jobs[-1]; folder = directory / 'jobs' / job['id'] / 'attempt-1'
    if fault == 'runtime': (directory / 'runtime/ctxpress/fixture.py').write_text('# changed\n', encoding='utf-8')
    elif fault == 'input': (directory / 'inputs/task-data/instances.jsonl').write_text('{}', encoding='utf-8')
    elif fault == 'missing-job': jobs.pop()
    elif fault == 'report-hash': (folder / 'official-logs/outputs/sample-result.json').write_text('{}', encoding='utf-8')
    else:
        job['result']['execution_health'] = dict(execution_invalid=True, tool_calls=1, tool_outputs=1,
            tool_runtime_failures=[dict(kind='code_mode_host_spawn_failure', call_id='fixture', error='missing helper')])
        write(folder / 'result.json', job['result'])
    original = eval_task_compare.eval_code_compare.author_cohort; called = []
    def observe(*args, **kwargs):
        called.append(kwargs['label'])
        return original(*args, **kwargs)
    monkeypatch.setattr(eval_task_compare.eval_code_compare, 'author_cohort', observe)
    out = run(data); block = out['author_code_cohorts']['candidate']
    assert block['available'] is False and block['cohort'] is None and block['reasons']
    assert 'candidate' not in called
    if fault in ('runtime', 'input'):
        assert called == [] and 'unverified frozen run' in block['reasons'][0]
    else:
        assert out['author_code_cohorts']['reference']['available'] is True
    if fault in ('tool-failure', 'report-hash'):
        assert out['coverage']['cost_jobs'] == 4 and out['coverage']['admissible_jobs'] == 3


@pytest.mark.linux_only
def test_bigcode_cohort_availability_does_not_require_complete_api_cost(tmp_path):
    data = bigcode_comparison_fixture(tmp_path); job = data[1][-1]; result = job['result']
    result['rewrites'][0]['usage'].pop('cache_write_tokens')
    Path(result['proxy_log']).write_text(json.dumps(result['rewrites'][0]) + '\n', encoding='utf-8')
    write(data[2] / 'jobs' / job['id'] / 'attempt-1/result.json', result)
    out = run(data)
    assert out['author_code_cohorts']['candidate']['available'] is True
    assert out['coverage']['admissible_jobs'] == 4 and out['coverage']['cost_jobs'] == 3


def test_other_families_do_not_get_author_code_cohort_blocks(tmp_path):
    assert 'author_code_cohorts' not in run(fixture(tmp_path))


@pytest.mark.linux_only
@pytest.mark.parametrize('resolved,tool_failure', [(True, False), (False, False), (False, True)])
def test_stable_poly_reader_routes_author_outcome_and_retains_failed_tool_cost(tmp_path, monkeypatch, resolved, tool_failure):
    from test_eval_poly_compare import fixture as poly_fixture
    plan, spec, result, folder, directory, paths = poly_fixture(tmp_path, resolved=resolved)
    if tool_failure:
        result['execution_health'] = dict(execution_invalid=True, tool_calls=1, tool_outputs=1,
            tool_runtime_failures=[dict(kind='code_mode_host_spawn_failure', call_id='fixture', error='missing helper')])
        write(folder / 'result.json', result)
    def forbidden(*args):
        raise AssertionError('Poly outcome was routed to Verified grading')
    monkeypatch.setattr(eval_task_compare, '_verified', forbidden)
    job = dict(id=spec['id'], spec=spec, result=result, status='completed', attempt=1)
    row = eval_task_compare._assessment(plan, spec, [job], directory, paths, [], True)
    assert row['api_cost_complete'] and row['api_cost_usd'] > 0
    assert row['quality_admissible'] is not tool_failure
    assert row['metrics'] == ({} if tool_failure else {'repository.resolved': resolved})


def test_single_reference_pilot_has_job_diagnostics_but_no_invented_comparison(tmp_path):
    data = fixture(tmp_path);plan, jobs, _ = data
    plan['jobs'] = plan['jobs'][:1];jobs[:] = jobs[:1]
    reseal(data)
    result = run(data)
    assert result['coverage']['admissible_jobs'] == 1
    assert result['candidates'] == []


def test_changed_pair_key_with_planned_id_is_a_changed_spec_not_a_new_valid_task(tmp_path):
    data = fixture(tmp_path);_, jobs, _ = data
    spec = json.loads(jobs[-1]['spec']);spec['repeat'] = 99;jobs[-1]['spec'] = json.dumps(spec)
    result = run(data)['candidates'][0]
    assert len(result['pairs']) == 1
    assert 'changed job specification' in result['pairs'][0]['candidate']['reasons']


def test_unplanned_duplicate_identity_poisoning_is_not_silently_deduplicated(tmp_path):
    data = fixture(tmp_path);_, jobs, _ = data
    extra = copy.deepcopy(jobs[-1]);extra['id'] = 'unplanned';jobs.append(extra)
    result = run(data)['candidates'][0]
    assert 'duplicate cohort member' in result['pairs'][0]['candidate']['reasons']


def test_prior_attempt_directory_is_not_silently_ignored(tmp_path):
    data = fixture(tmp_path);_, jobs, directory = data
    (directory / 'jobs' / jobs[-1]['id'] / 'attempt-2').mkdir()
    result = run(data)['candidates'][0]
    assert not result['quality_complete']
    assert 'retry or missing attempt directory' in result['pairs'][0]['candidate']['reasons']


@pytest.mark.parametrize('fault', ['metadata-image', 'metadata-model', 'metadata-reasoning'])
def test_native_actual_execution_metadata_must_match_frozen_resources(tmp_path, fault):
    data = fixture(tmp_path, 'swe-milestone');_, jobs, directory = data
    path = directory / 'jobs' / jobs[-1]['id'] / 'attempt-1' / 'trial' / 'trial_metadata.json'
    metadata = json.loads(path.read_text(encoding='utf-8'))
    metadata[{'metadata-image': 'image', 'metadata-model': 'model', 'metadata-reasoning': 'reasoning_effort'}[fault]] = 'changed'
    write(path, metadata)
    result = run(data)['candidates'][0]
    assert not result['quality_complete'] and result['api_cost_complete']


def test_official_report_path_cannot_read_auth_or_leave_its_attempt(tmp_path, monkeypatch):
    data = fixture(tmp_path);_, jobs, directory = data
    outside = tmp_path / 'auth.json';outside.write_text('must not read', encoding='utf-8')
    def modify(v):v['grade']['report'] = str(outside)
    change_result(jobs[-1], directory, modify)
    original = Path.read_bytes
    def guard(path):
        assert path != outside
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', guard)
    result = run(data)['candidates'][0]
    assert not result['quality_complete']
    assert 'credential files' in result['pairs'][0]['candidate']['quality_reasons'][0]



def test_frozen_same_task_id_cannot_hide_different_start_states_across_methods(tmp_path):
    data = fixture(tmp_path);plan, _, _ = data
    plan['jobs'][0]['task'] = copy.deepcopy(plan['jobs'][0]['task'])
    plan['jobs'][0]['task']['initial_state']['base_commit'] = 'other'
    plan['sha256'] = eval_task_compare._digest({k: v for k, v in plan.items() if k != 'sha256'})
    with pytest.raises(ValueError, match='task digest|identical task'):run(data)


def test_changed_plan_hash_is_rejected_before_any_artifact_read(tmp_path, monkeypatch):
    data = fixture(tmp_path);data[0]['config']['model'] = 'changed'
    monkeypatch.setattr(eval_task_compare, '_read', lambda *args, **kwargs: pytest.fail('corrupt plan reached artifact reads'))
    with pytest.raises(ValueError, match='plan has changed'):run(data)



@pytest.mark.parametrize('fault', ['boolean-denominator', 'duplicate-unsubmitted', 'scoring-coverage', 'submission-coverage'])
def test_native_counter_and_coverage_flags_must_be_consistent(tmp_path, fault):
    data = fixture(tmp_path, 'swe-milestone');_, jobs, directory = data
    def modify(v):
        grade = v['grade']
        if fault == 'boolean-denominator':grade['official_metrics']['graded'] = True
        elif fault == 'duplicate-unsubmitted':grade['coverage']['unsubmitted'] = ['M1', 'M1']
        elif fault == 'scoring-coverage':grade['scoring_complete'] = False
        else:grade['submission_complete'] = False
        write(Path(v['official_report']), grade)
    change_result(jobs[-1], directory, modify)
    result = run(data)['candidates'][0]
    assert not result['quality_complete'] and result['api_cost_complete']



def test_legacy_static_prices_cannot_hide_missing_cache_write_observation(tmp_path):
    data = fixture(tmp_path);plan, jobs, directory = data
    plan['config']['prices']['models']['main'].pop('cache_write')
    plan['config']['prices']['models']['main'].pop('long_context')
    reseal(data)
    def modify(v):
        v['rewrites'][0]['usage'].pop('cache_write_tokens')
        Path(v['proxy_log']).write_text(json.dumps(v['rewrites'][0]) + '\n', encoding='utf-8')
    change_result(jobs[-1], directory, modify)
    result = run(data)['candidates'][0]
    assert result['quality_complete'] and not result['api_cost_complete']
    cost = result['pairs'][0]['candidate']['bill']
    assert not cost['api_cost_complete'] and cost['api_cost_at_declared_rates_usd'] is None
    assert cost['usage_by_model']['main']['api_cost_at_declared_rates_usd'] is None
