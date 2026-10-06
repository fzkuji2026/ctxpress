"""Read-only PolyBench comparison evidence; never import or invoke its grader.

The frontend owns frozen-plan validation, routing, accounting and common health
checks. These gates also reject preparation diagnostics without model evidence.
Frontend helpers are imported inside functions to avoid circular imports.
"""
from __future__ import annotations

import json
import re
from email.parser import Parser
from pathlib import Path
from urllib.parse import urlsplit


def _real(document):
    if (not isinstance(document, dict) or document.get('test_only') or document.get('error') or
            document.get('failure_kind') or document.get('infra_failure') or
            document.get('infra_invalid') is True or document.get('model_calls') == 0):
        raise ValueError('test_only, no-model diagnostic or infrastructure-invalid PolyBench evidence')
    return document


def _request(spec, result, folder, evidence):
    from ctxpress.harness.results import task_compare as c
    _real(result)
    request = _real(c._read(folder / 'swe-request.json', folder, evidence))
    if (request.get('schema') != 'ctxpress.eval.swe_trial' or type(request.get('version')) is not int or
            request['version'] != 1 or request.get('api') != 'polybench' or
            not isinstance(request.get('project'), str) or not re.fullmatch(r'ctxp-sw-[0-9a-f]{24}', request['project']) or
            request.get('folder') != str(folder) or (request.get('run') or {}).get('grade') is not True):
        raise ValueError('PolyBench worker request identity/official grading differs')
    c.task_api.verify_remap(spec['task'], request['task'])
    if request['task']['benchmark'] != 'swe-polybench' or result.get('task') != request['task']:
        raise ValueError('PolyBench actual task differs')
    images = spec['resources']['images']
    if (set(images['grading']) != {'verifier'} or images.get('services') or
            request.get('agent_image') != images['agent']['id'] or
            request.get('grading_image') != images['grading']['verifier']['id']):
        raise ValueError('PolyBench actual Agent/verifier image differs')
    rows = result.get('rewrites')
    if not isinstance(rows, list) or any(not isinstance(row, dict) or row.get('test_only') for row in rows):
        raise ValueError('invalid or test_only PolyBench proxy evidence')
    main = [row for row in rows if 'request' in row]
    if (type(result.get('requests')) is not int or result['requests'] <= 0 or
            not any(type(row.get('status')) is int and 200 <= row['status'] < 300 and
                    not row.get('stream_error') for row in main) or
            any(row.get('model') != request.get('model') for row in main) or
            not isinstance(request.get('model'), str) or not request['model'] or
            result.get('model') != request['model'] or result.get('reasoning') != request.get('reasoning')):
        raise ValueError('PolyBench requires an observed successful request for the bound model')
    if c._path(result['proxy_log'], folder) != folder / 'agent/ctxpress-requests.jsonl':
        raise ValueError('PolyBench proxy log is not from this Agent attempt')
    raw = c._read(result['proxy_log'], folder, evidence, document=False)
    if [json.loads(line) for line in raw.decode().splitlines() if line.strip()] != rows:
        raise ValueError('PolyBench proxy log differs from recorded requests')
    return request


def _artifacts(result, folder, evidence):
    from ctxpress.harness.results import task_compare as c
    records = c._object(result.get('official_artifacts'))
    if not {'model.patch', 'predictions.jsonl'} <= set(records):
        raise ValueError('PolyBench submission artifacts missing')
    for name, row in records.items():
        path = Path(name)
        if (path.is_absolute() or path.as_posix() != name or '..' in path.parts or
                not isinstance(row, dict) or c._path(row['path'], folder) != c._path(name, folder)):
            raise ValueError('PolyBench artifact name/path differs')
    actual = {'model.patch', 'predictions.jsonl'} | {
        'official-logs/' + p.relative_to(folder / 'official-logs').as_posix()
        for p in (folder / 'official-logs').rglob('*') if p.is_file() or p.is_symlink()}
    if set(records) != actual:
        raise ValueError('PolyBench original grading artifact coverage changed')
    return c._records(records, folder, evidence)


def _outcome(spec, result, folder, evidence):
    from ctxpress.harness.results import task_compare as c
    from ctxpress.benchmarks.polybench import protocol as poly_protocol
    from ctxpress.benchmarks.polybench.adapter import PolyBench
    from ctxpress.harness.runtime import execution_health
    request = _request(spec, result, folder, evidence)
    grade = _real(c._object(result.get('grade')))
    if grade.get('infra_invalid') is not False or type(grade.get('resolved')) is not bool:
        raise ValueError('PolyBench official boolean grade missing or invalid')
    state = _real(c._read(folder / 'swe-worker-result.json', folder, evidence))
    separation = dict(agent_image=request['agent_image'], verifier_image=request['grading_image'],
                      checked_cleanup=True, author_grading=True, author_patch_application=True)
    for document in (result, state):
        if (document.get('separate_verifier') != separation or document.get('swe_api') != 'polybench' or
                document.get('protocol') != 'ctxpress_comparison' or
                document.get('network_policy') != 'no_tool_network_model_socket_only' or
                document.get('grading_run_id') != request['project']):
            raise ValueError('PolyBench independent author worker evidence differs')
    if (not isinstance(result.get('stop'), str) or not result['stop'] or type(result.get('calls')) is not int or
            result['calls'] < 0 or state.get('stop') != result['stop'] or type(state.get('calls')) is not int or
            state['calls'] != result['calls'] or state.get('official_artifacts') != result.get('official_artifacts')):
        raise ValueError('PolyBench actual worker result differs')
    exception = state.get('agent_exception')
    if ((result.get('agent_exception') is not None and result['agent_exception'] != exception) or
            grade.get('agent_exception') != (dict(exception_type=exception) if exception else None)):
        raise ValueError('PolyBench recorded Agent exit evidence differs')
    health = execution_health.observe(result)
    if health['execution_invalid'] or health.get('evidence_errors'):
        raise ValueError('PolyBench tool infrastructure failed; grade is diagnostic only')
    documents = _artifacts(result, folder, evidence)
    patch = documents['model.patch'].decode('utf-8')
    predictions = [json.loads(line) for line in documents['predictions.jsonl'].decode().splitlines() if line.strip()]
    if predictions != [dict(instance_id=spec['task']['id'], model_name_or_path=result['model'], model_patch=patch)]:
        raise ValueError('PolyBench prediction does not bind instance/model/captured patch')
    # Guard every path before the dataset helper can open it. Do not read live
    # source data or call prepared_instance with an author Pydantic class.
    directory = folder.parents[2]
    for item in request['task']['inputs']:
        c._read(item['path'], directory, evidence, item['sha256'], document=False)
    poly_protocol.prepared_instance(request['task'], lambda **fields: fields)
    owners = []
    for role in ('agent', 'verifier'):
        owner = _real(c._read(folder / ('resources-swe-' + role + '.json'), folder, evidence))
        identifier = owner.get('container_id')
        expected_image = request['agent_image' if role == 'agent' else 'grading_image']
        if (owner.get('schema') != 'ctxpress.eval.swe_resources' or type(owner.get('version')) is not int or
                owner['version'] != 1 or owner.get('project') != request['project'] or owner.get('role') != role or
                owner.get('label') != request.get('label') or owner.get('image') != expected_image or
                owner.get('container') != request['project'] + ('-agent' if role == 'agent' else '-grade') or
                owner.get('cleaned') is not True or owner.get('credentials_may_exist') is not False or
                owner.get('phase') not in ('stopped', 'recovered') or
                not isinstance(owner.get('daemon_id'), str) or not owner['daemon_id'] or
                owner.get('task_id', spec['task']['id']) != spec['task']['id'] or
                (identifier is not None and (not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{64}', identifier))) or
                (identifier is None and (role == 'agent' or patch.strip()))):
            raise ValueError('PolyBench cleanup ownership is incomplete or differs')
        owners.append(owner)
    if (owners[0]['daemon_id'] != owners[1]['daemon_id'] or not owners[0].get('channel') or
            owners[1].get('channel') is not None or owners[0]['container_id'] == owners[1].get('container_id')):
        raise ValueError('PolyBench verifier isolation differs')
    report_name = 'official-logs/' + spec['task']['id'] + '_result.json'
    report = folder / report_name
    if (report_name not in documents or any(c._path(document['official_report'], folder) != report
            for document in (result, state)) or c._path(grade['report'], folder) != report):
        raise ValueError('PolyBench author output is not from this instance/attempt')
    raw = _real(c._object(json.loads(documents[report_name])))
    c._read(report, folder, evidence, grade['report_sha256'])
    observed = PolyBench().read_grade(request['task'], report)
    if (observed.get('error') or observed.get('infra_invalid') is not False or
            any(grade.get(key) != value for key, value in observed.items()) or
            raw != grade.get('raw_instance_report') or raw.get('generation') is not bool(patch.strip()) or
            (not patch.strip() and (raw.get('patch_applied') or raw.get('with_logs')))):
        raise ValueError('PolyBench grade differs from original author boolean output')
    return request, raw


def binding(plan, spec, result, folder, directory, paths, evidence):
    """Bind actual dispatch, inputs, binary bundle and frozen official resources."""
    from ctxpress.harness.results import task_compare as c
    from ctxpress.harness.jobs import resources as task_resources
    from ctxpress.benchmarks.polybench import protocol as poly_protocol
    from ctxpress.harness.runtime.codex_binary import requires_companion
    from ctxpress.core import toml
    folder, directory = Path(folder).resolve(), Path(directory).resolve()
    config = plan['config']
    if (config.get('benchmark') != 'swe-polybench' or config.get('start_mode') != 'task_start' or
            spec not in plan['jobs'] or folder != directory / 'jobs' / spec['id'] / 'attempt-1'):
        raise ValueError('PolyBench attempt is not bound to its frozen job')
    request = _request(spec, result, folder, evidence)
    upstream = config['environment'].get('upstream') or 'https://chatgpt.com/backend-api/codex'
    url = urlsplit(upstream)
    if url.scheme != 'https' or not url.hostname or url.username is not None or url.password is not None or url.fragment:
        raise ValueError('PolyBench frozen provider address is invalid')
    target = ('[' + url.hostname + ']' if ':' in url.hostname else url.hostname) + ':' + str(url.port or 443)
    expected = dict(task=c.task_api.remap(spec['task'], paths), model=config['model'], reasoning=config['reasoning'],
        method=c._expected_method(spec['method'], plan, folder, evidence), run=config['run'],
        compact_limit=spec['compact_limit'], label=plan['sha256'][:12] + '-' + spec['id'],
        package=str(directory / 'runtime'), official=str(directory / 'inputs/official-inputs'),
        profiles=str(folder / 'method-inputs'), bindir=str(directory / 'inputs/bin'),
        upstream=upstream, target=target, via=config['environment'].get('via'), verifier_cap_add=[])
    if (any(request.get(key) != value for key, value in expected.items()) or
            result.get('method') != c.eval_inputs.method(spec['method'], paths) or
            result.get('binary_version') != request.get('binary_version') or not request.get('binary_version')):
        raise ValueError('PolyBench actual driver request differs from frozen plan')
    lock = plan['task_resources'];source = config['environment']['resources']
    if (c._read(paths[source], directory, evidence, plan['artifacts'][source]) != lock or
            spec['resources'] != lock['tasks'][spec['task']['id']] or request.get('runtime') != lock.get('runtime')):
        raise ValueError('PolyBench actual resources/runtime differ from frozen plan')
    task_resources.verify(lock, 'swe-polybench', [spec['task']])
    if poly_protocol.requirements(config, [request['task']], lock):
        raise ValueError('PolyBench frozen official resource requirements are incomplete')
    for item in request['task']['inputs']:
        c._read(item['path'], directory, evidence, item['sha256'], document=False)
    names = ['codex'] + (['codex-code-mode-host'] if requires_companion(request['binary_version']) else [])
    for name in names:
        source = str(Path(config['environment']['bindir']) / name)
        if source not in plan['artifacts'] or c._path(paths[source], directory) != directory / 'inputs/bin' / name:
            raise ValueError('PolyBench frozen binary bundle missing or unbound')
        c._read(paths[source], directory, evidence, plan['artifacts'][source], document=False)
    # The frontend already hashes all frozen input copies and execution modes.
    # Bind the complete file selection here, then read only author source and
    # distribution identity; do not rehash thousands of dependency binaries.
    for key, tree in lock['trees'].items():
        for name, digest in tree['files'].items():
            source = str(Path(tree['root']) / name)
            target_path = directory / 'inputs/official-inputs' / key / name
            if plan['artifacts'].get(source) != digest or Path(paths[source]) != target_path:
                raise ValueError('PolyBench official file is not bound to its frozen copy')
            if key == 'polybench':
                c._read(target_path, directory, evidence, digest, document=False)
    project = toml.loads(c._read(Path(request['official']) / 'polybench/pyproject.toml', directory,
                               evidence, document=False).decode())['project']
    metadata = [name for name in lock['trees']['dependencies']['files']
                if re.fullmatch(r'poly_bench_evaluation-[^/]+\.dist-info/METADATA', name)]
    if len(metadata) != 1:
        raise ValueError('PolyBench real distribution metadata missing/ambiguous')
    declared = Parser().parsestr(c._read(Path(request['official']) / 'dependencies' / metadata[0],
                                        directory, evidence, document=False).decode())
    if (project.get('name', '').replace('-', '_') != 'poly_bench_evaluation' or
            declared.get('Name', '').replace('-', '_') != 'poly_bench_evaluation' or
            not declared.get('Version') or declared['Version'] == 'local-dev' or
            project.get('version') != declared['Version']):
        raise ValueError('PolyBench frozen author source/distribution versions differ')


def quality(spec, result, folder, evidence):
    """Preserve the recorded author boolean; never calculate a replacement score."""
    request, raw = _outcome(spec, result, Path(folder).resolve(), evidence)
    return {'repository.resolved': raw['resolved']}, dict(kind='boolean_issue_outcome',
        author_interface='poly_bench_evaluation.evaluate_instance', raw_instance_report=raw,
        independent_grading=True, retrieval_metrics_supported=False, runtime=request['runtime'])
