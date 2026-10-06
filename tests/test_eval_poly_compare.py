"""Synthetic offline reader contracts, never model execution or benchmark scores."""
import copy
import json
from pathlib import Path

import pytest

from ctxpress.benchmarks.poly_bench import PolyBench
from ctxpress.harness import eval_inputs, eval_plan, eval_poly_compare, eval_trees, task, task_resources


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding='utf-8')
    return path


def fixture(tmp_path, *, resolved=True, patch='captured candidate patch\n'):
    data = tmp_path / 'source-data'
    row = dict(instance_id='owner__repo-1', repo='owner/repo', base_commit='a' * 40,
        language='Python', task_category='Bug Fix', problem_statement='Synthetic contract problem',
        patch='author reference patch', test_patch='author test patch', Dockerfile='FROM frozen:fixture',
        F2P="['regression']", P2P="['unchanged']", test_command='original-test-command', modified_nodes='[]')
    write(data / 'dataset_manifest.json', dict(dataset='AmazonScience/SWE-PolyBench_Verified', revision='fixture-v1'))
    (data / 'instances.jsonl').write_text(json.dumps(row) + '\n', encoding='utf-8')
    original = PolyBench().task_instances(data)[0]
    trees = {}
    files = {'polybench': {'pyproject.toml': '[project]\nname="poly_bench_evaluation"\nversion="0.1.0"\n',
        **{'src/poly_bench_evaluation/' + name: '# inert frozen file; never imported\n' for name in
           ('__init__.py', 'run_evaluation.py', 'polybench_data.py', 'docker_utils.py', 'scoring.py', 'constants.py', 'parsers/__init__.py')}},
        'dependencies': {'poly_bench_evaluation-0.1.0.dist-info/METADATA': 'Name: poly_bench_evaluation\nVersion: 0.1.0\n'}}
    for key, records in files.items():
        source = tmp_path / 'original-official' / key
        for name, text in records.items():
            p = source / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text, encoding='utf-8')
        trees[key] = eval_trees.capture(source, list(records), folder='unused')['tree']
    images = dict(agent={'id': 'sha256:' + '1' * 64}, grading={'verifier': {'id': 'sha256:' + '2' * 64}})
    resource = dict(task_sha256=task_resources.task_digest(original), images=images)
    runtime = dict(python='/frozen/python3.12', sha256='3' * 64, version='3.12.11 frozen fixture', platform='linux')
    lock = task_resources.seal(dict(schema=task_resources.SCHEMA, version=1, benchmark='swe-polybench',
        release='fixture-v1', tasks={original['id']: resource}, trees=trees, runtime=runtime))
    source_lock = write(tmp_path / 'original-resources.json', lock)
    binary = tmp_path / 'original-bin'; binary.mkdir()
    for name in ('codex', 'codex-code-mode-host'):
        (binary / name).write_bytes(b'inert binary fixture; never executed')
    spec = dict(id='reference-fixture', task=original, method={'class': 'NoCompaction'}, compact_limit=1000,
                resources=resource, repeat=0)
    config = dict(benchmark='swe-polybench', start_mode='task_start', model='main', reasoning='medium', repeats=1,
        run=dict(max_calls=4, timeout=60, grade=True), environment=dict(data=str(data), bindir=str(binary), resources=str(source_lock)),
        prices={'models': {'main': dict(input=2, cached=.1, cache_write=2.5, output=10, unit='USD_per_million_tokens',
                                       source='offline accounting fixture', as_of='2026-10-05')}})
    artifacts = {item['path']: item['sha256'] for item in original['inputs']}
    artifacts[str(source_lock)] = eval_plan.file_sha256(source_lock)
    for p in binary.iterdir(): artifacts[str(p)] = eval_plan.file_sha256(p)
    for tree in trees.values():
        artifacts.update({str(Path(tree['root']) / name): digest for name, digest in tree['files'].items()})
    plan = dict(config=config, jobs=[spec], artifacts=artifacts, sha256='b' * 64, task_resources=lock,
        input_trees={'official:' + key: dict(tree=tree, folder='official-inputs/' + key, environment_key=None)
                     for key, tree in trees.items()})
    directory = tmp_path / 'run'; directory.mkdir(); (directory / 'runtime').mkdir()
    paths = eval_inputs.prepare(plan, directory / 'inputs')
    moved = task.remap(original, paths)
    folder = directory / 'jobs' / spec['id'] / 'attempt-1'; folder.mkdir(parents=True)
    project = 'ctxp-sw-' + 'c' * 24
    request = dict(schema='ctxpress.eval.swe_trial', version=1, project=project, api='polybench',
        task=moved, method=spec['method'], compact_limit=1000, label=plan['sha256'][:12] + '-' + spec['id'],
        model='main', reasoning='medium', run=config['run'], runtime=runtime, folder=str(folder),
        package=str(directory / 'runtime'), official=str(directory / 'inputs/official-inputs'),
        profiles=str(folder / 'method-inputs'), bindir=str(directory / 'inputs/bin'), binary_version='0.159.2',
        upstream='https://chatgpt.com/backend-api/codex', target='chatgpt.com:443', via=None, verifier_cap_add=[],
        agent_image=images['agent']['id'], grading_image=images['grading']['verifier']['id'])
    write(folder / 'swe-request.json', request)
    proxy = folder / 'agent/ctxpress-requests.jsonl'; proxy.parent.mkdir()
    rows = [dict(request=1, status=200, model='main',
                 usage=dict(input_tokens=12, cached_tokens=2, cache_write_tokens=0, output_tokens=1))]
    proxy.write_text(json.dumps(rows[0]) + '\n', encoding='utf-8')
    (folder / 'model.patch').write_text(patch, encoding='utf-8')
    predictions = dict(instance_id=original['id'], model_name_or_path='main', model_patch=patch)
    (folder / 'predictions.jsonl').write_text(json.dumps(predictions) + '\n', encoding='utf-8')
    generated = bool(patch.strip())
    raw = dict(instance_id=original['id'], patch_applied=generated, generation=generated, with_logs=generated,
        all_f2p_passed=resolved if generated else False, no_p2p_failed=generated, resolved=resolved if generated else False,
        passed_tests=['regression', 'unchanged'] if generated and resolved else ['unchanged'] if generated else [],
        failed_tests=['regression'] if generated and not resolved else [])
    report = write(folder / 'official-logs' / (original['id'] + '_result.json'), raw)
    if generated:
        p = folder / 'official-logs/run_logs_python' / (original['id'] + '_run.log')
        p.parent.mkdir(); p.write_text('inert original test log fixture\n', encoding='utf-8')
    separation = dict(agent_image=request['agent_image'], verifier_image=request['grading_image'],
        checked_cleanup=True, author_grading=True, author_patch_application=True)
    records = {p.relative_to(folder).as_posix(): dict(path=str(p), sha256=eval_plan.file_sha256(p))
        for p in [folder / 'model.patch', folder / 'predictions.jsonl', *(folder / 'official-logs').rglob('*')] if p.is_file()}
    state = dict(stop='completed', calls=1, agent_exception=None, official_report=str(report), official_artifacts=records,
        separate_verifier=separation, protocol='ctxpress_comparison', network_policy='no_tool_network_model_socket_only',
        grading_run_id=project, swe_api='polybench')
    write(folder / 'swe-worker-result.json', state)
    for role in ('agent', 'verifier'):
        owner = dict(schema='ctxpress.eval.swe_resources', version=1, project=project, label=request['label'], role=role,
            container=project + ('-agent' if role == 'agent' else '-grade'), image=request['agent_image' if role == 'agent' else 'grading_image'],
            daemon_id='same-fixture-daemon', cleaned=True, credentials_may_exist=False, phase='stopped',
            channel='/tmp/' + project + '-channel-fixture' if role == 'agent' else None)
        if role == 'agent' or generated: owner['container_id'] = ('4' if role == 'agent' else '5') * 64
        write(folder / ('resources-swe-' + role + '.json'), owner)
    grade = PolyBench().read_grade(moved, report)
    result = dict(task=moved, method=spec['method'], model='main', reasoning='medium', benchmark={'name': 'swe-polybench'},
        requests=1, rewrites=rows, proxy_log=str(proxy), grade=grade, binary_version='0.159.2',
        usage=dict(summary_calls=0, native_compaction_calls=0), **state)
    write(folder / 'result.json', result)
    return plan, spec, result, folder, directory, paths


def check(data):
    plan, spec, result, folder, directory, paths = data
    evidence = []
    eval_poly_compare.binding(plan, spec, result, folder, directory, paths, evidence)
    metrics, details = eval_poly_compare.quality(spec, result, folder, evidence)
    return metrics, details, evidence


def change(path, mutate):
    document = json.loads(path.read_text(encoding='utf-8')); mutate(document); write(path, document)


def refresh(data):
    """Keep producer receipts coherent when testing semantic, not hash, faults."""
    _, _, result, folder, _, _ = data
    records = result['official_artifacts']
    for row in records.values(): row['sha256'] = eval_plan.file_sha256(row['path'])
    change(folder / 'swe-worker-result.json', lambda d: d.update(official_artifacts=records))
    result['grade']['report_sha256'] = eval_plan.file_sha256(result['grade']['report'])


@pytest.mark.linux_only
@pytest.mark.parametrize('resolved', [True, False])
def test_preserves_author_boolean_and_retains_original_evidence(tmp_path, resolved):
    data = fixture(tmp_path, resolved=resolved)
    metrics, details, evidence = check(data)
    assert metrics == {'repository.resolved': resolved} and type(metrics['repository.resolved']) is bool
    assert details['independent_grading'] and details['retrieval_metrics_supported'] is False
    assert details['author_interface'] == 'poly_bench_evaluation.evaluate_instance'
    assert details['raw_instance_report'] == data[2]['grade']['raw_instance_report']
    assert {Path(r['path']).name for r in evidence} >= {'swe-request.json', 'model.patch', 'predictions.jsonl',
        'resources-swe-agent.json', 'resources-swe-verifier.json', 'owner__repo-1_result.json', 'METADATA', 'codex-code-mode-host'}
    json.dumps((metrics, details, evidence), allow_nan=False)


@pytest.mark.linux_only
def test_real_model_empty_patch_is_official_false_without_a_verifier_container(tmp_path):
    data = fixture(tmp_path, patch=' \n')
    assert check(data)[0] == {'repository.resolved': False}


@pytest.mark.parametrize('location', ['result', 'grade', 'request', 'state', 'report', 'owner', 'proxy'])
def test_test_only_evidence_is_not_actual_quality(tmp_path, location):
    data = fixture(tmp_path); _, spec, result, folder, _, _ = data
    if location == 'result': result['test_only'] = True
    elif location == 'grade': result['grade']['test_only'] = True
    elif location == 'proxy':
        result['rewrites'][0]['test_only'] = True
        Path(result['proxy_log']).write_text(json.dumps(result['rewrites'][0]) + '\n', encoding='utf-8')
    else:
        path = {'request': folder / 'swe-request.json', 'state': folder / 'swe-worker-result.json',
                'report': Path(result['grade']['report']), 'owner': folder / 'resources-swe-verifier.json'}[location]
        change(path, lambda d: d.update(test_only=True))
        if location == 'report': refresh(data)
    with pytest.raises(ValueError): eval_poly_compare.quality(spec, result, folder, [])


@pytest.mark.parametrize('fault', ['no_requests', 'no_success', 'wrong_model', 'zero_model_diagnostic', 'proxy_drift'])
def test_no_model_gold_diagnostic_cannot_enter_quality(tmp_path, fault):
    data = fixture(tmp_path); _, spec, result, folder, _, _ = data
    if fault == 'no_requests': result['requests'] = 0
    if fault == 'no_success': result['rewrites'][0]['status'] = 500
    if fault == 'wrong_model': result['rewrites'][0]['model'] = 'other'
    if fault == 'zero_model_diagnostic': result['model_calls'] = 0
    if fault == 'proxy_drift': Path(result['proxy_log']).write_text('{}\n', encoding='utf-8')
    with pytest.raises(ValueError): eval_poly_compare.quality(spec, result, folder, [])


@pytest.mark.parametrize('fault', ['report_drift', 'patch_drift', 'prediction_drift', 'log_drift', 'added_artifact',
    'missing_artifact', 'prediction_model', 'prediction_task', 'prediction_patch', 'state_image', 'request_image',
    'state_api', 'state_protocol', 'state_calls', 'state_run', 'cleanup', 'credentials', 'daemon', 'same_container',
    'missing_container', 'verifier_channel', 'owner_image', 'owner_label', 'grade_bool', 'report_bool', 'infra_grade',
    'infra_report', 'report_path', 'artifact_alias', 'artifact_symlink', 'tool_failure', 'changed_dataset'])
def test_changed_or_invalid_author_chain_is_rejected(tmp_path, fault):
    data = fixture(tmp_path); plan, spec, result, folder, directory, paths = data
    report = Path(result['grade']['report'])
    if fault in ('report_drift', 'patch_drift', 'prediction_drift', 'log_drift'):
        p = {'report_drift': report, 'patch_drift': folder / 'model.patch', 'prediction_drift': folder / 'predictions.jsonl',
             'log_drift': next((folder / 'official-logs/run_logs_python').iterdir())}[fault]
        p.write_text(p.read_text(encoding='utf-8') + ' ', encoding='utf-8')
    if fault == 'added_artifact': write(folder / 'official-logs/extra.json', {})
    if fault == 'missing_artifact': report.unlink()
    if fault in ('prediction_model', 'prediction_task', 'prediction_patch'):
        key = {'prediction_model': 'model_name_or_path', 'prediction_task': 'instance_id', 'prediction_patch': 'model_patch'}[fault]
        change(folder / 'predictions.jsonl', lambda d: d.update({key: 'other'})); refresh(data)
    if fault == 'state_image': change(folder / 'swe-worker-result.json', lambda d: d['separate_verifier'].update(verifier_image='sha256:' + '9' * 64))
    if fault == 'request_image': change(folder / 'swe-request.json', lambda d: d.update(grading_image='sha256:' + '9' * 64))
    if fault in ('state_api', 'state_protocol', 'state_calls', 'state_run'):
        key = {'state_api': 'swe_api', 'state_protocol': 'protocol', 'state_calls': 'calls', 'state_run': 'grading_run_id'}[fault]
        change(folder / 'swe-worker-result.json', lambda d: d.update({key: 'other'}))
    if fault in ('cleanup', 'credentials', 'daemon', 'same_container', 'missing_container', 'verifier_channel', 'owner_image', 'owner_label'):
        key, value = {'cleanup': ('cleaned', False), 'credentials': ('credentials_may_exist', True), 'daemon': ('daemon_id', 'other'),
            'same_container': ('container_id', '4' * 64), 'missing_container': ('container_id', None),
            'verifier_channel': ('channel', '/unexpected'), 'owner_image': ('image', 'sha256:' + '9' * 64), 'owner_label': ('label', 'other')}[fault]
        change(folder / 'resources-swe-verifier.json', lambda d: d.update({key: value}))
    if fault == 'grade_bool': result['grade']['resolved'] = 1
    if fault in ('report_bool', 'infra_report'):
        change(report, lambda d: d.update({'resolved': 1} if fault == 'report_bool' else {'infra_failure': True})); refresh(data)
    if fault == 'infra_grade': result['grade']['infra_invalid'] = True
    if fault == 'report_path': result['grade']['report'] = str(folder / 'wrong.json')
    if fault == 'artifact_alias': result['official_artifacts']['model.patch']['path'] = str(folder / 'predictions.jsonl')
    if fault == 'artifact_symlink':
        p = folder / 'model.patch'; target = folder / 'patch-copy'; target.write_bytes(p.read_bytes()); p.unlink(); p.symlink_to(target)
    if fault == 'tool_failure':
        result['execution_health'] = dict(execution_invalid=True, tool_calls=1, tool_outputs=1,
            tool_runtime_failures=[dict(kind='code_mode_host_spawn_failure', call_id='x', error='missing companion')])
    if fault == 'changed_dataset': Path(paths[spec['task']['inputs'][0]['path']]).write_text('{}\n', encoding='utf-8')
    with pytest.raises((ValueError, OSError)): eval_poly_compare.quality(spec, result, folder, [])


@pytest.mark.parametrize('fault', ['model', 'method', 'task', 'run', 'label', 'runtime', 'package', 'official', 'target',
    'caps', 'resources', 'companion', 'source', 'metadata', 'job', 'repeat_path'])
def test_binding_rejects_changed_frozen_dispatch_and_resources(tmp_path, fault):
    data = fixture(tmp_path); plan, spec, result, folder, directory, paths = data
    request = folder / 'swe-request.json'
    fields = {'model': 'model', 'method': 'method', 'task': 'task', 'run': 'run', 'label': 'label', 'runtime': 'runtime',
              'package': 'package', 'official': 'official', 'target': 'target', 'caps': 'verifier_cap_add'}
    if fault in fields:
        key = fields[fault]
        value = {'model': 'other', 'method': {'class': 'Other'}, 'task': copy.deepcopy(result['task']), 'run': {'grade': False},
            'label': 'other', 'runtime': {}, 'package': '/live', 'official': '/live', 'target': 'other:443', 'caps': ['SYS_ADMIN']}[fault]
        if fault == 'task': value['initial_state']['base_commit'] = '9' * 40
        change(request, lambda d: d.update({key: value}))
    if fault == 'resources': change(Path(paths[plan['config']['environment']['resources']]), lambda d: d.update(release='other'))
    if fault == 'companion': Path(paths[str(Path(plan['config']['environment']['bindir']) / 'codex-code-mode-host')]).unlink()
    if fault == 'source': (directory / 'inputs/official-inputs/polybench/pyproject.toml').write_text('[project]\nversion="other"\n', encoding='utf-8')
    if fault == 'metadata': (directory / 'inputs/official-inputs/dependencies/poly_bench_evaluation-0.1.0.dist-info/METADATA').write_text('Name: other\nVersion: 0.1.0\n', encoding='utf-8')
    if fault == 'job': plan['jobs'] = []
    if fault == 'repeat_path': folder = directory / 'jobs' / spec['id'] / 'attempt-2'
    with pytest.raises((ValueError, OSError)): eval_poly_compare.binding(plan, spec, result, folder, directory, paths, [])


@pytest.mark.linux_only
def test_reader_does_not_import_author_launch_commands_or_write_artifacts(tmp_path, monkeypatch):
    data = fixture(tmp_path)
    import builtins
    import subprocess
    original_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.startswith(('poly_bench_evaluation', 'docker')):
            raise AssertionError('reader imported an execution library')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    def forbidden(*args, **kwargs): raise AssertionError('reader launched a process')
    monkeypatch.setattr(subprocess, 'run', forbidden); monkeypatch.setattr(subprocess, 'Popen', forbidden)
    directory = data[4]
    before = {str(p): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    assert check(data)[0] == {'repository.resolved': True}
    assert before == {str(p): p.read_bytes() for p in directory.rglob('*') if p.is_file()}


@pytest.mark.linux_only
@pytest.mark.parametrize('fault', ['grade_error', 'grade_test_only', 'report_drift', 'patch_drift', 'owner_missing',
                                   'worker_missing', 'bad_raw_report', 'added_artifact', 'tool_failure'])
def test_official_quality_faults_do_not_destroy_model_binding_or_accounting(tmp_path, fault):
    data = fixture(tmp_path); plan, spec, result, folder, directory, paths = data
    if fault == 'grade_error': result['grade'] = dict(resolved=None, infra_invalid=True, error='official grader failed')
    if fault == 'grade_test_only': result['grade']['test_only'] = True
    if fault == 'report_drift': Path(result['grade']['report']).write_text('{}', encoding='utf-8')
    if fault == 'patch_drift': (folder / 'model.patch').write_text('altered', encoding='utf-8')
    if fault == 'owner_missing': (folder / 'resources-swe-verifier.json').unlink()
    if fault == 'worker_missing': (folder / 'swe-worker-result.json').unlink()
    if fault == 'bad_raw_report': change(Path(result['grade']['report']), lambda d: d.update(test_only=True)); refresh(data)
    if fault == 'added_artifact': write(folder / 'official-logs/extra.json', {})
    if fault == 'tool_failure':
        result['execution_health'] = dict(execution_invalid=True, tool_calls=1, tool_outputs=1,
            tool_runtime_failures=[dict(kind='code_mode_host_spawn_failure', call_id='x', error='missing helper')])
    evidence = []
    assert eval_poly_compare.binding(plan, spec, result, folder, directory, paths, evidence) is None
    assert not any('official-logs' in row['path'] or Path(row['path']).name in
                   ('model.patch', 'predictions.jsonl', 'swe-worker-result.json', 'resources-swe-verifier.json') for row in evidence)
    from ctxpress.live import usage as eval_usage
    usage = eval_usage.analyze(result, plan['config']['model'])
    bill = eval_usage.bill(usage, plan['config']['prices'])
    assert usage['complete'] and usage['model_usage_complete'] and not usage['missing_api_cache_write_usage']
    assert bill['api_cost_complete'] and bill['api_cost_at_declared_rates_usd'] > 0
    with pytest.raises((ValueError, OSError)): eval_poly_compare.quality(spec, result, folder, [])
