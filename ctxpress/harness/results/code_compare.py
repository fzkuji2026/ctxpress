"""Read-only BigCodeBench evidence; sample success is not issue resolution.

The frontend must first validate its frozen plan/inputs, common result/model/
proxy-log binding, and execution health. Its accounting gate remains independent
of quality. No author package, executable, grader, or provider is invoked here.
Import frontend helpers inside functions so the frontend can import this reader.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from urllib.parse import urlsplit
from email.parser import Parser
from pathlib import Path

from ctxpress.benchmarks.bigcode import protocol as code_protocol
from ctxpress.harness.jobs import inputs as eval_inputs, task as task_api, resources as task_resources


def _real(document):
    if not isinstance(document, dict) or document.get('test_only'):
        raise ValueError('test_only or invalid BigCodeBench evidence')
    return document


def _request(spec, folder, evidence):
    from ctxpress.harness.results import task_compare as common
    request = _real(common._read(folder / 'swe-request.json', folder, evidence))
    if (request.get('schema') != 'ctxpress.eval.swe_trial' or request.get('version') != 1 or
            request.get('api') != 'codebench' or
            not isinstance(request.get('project'), str) or
            not re.fullmatch(r'ctxp-sw-[0-9a-f]{24}', request['project']) or
            request.get('folder') != str(folder) or request.get('sample_index') != spec['repeat'] or
            type(request.get('sample_index')) is not int or
            type(request.get('n_samples')) is not int or request['n_samples'] <= spec['repeat']):
        raise ValueError('BigCodeBench worker request/sample identity differs')
    return request


def _artifacts(result, folder, evidence):
    from ctxpress.harness.results import task_compare as common
    records = common._object(result.get('official_artifacts'))
    required = {'samples.jsonl', 'official-logs/inputs/problem.json', 'official-logs/inputs/solution.py',
                'official-logs/inputs/request.json', 'official-logs/outputs/sample-result.json',
                'official-logs/outputs/code-resources.json', 'official-logs/preflight.log', 'official-logs/grading.log'}
    if not required <= set(records):
        raise ValueError('BigCodeBench grading artifacts are incomplete')
    for name, record in records.items():
        if not isinstance(record, dict) or not isinstance(record.get('path'), str):
            raise ValueError('BigCodeBench grading artifact record is invalid')
        if common._path(name, folder) != common._path(record['path'], folder):
            raise ValueError('BigCodeBench artifact name/path differs')
    actual = {'samples.jsonl'} | {'official-logs/' + p.relative_to(folder / 'official-logs').as_posix()
                                for p in (folder / 'official-logs').rglob('*') if p.is_file() or p.is_symlink()}
    if set(records) != actual:
        raise ValueError('BigCodeBench grading artifact coverage changed')
    return common._records(records, folder, evidence)


def _worker(request, result, folder, evidence):
    """Bind execution without turning grader failures into missing API costs."""
    from ctxpress.harness.results import task_compare as common
    _real(result)
    state = _real(common._read(folder / 'swe-worker-result.json', folder, evidence))
    expected = dict(swe_api='codebench', artifact_kind='code_samples', sample_id=request['sample_index'],
                    grading_run_id=request['project'], protocol='ctxpress_comparison',
                    network_policy='no_tool_network_model_socket_only')
    if (any(result.get(key) != value or state.get(key) != value for key, value in expected.items()) or
            type(result.get('sample_id')) is not int or type(state.get('sample_id')) is not int or
            not isinstance(result.get('stop'), str) or state.get('stop') != result['stop'] or
            type(result.get('calls')) is not int or result['calls'] < 0 or type(state.get('calls')) is not int or
            state['calls'] != result['calls'] or state.get('agent_exception') != result.get('agent_exception')):
        raise ValueError('BigCodeBench actual worker execution differs')
    return state


def _declared_version(source, metadata):
    declared = Parser().parsestr(metadata)
    versions = [node.value.value for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                and any(isinstance(target, ast.Name) and target.id == '__version__' for target in node.targets)]
    if (declared.get('Name', '').replace('-', '_') != 'bigcodebench' or versions != [declared.get('Version')] or
            not versions or versions[0] == 'local-dev'):
        raise ValueError('BigCodeBench frozen author source/metadata version differs')
    return versions[0]


def _sample(spec, result, folder, evidence):
    """Bind the original grader payload, official report and independent cleanup."""
    from ctxpress.harness.results import task_compare as common
    _real(result)
    grade = _real(common._object(result.get('grade')))
    if (grade.get('error') or grade.get('failure_kind') or grade.get('infra_invalid') is not False or
            grade.get('quality_kind') != 'code_sample' or grade.get('resolved') is not None or
            type(grade.get('sample_passed')) is not bool or type(grade.get('sample_id')) is not int or
            type(grade.get('n_samples')) is not int):
        raise ValueError('BigCodeBench recorded official sample grade is invalid')
    request = _request(spec, folder, evidence)
    documents = _artifacts(result, folder, evidence)
    state = _worker(request, result, folder, evidence)
    images = spec['resources']['images']
    expected_separation = dict(agent_image=images['agent']['id'], verifier_image=images['grading']['verifier']['id'],
                               checked_cleanup=True, author_grading=True)
    if (request.get('agent_image') != expected_separation['agent_image'] or
            request.get('grading_image') != expected_separation['verifier_image'] or
            result.get('separate_verifier') != expected_separation or state.get('separate_verifier') != expected_separation or
            state.get('official_artifacts') != result['official_artifacts']):
        raise ValueError('BigCodeBench independent worker execution differs')
    report = folder / 'official-logs/outputs/sample-result.json'
    samples = folder / 'samples.jsonl'
    if any(common._path(value, folder) != target for value, target in (
            (grade['report'], report), (result['official_report'], report), (state['official_report'], report),
            (result['code_samples'], samples), (state['code_samples'], samples))):
        raise ValueError('BigCodeBench report/sample is not from this attempt')
    payload = _real(common._object(json.loads(documents['official-logs/inputs/request.json'])))
    policy = code_protocol.options(request['run']['code'])
    expected = dict(schema='ctxpress.eval.bigcode_grade_request', version=1, official='/ctxpress-official',
                    package='/ctxpress-runtime', task_id=spec['task']['id'], sample_id=spec['repeat'],
                    n_samples=request['n_samples'], dataset=spec['task']['evaluation']['dataset'], options=policy,
                    host_python=request['runtime']['version'], problem='/ctxpress-grade-input/problem.json',
                    solution='/ctxpress-grade-input/solution.py', result='/ctxpress-grade-output/sample-result.json')
    if (any(payload.get(key) != value for key, value in expected.items()) or
            type(payload.get('sample_id')) is not int or type(payload.get('n_samples')) is not int):
        raise ValueError('BigCodeBench actual grading request differs')
    for name in ('problem', 'solution'):
        suffix = '.json' if name == 'problem' else '.py'
        raw = documents['official-logs/inputs/' + name + suffix]
        if payload.get(name + '_sha256') != hashlib.sha256(raw).hexdigest():
            raise ValueError('BigCodeBench bound grading input hash differs')
    problem = _real(common._object(json.loads(documents['official-logs/inputs/problem.json'])))
    # Validate paths before the existing dataset reader can open them.
    for item in request['task']['inputs']:
        common._path(item['path'], folder.parents[2])
    if problem != code_protocol.instance(request['task']):
        raise ValueError('BigCodeBench bound problem differs from frozen publisher row')
    exported = [json.loads(line) for line in documents['samples.jsonl'].decode().splitlines() if line.strip()]
    solution = documents['official-logs/inputs/solution.py'].decode('utf-8')
    if exported != [dict(task_id=spec['task']['id'], solution=solution)]:
        raise ValueError('BigCodeBench bound solution differs from exported sample')
    raw = _real(common._object(json.loads(documents['official-logs/outputs/sample-result.json'])))
    # binding() already checked the frozen hashes for these publisher files.
    # Read their version again here so actual grading remains a quality gate.
    root = folder.parents[2]
    official = common._path(Path(request['official']) / 'dependencies', root)
    metadata = list(official.glob('bigcodebench-*.dist-info/METADATA'))
    if len(metadata) != 1:
        raise ValueError('BigCodeBench real distribution metadata is ambiguous')
    source = common._read(Path(request['official']) / 'bigcodebench/bigcodebench/_version.py', root, evidence, document=False)
    distribution = common._read(metadata[0], root, evidence, document=False)
    if raw.get('author_version') != _declared_version(source.decode('utf-8'), distribution.decode('utf-8')):
        raise ValueError('BigCodeBench recorded author version differs from frozen source/metadata')
    proof = _real(common._object(json.loads(documents['official-logs/outputs/code-resources.json'])))
    if proof.get('project') != request['project']:
        raise ValueError('BigCodeBench grading project differs from worker request')
    owners = []
    for role in ('agent', 'verifier'):
        owner = _real(common._read(folder / ('resources-swe-' + role + '.json'), folder, evidence,
                                   proof[role + '_resources_sha256']))
        if (owner.get('schema') != 'ctxpress.eval.swe_resources' or owner.get('version') != 1 or
                owner.get('role') != role or owner.get('project') != request['project'] or
                owner.get('label') != request['label'] or owner.get('task_id') != spec['task']['id'] or
                owner.get('container') != request['project'] + ('-agent' if role == 'agent' else '-grade') or
                not isinstance(owner.get('container_id'), str) or not re.fullmatch(r'[0-9a-f]{64}', owner['container_id']) or
                owner.get('image') != expected_separation['agent_image' if role == 'agent' else 'verifier_image'] or
                owner.get('cleaned') is not True or owner.get('credentials_may_exist') is not False or
                owner.get('phase') not in ('stopped', 'recovered') or not owner.get('daemon_id')):
            raise ValueError('BigCodeBench independent cleanup ownership differs')
        owners.append(owner)
    if (owners[0]['daemon_id'] != owners[1]['daemon_id'] or owners[0]['container_id'] == owners[1]['container_id'] or
            owners[1].get('channel') is not None or not owners[0].get('channel')):
        raise ValueError('BigCodeBench verifier isolation differs')
    if (raw.get('author_interfaces') != ['trusted_check', 'untrusted_check', 'estimate_pass_at_k'] or
            raw.get('protocol') != 'ctxpress_comparison' or raw.get('options') != policy or
            raw.get('sample_id') != spec['repeat'] or type(raw.get('sample_id')) is not int or
            raw.get('n_samples') != request['n_samples'] or type(raw.get('n_samples')) is not int):
        raise ValueError('BigCodeBench original API/repeat/policy evidence differs')
    runtime = _real(raw.get('grading_runtime'))
    version = re.match(r'(\d+)\.(\d+)', runtime.get('version', ''))
    host = re.match(r'(\d+)\.(\d+)', request['runtime']['version'])
    if (runtime.get('platform') != 'linux' or not version or not host or version.groups() != host.groups() or
            not isinstance(runtime.get('python'), str) or not Path(runtime['python']).is_absolute()):
        raise ValueError('BigCodeBench actual grading Python ABI differs')
    reference = _real(raw.get('groundtruth'))
    timing = reference.get('time')
    if (raw.get('groundtruth_valid') is not True or reference.get('task_id') != spec['task']['id'] or
            type(timing) not in (int, float) or not math.isfinite(timing) or timing < 0):
        raise ValueError('BigCodeBench official reference did not pass')
    # This published reader checks report/proof hashes, denominator, verdict and
    # author estimator values. It imports no author library and launches nothing.
    observed = code_protocol.read_grade(request['task'], report)
    if (observed.get('error') or observed.get('quality_kind') != 'code_sample' or
            grade.get('resolved') is not None or grade.get('sample_id') != spec['repeat'] or
            type(grade.get('sample_id')) is not int or type(grade.get('n_samples')) is not int or
            grade.get('n_samples') != request['n_samples'] or type(grade.get('sample_passed')) is not bool or
            grade.get('infra_invalid') is not False or grade.get('error') or grade.get('failure_kind') or
            raw.get('infra_invalid') is not False or
            any(grade.get(key) != value for key, value in observed.items())):
        raise ValueError('BigCodeBench recorded grade differs from original independent report')
    return request, raw, observed


def binding(plan, spec, result, folder, directory, paths, evidence):
    """BigCode-only driver/resource/actual execution gate after common binding.

    Official report, submission hashes and cleanup are quality gates below.
    Their absence/failure must not discard independently observed API usage.
    """
    from ctxpress.harness.results import task_compare as common
    folder, directory = Path(folder).resolve(), Path(directory).resolve()
    config = plan['config']
    if (config.get('benchmark') != 'bigcodebench' or config.get('start_mode') != 'task_start' or
            spec not in plan['jobs'] or folder != directory / 'jobs' / spec['id'] / 'attempt-1'):
        raise ValueError('BigCodeBench attempt is not bound to its frozen job')
    request = _request(spec, folder, evidence)
    expected_task = task_api.remap(spec['task'], paths)
    upstream = config['environment'].get('upstream') or 'https://chatgpt.com/backend-api/codex'
    url = urlsplit(upstream)
    if url.scheme != 'https' or not url.hostname or url.username is not None or url.password is not None or url.fragment:
        raise ValueError('BigCodeBench frozen provider address is invalid')
    target = ('[' + url.hostname + ']' if ':' in url.hostname else url.hostname) + ':' + str(url.port or 443)
    expected = dict(task=expected_task, model=config['model'], reasoning=config['reasoning'],
                    method=common._expected_method(spec['method'], plan, folder, evidence), run=config['run'],
                    compact_limit=spec['compact_limit'], label=plan['sha256'][:12] + '-' + spec['id'],
                    package=str(directory / 'runtime'), official=str(directory / 'inputs/official-inputs'),
                    profiles=str(folder / 'method-inputs'), bindir=str(directory / 'inputs/bin'), n_samples=config['repeats'],
                    upstream=upstream, target=target, via=config['environment'].get('via'), verifier_cap_add=[])
    if any(request.get(key) != value for key, value in expected.items()):
        raise ValueError('BigCodeBench actual driver request differs from frozen plan')
    if (result.get('method') != eval_inputs.method(spec['method'], paths) or
            result.get('binary_version') != request.get('binary_version') or not request.get('binary_version')):
        raise ValueError('BigCodeBench method or actual CLI version differs')
    if any(row.get('test_only') for row in result.get('rewrites', []) if isinstance(row, dict)):
        raise ValueError('test_only provider/model log')
    lock = plan['task_resources']
    resource_source = config['environment']['resources']
    copied = common._read(paths[resource_source], directory, evidence, plan['artifacts'][resource_source])
    if (copied != lock or spec['resources'] != lock['tasks'][spec['task']['id']] or
            spec['resources'].get('task_sha256') != task_resources.task_digest(spec['task']) or
            request.get('runtime') != lock.get('runtime')):
        raise ValueError('BigCodeBench actual resources/runtime differ from frozen plan')
    version = re.match(r'(\d+)\.(\d+)\.', lock['runtime'].get('version', ''))
    if (lock['runtime'].get('platform') != 'linux' or not version or tuple(map(int, version.groups())) < (3, 10) or
            spec['task']['evaluation']['dataset']['revision'] != lock['release']):
        raise ValueError('BigCodeBench prepared Python or dataset release differs')
    for item in expected_task['inputs']:
        common._read(item['path'], directory, evidence, item['sha256'], document=False)
    # Never run the CLI while analyzing. Match recorded driver version against
    # hashed CLI/helper copies; common input verification also preserves modes.
    from ctxpress.harness.runtime.codex_binary import requires_companion
    binary_names = ['codex'] + (['codex-code-mode-host'] if requires_companion(request['binary_version']) else [])
    for name in binary_names:
        source = str(Path(config['environment']['bindir']) / name)
        if source not in plan['artifacts'] or common._path(paths[source], directory) != directory / 'inputs/bin' / name:
            raise ValueError('BigCodeBench frozen binary bundle missing or unbound')
        common._read(paths[source], directory, evidence, plan['artifacts'][source], document=False)
    _worker(request, result, folder, evidence)
    tree = lock['trees']['dependencies']
    metadata = [name for name in tree['files'] if re.fullmatch(r'bigcodebench-[^/]+\.dist-info/METADATA', name)]
    if len(metadata) != 1:
        raise ValueError('BigCodeBench real distribution metadata is ambiguous')
    def frozen(tree, name):
        source = str(Path(tree['root']) / name)
        if plan['artifacts'].get(source) != tree['files'][name]:
            raise ValueError('BigCodeBench author source is not frozen')
        return common._read(paths[source], directory, evidence, tree['files'][name], document=False).decode('utf-8')
    _declared_version(frozen(lock['trees']['bigcodebench'], 'bigcodebench/_version.py'), frozen(tree, metadata[0]))


def quality(spec, result, folder, evidence):
    """Return code.sample_passed only; common health/cost gates stay separate.

    A frozen author estimator lookup is evidence, not an aggregate pass@k score.
    The optional author_cohort reader consumes a stored, complete cohort report.
    Call this only after common binding and binding above have succeeded.
    """
    _, raw, observed = _sample(spec, result, Path(folder).resolve(), evidence)
    return {'code.sample_passed': observed['sample_passed']}, dict(
        kind='code_sample', sample_id=observed['sample_id'], n_samples=observed['n_samples'],
        resolved=None, published_protocol_reproduced=False, raw_instance_report=raw,
        independent_grading=observed['independent_grading'],
        author_estimator_evidence=dict(interface='estimate_pass_at_k', table=observed['estimator_table']),
        author_pass_at_k=None, author_cohort_required=True)


def author_cohort(plan, jobs, directory, paths, evidence, *, label):
    """Preserve an existing code_report cohort; never manufacture one from booleans.

    Missing stored report returns None. Changed/incomplete evidence raises. All
    planned task/repeat members of this method must pass the common binding and
    health gates and both readers above. No code_report/author scorer is called.
    """
    from ctxpress.harness.results import task_compare as common
    from ctxpress.harness.runtime import execution_health
    directory = Path(directory).resolve()
    report_path = directory / 'report.json'
    if not report_path.exists():
        return None
    report = _real(common._read(report_path, directory, evidence))
    if (report.get('schema') != 'ctxpress.eval.report' or report.get('version') != 1 or
            report.get('plan_sha256') != plan['sha256'] or report.get('code_sha256') != plan['code_sha256'] or
            report.get('model') != plan['config']['model'] or report.get('backend') != plan['config']['backend'] or
            report.get('task_resource_manifest_sha256') != plan['task_resources']['sha256'] or
            report.get('benchmark_release') != plan['task_resources']['release'] or
            report.get('evidence') != 'real execution only; mechanism checks do not establish task quality'):
        raise ValueError('BigCodeBench stored author cohort is not bound to frozen execution')
    groups = [group for group in report['methods'] if group.get('method') == label]
    if len(groups) != 1:
        raise ValueError('BigCodeBench stored method cohort missing or duplicated')
    group = _real(groups[0])
    metrics = _real(group['code_metrics'])
    expected = {spec['id']: spec for spec in plan['jobs'] if spec['label'] == label}
    members = [job for job in jobs if common._object(job['spec']).get('label') == label]
    if not expected or len(members) != len(expected) or {job['id'] for job in members} != set(expected):
        raise ValueError('BigCodeBench author cohort is incomplete or duplicated')
    reported = group.get('jobs')
    if (not isinstance(reported, list) or len(reported) != len(expected) or
            {row.get('id') for row in reported} != set(expected)):
        raise ValueError('BigCodeBench stored author member coverage differs')
    reported = {row['id']: _real(row) for row in reported}
    n, policy = plan['config']['repeats'], plan['config']['run']['code']
    tasks = {}
    for job in members:
        spec = common._object(job['spec'])
        if spec != expected[job['id']] or job.get('status') != 'completed' or type(job.get('attempt')) is not int or job['attempt'] != 1:
            raise ValueError('BigCodeBench cohort member changed, incomplete or retried')
        folder = directory / 'jobs' / spec['id'] / 'attempt-1'
        if {p.name for p in folder.parent.glob('attempt-*')} != {'attempt-1'}:
            raise ValueError('BigCodeBench cohort has retry or missing attempt evidence')
        result = _real(common._object(job['result']))
        recorded = reported[job['id']]
        if (recorded.get('task_id') != spec['task']['id'] or recorded.get('repeat') != spec['repeat'] or
                type(recorded.get('repeat')) is not int or recorded.get('status') != 'completed' or
                type(recorded.get('attempt')) is not int or recorded['attempt'] != 1 or
                recorded.get('grade') != result.get('grade')):
            raise ValueError('BigCodeBench stored author member binding differs')
        if common._read(folder / 'result.json', folder, evidence) != result:
            raise ValueError('BigCodeBench cohort result artifact differs')
        common._binding(plan, spec, result, folder, directory, paths, evidence)
        binding(plan, spec, result, folder, directory, paths, evidence)
        if execution_health.observe(result)['execution_invalid']:
            raise ValueError('BigCodeBench cohort has invalid execution health')
        values, details = quality(spec, result, folder, evidence)
        row = tasks.setdefault(spec['task']['id'], dict(indices=set(), passed=0, table=details['author_estimator_evidence']['table']))
        if spec['repeat'] in row['indices'] or row['table'] != details['author_estimator_evidence']['table']:
            raise ValueError('BigCodeBench cohort repeat or author estimator evidence differs')
        row['indices'].add(spec['repeat']); row['passed'] += values['code.sample_passed']
    if (metrics.get('kind') != 'code_samples' or metrics.get('method', label) != label or
            metrics.get('complete') is not True or metrics.get('options') != policy or
            metrics.get('samples_per_task') != n or metrics.get('task_count') != len(tasks) or
            metrics.get('planned_samples') != n * len(tasks) or metrics.get('valid_samples') != n * len(tasks) or
            metrics.get('estimator') != 'frozen author bigcodebench.eval.estimate_pass_at_k' or
            metrics.get('protocol') != 'ctxpress_comparison'):
        raise ValueError('BigCodeBench stored author cohort denominators/protocol differ')
    if (any(type(metrics.get(key)) is not int for key in
            ('samples_per_task', 'task_count', 'planned_samples', 'valid_samples')) or
            metrics.get('unavailable') != {str(k): 'insufficient samples' for k in policy['pass_k'] if k > n} or
            metrics.get('generation') != dict(host='codex', independent_sessions=True, samples_per_task=n,
                decoding='provider-controlled; temperature/top-p are not configured by this CLI')):
        raise ValueError('BigCodeBench stored author cohort availability/generation differs')
    stored_tasks = {row['task_id']: row for row in metrics['tasks']}
    if len(stored_tasks) != len(metrics['tasks']) or set(stored_tasks) != set(tasks):
        raise ValueError('BigCodeBench stored author task cohort differs')
    for identity, row in tasks.items():
        actual = _real(stored_tasks[identity])
        scores = {str(k): row['table'][str(k)][row['passed']] if k <= n else None for k in policy['pass_k']}
        if (row['indices'] != set(range(n)) or actual.get('complete') is not True or actual.get('errors') != [] or
                actual.get('planned_samples') != n or actual.get('valid_samples') != n or
                actual.get('passed_samples') != row['passed'] or actual.get('pass_at_k') != scores or
                any(type(actual.get(key)) is not int for key in ('planned_samples', 'valid_samples', 'passed_samples'))):
            raise ValueError('BigCodeBench stored scores differ from complete author estimator evidence')
        if any(type(value) not in (int, float) for key, value in actual['pass_at_k'].items() if int(key) <= n):
            raise ValueError('BigCodeBench stored author task score is not numeric')
    means = {str(k): sum(stored_tasks[t]['pass_at_k'][str(k)] for t in tasks) / len(tasks) if k <= n else None
             for k in policy['pass_k']}
    if (metrics.get('pass_at_k') != means or
            any(type(value) not in (int, float) for key, value in metrics['pass_at_k'].items() if int(key) <= n)):
        raise ValueError('BigCodeBench stored author cohort aggregate differs')
    return dict(kind='frozen_author_code_cohort', metrics=metrics, source=str(report_path),
                published_protocol_reproduced=False, sample_metric='code.sample_passed')
