"""Offline artifact fixtures test binding, never claim real benchmark performance.

The optional environment-selected pilot test reads a completed real run only.
No fixture executes Codex, an author grader, Docker, or a provider.
"""
import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from ctxpress.benchmarks import code_protocol
from ctxpress.benchmarks.bigcode_bench import BigCodeBench
from ctxpress.harness import eval_code_compare as reader
from ctxpress.harness import eval_plan, eval_task_compare as common, execution_health, task as task_api, task_resources
from ctxpress.live import usage as eval_usage


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path, statuses=('pass',), pass_k=(1, 5, 10)):
    """Small explicit schema fixture; payload code/publisher packages never run."""
    data = tmp_path / 'source-data'
    row = dict(task_id='BigCodeBench/294', complete_prompt='def task_func(x):\n    """Return x."""\n',
               instruct_prompt='Return x.', code_prompt='def task_func(x):\n', canonical_solution='    return x\n',
               test='assert task_func(1) == 1\n', entry_point='task_func')
    write(data / 'instances.jsonl', row)
    write(data / 'dataset_manifest.json', dict(dataset='bigcode/bigcodebench',
        revision='b74c0d0bf70d2c0bc459be537895cca163007f1a', split='complete', subset='full'))
    task = BigCodeBench().task_instances(data)[0]
    directory = tmp_path / 'run'
    paths, artifacts = {}, {}

    def freeze(source, target, raw):
        target = directory / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        paths[str(source)] = str(target)
        artifacts[str(source)] = digest(target)
        return artifacts[str(source)]

    for item in task['inputs']:
        freeze(item['path'], 'inputs/task-data/' + Path(item['path']).name, Path(item['path']).read_bytes())
    binary = tmp_path / 'source-bin'
    for name in ('codex', 'codex-code-mode-host'):
        freeze(binary / name, 'inputs/bin/' + name, b'opaque offline binary fixture: ' + name.encode())
        Path(paths[str(binary / name)]).chmod(0o755)
    trees = {}
    for key, name, raw in (
        ('bigcodebench', 'bigcodebench/_version.py', b"__version__ = version = '0.2.5'\n"),
        ('dependencies', 'bigcodebench-0.2.5.dist-info/METADATA', b'Name: bigcodebench\nVersion: 0.2.5\n'),
    ):
        root = tmp_path / 'source-official' / key
        sha = freeze(root / name, 'inputs/official-inputs/' + key + '/' + name, raw)
        trees[key] = dict(root=str(root), files={name: sha})
    images = dict(agent={'id': 'sha256:' + 'a' * 64}, grading={'verifier': {'id': 'sha256:' + 'b' * 64}})
    resource = dict(task_sha256=task_resources.task_digest(task), images=images)
    runtime = dict(python='/offline/python3.10', sha256='c' * 64, version='3.10.21 fixture', platform='linux')
    lock = task_resources.seal(dict(schema=task_resources.SCHEMA, version=1, benchmark='bigcodebench',
        release=task['evaluation']['dataset']['revision'], trees=trees, tasks={task['id']: resource}, runtime=runtime))
    source_resource = tmp_path / 'source-resources.json'
    freeze(source_resource, 'inputs/artifacts/resources.json', json.dumps(lock).encode())
    n = len(statuses)
    method = {'class': 'NoCompaction', 'args': {}}
    config = dict(benchmark='bigcodebench', start_mode='task_start', backend='codex_docker', model='main',
        reasoning='medium', repeats=n, environment=dict(bindir=str(binary), resources=str(source_resource)),
        run=dict(max_calls=100, timeout=1800, grading_timeout=1800, grade=True, code=code_protocol.options({'pass_k': list(pass_k)})))
    specs = [dict(id=f'm000-t000-r{i:03}', task=copy.deepcopy(task), method=copy.deepcopy(method), label='reference',
                  repeat=i, compact_limit=230000, resources=copy.deepcopy(resource)) for i in range(n)]
    plan = dict(config=config, jobs=specs, artifacts=artifacts, task_resources=lock, code_sha256='d' * 64)
    plan['sha256'] = common._digest(plan)
    jobs = []
    for spec, status in zip(specs, statuses):
        folder = directory / 'jobs' / spec['id'] / 'attempt-1'
        folder.mkdir(parents=True)
        project = 'ctxp-sw-' + f"{spec['repeat'] + 1:024x}"
        label = plan['sha256'][:12] + '-' + spec['id']
        moved = task_api.remap(task, paths)
        request = dict(schema='ctxpress.eval.swe_trial', version=1, project=project, label=label, task=moved,
            method=method, model=config['model'], reasoning=config['reasoning'], run=config['run'],
            compact_limit=spec['compact_limit'], binary_version='0.159.0-alpha.12.1',
            bindir=str(directory / 'inputs/bin'), package=str(directory / 'runtime'),
            official=str(directory / 'inputs/official-inputs'), profiles=str(folder / 'method-inputs'), folder=str(folder),
            runtime=runtime, api='codebench', agent_image=images['agent']['id'], grading_image=images['grading']['verifier']['id'],
            verifier_cap_add=[], upstream='https://chatgpt.com/backend-api/codex', target='chatgpt.com:443', via=None,
            sample_index=spec['repeat'], n_samples=n)
        write(folder / 'swe-request.json', request)
        for role in ('agent', 'verifier'):
            write(folder / ('resources-swe-' + role + '.json'), dict(schema='ctxpress.eval.swe_resources', version=1,
                project=project, container=project + ('-agent' if role == 'agent' else '-grade'), label=label,
                role=role, image=images['agent']['id'] if role == 'agent' else images['grading']['verifier']['id'],
                daemon_id='offline-daemon', channel='/tmp/' + project + '-channel-fixture' if role == 'agent' else None,
                credentials_may_exist=False, cleaned=True, phase='stopped', container_id=('1' if role == 'agent' else '2') * 64,
                task_id=task['id']))
        solution = 'def task_func(x):\n    return x\n'
        write(folder / 'samples.jsonl', dict(task_id=task['id'], solution=solution))
        write(folder / 'official-logs/inputs/problem.json', code_protocol.instance(moved))
        (folder / 'official-logs/inputs/solution.py').write_text(solution, encoding='utf-8')
        payload = dict(schema='ctxpress.eval.bigcode_grade_request', version=1, official='/ctxpress-official',
            package='/ctxpress-runtime', task_id=task['id'], sample_id=spec['repeat'], n_samples=n,
            dataset=task['evaluation']['dataset'], options=config['run']['code'], host_python=runtime['version'],
            problem='/ctxpress-grade-input/problem.json', solution='/ctxpress-grade-input/solution.py',
            problem_sha256=digest(folder / 'official-logs/inputs/problem.json'),
            solution_sha256=digest(folder / 'official-logs/inputs/solution.py'), result='/ctxpress-grade-output/sample-result.json')
        write(folder / 'official-logs/inputs/request.json', payload)
        for name in ('preflight.log', 'grading.log'):
            (folder / 'official-logs' / name).write_text('offline schema fixture\n', encoding='utf-8')
        raw = dict(schema=code_protocol.SCHEMA, version=1, task_id=task['id'], sample_id=spec['repeat'], n_samples=n,
            dataset=task['evaluation']['dataset'], options=config['run']['code'], solution_sha256=payload['solution_sha256'],
            groundtruth_valid=True, groundtruth=dict(task_id=task['id'], time=.25), author_version='0.2.5',
            grading_runtime=dict(python='/usr/local/bin/python3', version='3.10.22 fixture', platform='linux'),
            author_interfaces=['trusted_check', 'untrusted_check', 'estimate_pass_at_k'], protocol='ctxpress_comparison',
            status=status, details={}, estimator_table={str(k): [c / n for c in range(n + 1)] for k in pass_k if k <= n},
            infra_invalid=False)
        write(folder / 'official-logs/outputs/sample-result.json', raw)
        proof = dict(schema=code_protocol.PROOF, version=1, task_id=task['id'], project=project,
            agent_image=request['agent_image'], verifier_image=request['grading_image'])
        write(folder / 'official-logs/outputs/code-resources.json', proof)
        rows = [dict(request=1, status=200, model='main', usage=dict(input_tokens=100, output_tokens=10,
                                                                   cached_tokens=0, cache_write_tokens=0))]
        logs = folder / 'agent/requests.jsonl'
        logs.parent.mkdir()
        logs.write_text(json.dumps(rows[0]) + '\n', encoding='utf-8')
        separation = dict(agent_image=request['agent_image'], verifier_image=request['grading_image'],
                          checked_cleanup=True, author_grading=True)
        state = dict(stop='completed', calls=1, agent_exception=None, official_report=str(folder / 'official-logs/outputs/sample-result.json'),
            code_samples=str(folder / 'samples.jsonl'), sample_id=spec['repeat'], artifact_kind='code_samples',
            separate_verifier=separation, protocol='ctxpress_comparison', network_policy='no_tool_network_model_socket_only',
            swe_api='codebench', grading_run_id=project)
        write(folder / 'swe-worker-result.json', state)
        result = dict(state, task=moved, method=method, model='main', reasoning='medium', benchmark={'name': 'bigcodebench'},
            requests=1, rewrites=rows, proxy_log=str(logs), binary_version=request['binary_version'],
            usage=dict(summary_calls=0, native_compaction_calls=0),
            execution_health=dict(execution_invalid=False, tool_calls=1, tool_outputs=1, tool_runtime_failures=[]))
        jobs.append(dict(id=spec['id'], spec=spec, result=result, status='completed', attempt=1))
        refresh(jobs[-1], directory)
    return plan, jobs, directory, paths


def folder_for(job, directory):
    return directory / 'jobs' / job['id'] / 'attempt-1'


def refresh(job, directory):
    """Refresh hashes after a semantic mutation so hash failure cannot hide it."""
    folder = folder_for(job, directory)
    proof_path = folder / 'official-logs/outputs/code-resources.json'
    proof = load(proof_path)
    for role in ('agent', 'verifier'):
        proof[role + '_resources_sha256'] = digest(folder / ('resources-swe-' + role + '.json'))
    for key, name in (('report', 'official-logs/outputs/sample-result.json'), ('samples', 'samples.jsonl')):
        proof[key] = dict(path=str(folder / name), sha256=digest(folder / name))
    write(proof_path, proof)
    records = {str(p.relative_to(folder)): dict(path=str(p), sha256=digest(p))
               for p in [folder / 'samples.jsonl', *(folder / 'official-logs').rglob('*')] if p.is_file()}
    state_path = folder / 'swe-worker-result.json'
    state = load(state_path)
    state['official_artifacts'] = records
    write(state_path, state)
    result = job['result']
    result['official_artifacts'] = records
    result['grade'] = code_protocol.read_grade(result['task'], folder / 'official-logs/outputs/sample-result.json')
    write(folder / 'result.json', result)


def sample(case, index=0):
    plan, jobs, directory, paths = case
    job = jobs[index]; spec = job['spec']; result = job['result']; folder = folder_for(job, directory); evidence = []
    common._binding(plan, spec, result, folder, directory, paths, evidence)
    reader.binding(plan, spec, result, folder, directory, paths, evidence)
    assert execution_health.observe(result)['execution_invalid'] is False
    metrics, details = reader.quality(spec, result, folder, evidence)
    return metrics, details, evidence


def store_cohort(case):
    """Write an explicit offline report fixture, never invoke a score writer."""
    plan, jobs, directory, paths = case
    n = len(jobs); policy = plan['config']['run']['code']; passed = sum(j['result']['grade']['sample_passed'] for j in jobs)
    table = jobs[0]['result']['grade']['estimator_table']
    scores = {str(k): table[str(k)][passed] if k <= n else None for k in policy['pass_k']}
    metrics = dict(kind='code_samples', samples_per_task=n, task_count=1, complete=True, options=policy,
        tasks=[dict(task_id=jobs[0]['spec']['task']['id'], planned_samples=n, valid_samples=n, passed_samples=passed,
                    complete=True, pass_at_k=scores, errors=[])], planned_samples=n, valid_samples=n, pass_at_k=scores,
        unavailable={str(k): 'insufficient samples' for k in policy['pass_k'] if k > n},
        estimator='frozen author bigcodebench.eval.estimate_pass_at_k',
        generation=dict(host='codex', independent_sessions=True, samples_per_task=n,
                        decoding='provider-controlled; temperature/top-p are not configured by this CLI'), protocol='ctxpress_comparison')
    report = dict(schema='ctxpress.eval.report', version=1, plan_sha256=plan['sha256'], code_sha256=plan['code_sha256'],
        model=plan['config']['model'], backend=plan['config']['backend'], task_resource_manifest_sha256=plan['task_resources']['sha256'],
        benchmark_release=plan['task_resources']['release'], evidence='real execution only; mechanism checks do not establish task quality',
        methods=[dict(method='reference', code_metrics=metrics, jobs=[dict(id=j['id'], task_id=j['spec']['task']['id'],
            repeat=j['spec']['repeat'], status='completed', attempt=1, grade=copy.deepcopy(j['result']['grade'])) for j in jobs])])
    write(directory / 'report.json', report)
    return report


def cohort(case):
    plan, jobs, directory, paths = case
    return reader.author_cohort(plan, jobs, directory, paths, [], label='reference')


@pytest.mark.linux_only
@pytest.mark.parametrize('status,passed', [('pass', True), ('fail', False), ('timeout', False)])
def test_sample_retains_boolean_and_separate_denominator(tmp_path, status, passed):
    case = fixture(tmp_path, (status,))
    metrics, details, evidence = sample(case)
    assert metrics == {'code.sample_passed': passed}
    assert type(metrics['code.sample_passed']) is bool
    assert details['resolved'] is None and details['author_pass_at_k'] is None
    assert details['published_protocol_reproduced'] is False
    assert details['raw_instance_report'] == case[1][0]['result']['grade']['raw_instance_report']
    assert details['author_estimator_evidence']['table'] == {'1': [0.0, 1.0]}
    names = {Path(item['path']).name for item in evidence}
    assert {'codex', 'codex-code-mode-host', 'sample-result.json', 'code-resources.json',
            'resources-swe-agent.json', 'resources-swe-verifier.json', 'problem.json', 'solution.py'} <= names
    assert cohort(case) is None  # No aggregate is manufactured from the sample.


@pytest.mark.parametrize('key,value', [
    ('model', 'other'), ('reasoning', 'high'), ('api', 'swebench'), ('task', {}), ('method', {'class': 'Other'}),
    ('run', {}), ('compact_limit', 1), ('sample_index', True), ('sample_index', 1), ('n_samples', 2),
    ('label', 'wrong'), ('project', 'wrong'), ('package', '/other'), ('official', '/other'), ('profiles', '/other'),
    ('bindir', '/other'), ('runtime', {}), ('agent_image', 'sha256:' + 'c' * 64),
    ('grading_image', 'sha256:' + 'c' * 64), ('binary_version', '1.0'), ('upstream', 'https://other.invalid'),
    ('target', 'other.invalid:443'), ('via', 'http://other.invalid'), ('verifier_cap_add', ['SYS_ADMIN']), ('test_only', True),
])
def test_changed_worker_request_rejected(tmp_path, key, value):
    case = fixture(tmp_path); job = case[1][0]; path = folder_for(job, case[2]) / 'swe-request.json'
    request = load(path); request[key] = value; write(path, request)
    with pytest.raises(ValueError):
        sample(case)


@pytest.mark.parametrize('name', ['codex', 'codex-code-mode-host'])
def test_binary_bundle_hash_required_without_execution(tmp_path, name):
    case = fixture(tmp_path); path = case[2] / 'inputs/bin' / name
    path.write_bytes(b'changed binary')
    with pytest.raises(ValueError, match='hash'):
        sample(case)


def test_missing_required_companion_rejected(tmp_path):
    case = fixture(tmp_path)
    source = str(Path(case[0]['config']['environment']['bindir']) / 'codex-code-mode-host')
    del case[0]['artifacts'][source]
    with pytest.raises(ValueError, match='bundle'):
        sample(case)


@pytest.mark.parametrize('role,key,value', [
    ('agent', 'cleaned', False), ('verifier', 'credentials_may_exist', True), ('verifier', 'channel', '/tmp/credentials'),
    ('agent', 'channel', None), ('verifier', 'daemon_id', 'other'), ('verifier', 'container_id', '1' * 64),
    ('verifier', 'container_id', 'short'), ('agent', 'label', 'other'), ('verifier', 'project', 'other'),
    ('agent', 'container', 'other'), ('verifier', 'task_id', 'BigCodeBench/295'), ('agent', 'phase', 'running'),
    ('agent', 'image', 'sha256:' + 'c' * 64), ('verifier', 'test_only', True),
])
def test_cleanup_semantics_rejected_even_with_updated_hashes(tmp_path, role, key, value):
    case = fixture(tmp_path); job = case[1][0]; path = folder_for(job, case[2]) / ('resources-swe-' + role + '.json')
    owner = load(path); owner[key] = value; write(path, owner); refresh(job, case[2])
    with pytest.raises(ValueError):
        sample(case)


@pytest.mark.parametrize('key,value', [
    ('author_interfaces', ['estimate_pass_at_k']), ('author_version', 'local-dev'), ('author_version', '0.2.6'),
    ('sample_id', True), ('n_samples', True), ('dataset', {}), ('options', {}), ('protocol', 'official'),
    ('groundtruth_valid', False), ('groundtruth', {'task_id': 'BigCodeBench/294', 'time': -1}),
    ('groundtruth', {'task_id': 'BigCodeBench/294', 'time': True}), ('groundtruth', {'task_id': 'other', 'time': 1}),
    ('grading_runtime', {'python': '/usr/bin/python3', 'version': '3.12.1', 'platform': 'linux'}),
    ('grading_runtime', {'python': 'python3', 'version': '3.10.22', 'platform': 'linux'}),
    ('status', 'unknown'), ('estimator_table', {'1': [0, True]}), ('infra_invalid', True), ('test_only', True),
])
def test_original_api_semantics_rejected_even_with_updated_hashes(tmp_path, key, value):
    case = fixture(tmp_path); job = case[1][0]; path = folder_for(job, case[2]) / 'official-logs/outputs/sample-result.json'
    raw = load(path); raw[key] = value; write(path, raw); refresh(job, case[2])
    with pytest.raises(ValueError):
        sample(case)


@pytest.mark.parametrize('key,value', [('resolved', True), ('sample_passed', 1), ('n_samples', True), ('sample_id', False),
                                    ('infra_invalid', 0), ('error', 'grader failed'), ('test_only', True)])
def test_recorded_grade_type_and_error_gates(tmp_path, key, value):
    case = fixture(tmp_path); case[1][0]['result']['grade'][key] = value
    with pytest.raises(ValueError):
        sample(case)


@pytest.mark.parametrize('filename,key,value', [
    ('swe-worker-result.json', 'separate_verifier', {}), ('swe-worker-result.json', 'sample_id', True),
    ('swe-worker-result.json', 'calls', True), ('swe-worker-result.json', 'stop', 'timeout'),
    ('swe-worker-result.json', 'agent_exception', 'crashed'), ('swe-worker-result.json', 'test_only', True),
    ('official-logs/inputs/request.json', 'n_samples', 3), ('official-logs/inputs/request.json', 'host_python', '3.12.1'),
    ('official-logs/inputs/request.json', 'solution_sha256', '0' * 64), ('official-logs/inputs/request.json', 'test_only', True),
    ('official-logs/outputs/code-resources.json', 'project', 'ctxp-sw-' + 'f' * 24),
])
def test_actual_execution_mismatch(tmp_path, filename, key, value):
    case = fixture(tmp_path); job = case[1][0]; path = folder_for(job, case[2]) / filename
    value_doc = load(path); value_doc[key] = value; write(path, value_doc); refresh(job, case[2])
    with pytest.raises(ValueError):
        sample(case)


@pytest.mark.linux_only
def test_problem_cannot_be_replaced_despite_rehashed_payload(tmp_path):
    case = fixture(tmp_path); job = case[1][0]; folder = folder_for(job, case[2])
    path = folder / 'official-logs/inputs/problem.json'; problem = load(path); problem['test'] = 'pass'
    write(path, problem); path_request = folder / 'official-logs/inputs/request.json'; payload = load(path_request)
    payload['problem_sha256'] = digest(path); write(path_request, payload); refresh(job, case[2])
    with pytest.raises(ValueError, match='publisher row'):
        sample(case)


@pytest.mark.linux_only
def test_solution_must_be_the_exported_sample(tmp_path):
    case = fixture(tmp_path); job = case[1][0]; folder = folder_for(job, case[2])
    solution = folder / 'official-logs/inputs/solution.py'; solution.write_text('def task_func(x): return 0\n', encoding='utf-8')
    request_path = folder / 'official-logs/inputs/request.json'; payload = load(request_path)
    payload['solution_sha256'] = digest(solution); write(request_path, payload); refresh(job, case[2])
    with pytest.raises(ValueError, match='exported sample'):
        sample(case)


@pytest.mark.linux_only
@pytest.mark.parametrize('change', ['hash', 'coverage', 'key', 'outside', 'auth', 'symlink'])
def test_artifact_hash_coverage_and_path_safety(tmp_path, change):
    case = fixture(tmp_path); job = case[1][0]; folder = folder_for(job, case[2]); name = 'official-logs/grading.log'
    records = job['result']['official_artifacts']; path = folder / name
    if change == 'hash':
        path.write_text('changed', encoding='utf-8')
    elif change == 'coverage':
        (folder / 'official-logs/new-file.txt').write_text('unexpected', encoding='utf-8')
    elif change == 'key':
        records['official-logs/other.log'] = records.pop(name)
    elif change == 'outside':
        records[name]['path'] = str(tmp_path / 'outside.log')
    elif change == 'auth':
        records[name]['path'] = str(folder / 'auth.json')  # Deliberately never create/read credentials.
    else:
        path.unlink(); path.symlink_to(folder / 'official-logs/preflight.log')
    with pytest.raises(ValueError):
        sample(case)


@pytest.mark.parametrize('gate', ['no_model', 'wrong_model', 'log', 'test_only'])
def test_prep_fixtures_and_unobserved_model_requests_are_not_real_runs(tmp_path, gate):
    case = fixture(tmp_path); result = case[1][0]['result']; path = Path(result['proxy_log'])
    if gate == 'no_model':
        result['requests'] = 0
    elif gate == 'wrong_model':
        result['rewrites'][0]['model'] = 'other'; path.write_text(json.dumps(result['rewrites'][0]) + '\n', encoding='utf-8')
    elif gate == 'log':
        path.write_text('', encoding='utf-8')
    else:
        result['rewrites'][0]['test_only'] = True; path.write_text(json.dumps(result['rewrites'][0]) + '\n', encoding='utf-8')
    with pytest.raises(ValueError):
        sample(case)


@pytest.mark.linux_only
def test_quality_does_not_depend_on_known_cost_or_invent_a_cost(tmp_path):
    case = fixture(tmp_path); result = case[1][0]['result']; result['rewrites'][0].pop('usage')
    Path(result['proxy_log']).write_text(json.dumps(result['rewrites'][0]) + '\n', encoding='utf-8')
    metrics, details, _ = sample(case)
    assert metrics == {'code.sample_passed': True}
    assert 'cost' not in details and 'resolved' not in metrics


@pytest.mark.linux_only
def test_complete_stored_author_cohort_preserved_separately(tmp_path):
    case = fixture(tmp_path, ('pass', 'fail'))
    report = store_cohort(case); actual = cohort(case)
    assert actual['metrics'] == report['methods'][0]['code_metrics']
    assert actual['metrics']['pass_at_k'] == {'1': .5, '5': None, '10': None}
    assert actual['published_protocol_reproduced'] is False
    for i in range(2):
        assert sample(case, i)[1]['author_pass_at_k'] is None


@pytest.mark.linux_only
@pytest.mark.parametrize('gate', ['missing_member', 'duplicate', 'retry', 'status', 'invalid_health', 'changed_table'])
def test_cohort_requires_all_real_bound_healthy_members(tmp_path, gate):
    case = fixture(tmp_path, ('pass', 'fail')); store_cohort(case); plan, jobs, directory, _ = case
    if gate == 'missing_member':
        jobs.pop()
    elif gate == 'duplicate':
        jobs[1] = copy.deepcopy(jobs[0])
    elif gate == 'retry':
        (folder_for(jobs[0], directory).parent / 'attempt-2').mkdir()
    elif gate == 'status':
        jobs[0]['status'] = 'failed'
    elif gate == 'invalid_health':
        jobs[0]['result']['execution_health'] = dict(execution_invalid=True, tool_calls=1, tool_outputs=1,
            tool_runtime_failures=[dict(kind='code_mode_host_spawn_failure', call_id='fixture', error='missing helper')])
        write(folder_for(jobs[0], directory) / 'result.json', jobs[0]['result'])
    else:
        path = folder_for(jobs[1], directory) / 'official-logs/outputs/sample-result.json'; raw = load(path)
        raw['estimator_table']['1'][1] = .4; write(path, raw); refresh(jobs[1], directory)
    with pytest.raises(ValueError):
        cohort(case)


@pytest.mark.linux_only
@pytest.mark.parametrize('gate', ['plan', 'model', 'synthetic', 'denominator', 'bool_denominator', 'aggregate',
                                  'task_score', 'duplicate_task', 'incomplete', 'unavailable', 'generation', 'test_only',
                                  'missing_member', 'member_grade', 'member_repeat'])
def test_changed_author_report_rejected(tmp_path, gate):
    case = fixture(tmp_path, ('pass', 'fail')); report = store_cohort(case); metrics = report['methods'][0]['code_metrics']
    if gate == 'plan': report['plan_sha256'] = '0' * 64
    elif gate == 'model': report['model'] = 'other'
    elif gate == 'synthetic': report['evidence'] = 'synthetic offline fixture'
    elif gate == 'denominator': metrics['valid_samples'] = 1
    elif gate == 'bool_denominator': metrics['task_count'] = True
    elif gate == 'aggregate': metrics['pass_at_k'] = {'1': 1., '5': None, '10': None}
    elif gate == 'task_score': metrics['tasks'][0]['pass_at_k'] = {'1': 1., '5': None, '10': None}
    elif gate == 'duplicate_task': metrics['tasks'].append(copy.deepcopy(metrics['tasks'][0]))
    elif gate == 'incomplete': metrics['complete'] = False
    elif gate == 'unavailable': metrics['unavailable'] = {}
    elif gate == 'generation': metrics['generation']['independent_sessions'] = False
    elif gate == 'missing_member': report['methods'][0]['jobs'].pop()
    elif gate == 'member_grade': report['methods'][0]['jobs'][0]['grade']['sample_passed'] = False
    elif gate == 'member_repeat': report['methods'][0]['jobs'][0]['repeat'] = True
    else: metrics['test_only'] = True
    write(case[2] / 'report.json', report)
    with pytest.raises(ValueError):
        cohort(case)


@pytest.mark.parametrize('invalid', ['reference', 'missing_report', 'grade_error', 'cleanup', 'health'])
def test_grading_invalidity_keeps_execution_binding_and_observed_cost(tmp_path, monkeypatch, invalid):
    case = fixture(tmp_path); plan, jobs, directory, paths = case; job = jobs[0]; folder = folder_for(job, directory)
    if invalid == 'reference':
        path = folder / 'official-logs/outputs/sample-result.json'; raw = load(path)
        raw['groundtruth_valid'] = False; write(path, raw); refresh(job, directory)
    elif invalid == 'cleanup':
        path = folder / 'resources-swe-verifier.json'; owner = load(path)
        owner['cleaned'] = False; write(path, owner); refresh(job, directory)
    elif invalid == 'missing_report':
        (folder / 'official-logs/outputs/sample-result.json').unlink()
    elif invalid == 'grade_error':
        job['result']['grade'] = dict(resolved=None, error='author grader failed', infra_invalid=None)
    else:
        job['result']['execution_health'] = dict(execution_invalid=True, tool_calls=1, tool_outputs=1,
            tool_runtime_failures=[dict(kind='code_mode_host_spawn_failure', call_id='fixture', error='missing helper')])
    result = job['result']; evidence = []
    common._binding(plan, job['spec'], result, folder, directory, paths, evidence)
    reader.binding(plan, job['spec'], result, folder, directory, paths, evidence)
    usage = eval_usage.analyze(result, plan['config']['model'])
    prices = {'models': {'main': dict(input=2, cached=.1, cache_write=2.5, output=10,
                                     unit='USD_per_million_tokens', source='offline fixture', as_of='2026-10-05')}}
    bill = eval_usage.bill(usage, prices)
    assert bill['api_cost_complete'] is True and bill['api_cost_at_declared_rates_usd'] > 0
    if invalid != 'health':
        with pytest.raises((ValueError, FileNotFoundError)):
            reader.quality(job['spec'], result, folder, evidence)
    write(folder / 'result.json', result)
    plan['config']['prices'] = prices
    # Exercise the existing frontend's independent accounting/quality gates
    # through the agreed callbacks without editing or enabling its dispatcher.
    original_binding = common._binding
    def with_code_binding(*args):
        original_binding(*args)
        reader.binding(*args)
    monkeypatch.setattr(common, '_binding', with_code_binding)
    monkeypatch.setattr(common, '_verified', reader.quality)
    assessed = common._assessment(plan, job['spec'], [job], directory, paths, [], True)
    assert assessed['quality_admissible'] is False
    assert assessed['api_usage_complete'] is True and assessed['api_cost_complete'] is True
    assert assessed['api_cost_usd'] == bill['api_cost_at_declared_rates_usd']


@pytest.mark.parametrize('part', ['resource', 'dataset', 'source', 'metadata'])
def test_frozen_resource_original_input_and_author_hashes(tmp_path, part):
    case = fixture(tmp_path); plan, _, directory, paths = case
    if part == 'resource':
        path = Path(paths[plan['config']['environment']['resources']])
    elif part == 'dataset':
        path = directory / 'inputs/task-data/instances.jsonl'
    elif part == 'source':
        path = directory / 'inputs/official-inputs/bigcodebench/bigcodebench/_version.py'
    else:
        path = directory / 'inputs/official-inputs/dependencies/bigcodebench-0.2.5.dist-info/METADATA'
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='hash'):
        sample(case)


@pytest.mark.linux_only
def test_readers_are_read_only_and_never_spawn(tmp_path, monkeypatch):
    case = fixture(tmp_path, ('pass', 'fail')); store_cohort(case)
    tracked = [p for p in case[2].rglob('*') if p.is_file()]
    before = {str(p): (digest(p), p.stat().st_mtime_ns, p.stat().st_mode) for p in tracked}
    def forbidden(*args, **kwargs):
        pytest.fail('evidence reader must not launch or write')
    monkeypatch.setattr(subprocess, 'run', forbidden); monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(os, 'system', forbidden); monkeypatch.setattr(eval_plan, 'atomic_json', forbidden)
    sample(case); cohort(case)
    assert before == {str(p): (digest(p), p.stat().st_mtime_ns, p.stat().st_mode) for p in tracked}


@pytest.mark.skipif(not os.environ.get('CTXPRESS_BIGCODE_READER_PILOT'), reason='optional completed real pilot')
def test_completed_real_pilot_readonly():
    directory = Path(os.environ['CTXPRESS_BIGCODE_READER_PILOT']).resolve()
    plan = load(directory / 'plan.json'); manifest = load(directory / 'inputs/manifest.json')
    paths = {name: str(directory / 'inputs' / record['path']) for name, record in manifest['files'].items()}
    jobs = []
    for spec in plan['jobs']:
        folder = directory / 'jobs' / spec['id'] / 'attempt-1'
        jobs.append(dict(id=spec['id'], spec=spec, result=load(folder / 'result.json'), status='completed', attempt=1))
    case = plan, jobs, directory, paths
    for i, job in enumerate(jobs):
        metrics, details, _ = sample(case, i)
        assert metrics['code.sample_passed'] == job['result']['grade']['sample_passed']
        assert details['raw_instance_report']['author_version'] == '0.2.5'
    stored = load(directory / 'report.json')['methods'][0]['code_metrics']
    actual = reader.author_cohort(plan, jobs, directory, paths, [], label=plan['jobs'][0]['label'])
    assert actual['metrics'] == stored
