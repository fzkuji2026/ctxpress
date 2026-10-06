"""Read frozen Harbor/Pier reports without importing or rerunning their graders.

Native rewards remain named numeric observations. A shared verifier or a legacy
Pro trial without retained artifact receipts is an explicitly unsupported variant.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

BENCHMARKS = ('terminal-bench', 'terminal-bench-science', 'deep-swe', 'swe-bench-pro')


class UnsupportedVariant(ValueError):
    """The family has a reader, but this variant lacks the required evidence."""


def _common():
    from ctxpress.harness import eval_task_compare
    return eval_task_compare


def binding(plan, spec, result, folder, directory, paths, evidence):
    c = _common()
    if spec['task']['initial_state'].get('pro_version') == 'v1':
        raise UnsupportedVariant('Pro V1 author Docker pipeline is outside the Pro V2 replay reader')
    request = c._read(folder / 'harbor-request.json', folder, evidence)
    if request.get('test_only') or request.get('pro_replay'):
        raise ValueError('test_only or replay request cannot be a real model execution')
    moved = c.task_api.remap(spec['task'], paths)
    configs = [Path(item['path']) for item in moved['inputs'] if item['role'] == 'runtime' and Path(item['path']).name == 'task.toml']
    if len(configs) != 1:
        raise ValueError('Harbor task must bind exactly one official task.toml')
    expected = dict(schema='ctxpress.eval.harbor_trial', version=1, task_id=moved['id'], benchmark=moved['benchmark'],
        task=str(configs[0].parent), package=str(directory / 'runtime'), folder=str(folder),
        official=str(directory / 'inputs' / 'official-inputs'), profiles=str(folder / 'method-inputs'),
        bindir=str(directory / 'inputs' / 'bin'), model=plan['config']['model'],
        reasoning=plan['config']['reasoning'], method=c._expected_method(spec['method'], plan, folder, evidence),
        run=plan['config']['run'], compact_limit=spec['compact_limit'], label=plan['sha256'][:12] + '-' + spec['id'],
        runtime=plan['task_resources']['runtime'], gpu_device_ids=spec['resources'].get('gpu_device_ids'))
    if any(request.get(k) != v for k, v in expected.items()):
        raise ValueError('actual Harbor dispatch differs from frozen specification/runtime/task')
    project = request.get('project')
    if not isinstance(project, str) or not re.fullmatch(r'ctxp-hb-[0-9a-f]{24}', project):
        raise ValueError('invalid Harbor trial project')
    if (result.get('binary_version') != request.get('binary_version') or not request.get('binary_version') or
            c._path(result['proxy_log'], folder) != folder / project / 'agent' / 'ctxpress-requests.jsonl'):
        raise ValueError('Harbor binary/proxy trial path differs')
    images = spec['resources']['images']
    expected_images = dict(main=images['agent']['id'], **{k: v['id'] for k, v in images.get('services', {}).items()})
    if request.get('images') != expected_images:
        raise ValueError('actual Harbor Agent/service images differ')
    deep = moved['benchmark'] == 'deep-swe'
    pro = moved['benchmark'] == 'swe-bench-pro'
    if request.get('framework', 'harbor') != ('pier' if deep else 'harbor'):
        raise ValueError('actual official framework differs')
    from ctxpress.benchmarks import harbor_protocol
    if not deep and request.get('harbor_api', 'legacy') != harbor_protocol.api(plan['task_resources']):
        raise ValueError('actual Harbor API differs from frozen source')
    separate = 'verifier_environment' in moved['initial_state']
    if not deep and request.get('harbor_api') == 'modern':
        if request.get('separate_verifier') is not separate:
            raise ValueError('actual Harbor verifier mode differs')
        if separate:
            bundled = bool((moved['evaluation']['verifier'].get('environment') or {}).get('docker_image') or
                           (Path(request['task']) / 'tests' / 'Dockerfile').is_file())
            if (request.get('verifier_bundled_tests') is not bundled or
                    request.get('verifier_gpu_device_ids') != spec['resources'].get('verifier_gpu_device_ids')):
                raise ValueError('actual verifier tests/GPU contract differs')
    if deep or pro or separate:
        if set(images['grading']) != {'verifier'} or request.get('grading_image') != images['grading']['verifier']['id']:
            raise ValueError('actual Harbor/Pier grading image differs')
    elif any(v['id'] != images['agent']['id'] for v in images['grading'].values()):
        raise ValueError('shared verifier image differs')
    if pro and (request.get('pro_version') != 'v2' or request.get('pro_base_commit') != moved['initial_state']['base_commit'] or
                moved['evaluation']['dataset']['benchmark_version'] != 'v2' or
                moved['evaluation']['dataset']['revision'] != plan['task_resources']['release']):
        raise ValueError('Pro V2 version/base commit/release differs')


def _config(raw, request, folder, project, *, replay=False):
    """Check both author config serializations, including defaults and mounts."""
    if raw.get('test_only') or raw.get('install_only'):
        raise ValueError('test_only/install_only official trial')
    module = ('pier_trial' if request.get('framework') == 'pier' else
              'harbor_modern' if request.get('harbor_api') == 'modern' else 'harbor_worker')
    expected_model = 'replay' if replay else request['model']
    agent = raw.get('agent') or {}; env = raw.get('environment') or {}; verifier = raw.get('verifier') or {}
    if (raw.get('trial_name') != project or raw.get('trials_dir') != str(folder) or
            (raw.get('task') or {}).get('path') != request['task'] or
            agent.get('model_name') != expected_model or
            agent.get('import_path') != 'ctxpress.harness.' + module + ':CtxpressCodex' or
            agent.get('override_timeout_sec') != (300 if replay else request['run']['timeout']) or
            env.get('import_path') != 'ctxpress.harness.' + module + ':CtxpressDocker' or
            env.get('force_build', False) is not False or env.get('delete', True) is not True or
            verifier.get('disable', False) is not (request['benchmark'] == 'swe-bench-pro' and not replay)):
        raise ValueError('official trial config differs from frozen dispatch')
    for key in ('timeout_multiplier',):
        if raw.get(key, 1.0) != 1.0:
            raise ValueError('official timeout multiplier differs')
    for key in ('agent_timeout_multiplier', 'verifier_timeout_multiplier', 'agent_setup_timeout_multiplier',
                'environment_build_timeout_multiplier', 'user_agent', 'source_trial'):
        if raw.get(key) is not None:
            raise ValueError('unexpected official config override: ' + key)
    for key in ('override_setup_timeout_sec', 'max_timeout_sec', 'kwargs', 'env', 'skills',
                'extra_allowed_hosts', 'mcp_servers', 'resume_trajectory', 'load_trajectory'):
        if agent.get(key):
            raise ValueError('unexpected official agent override: ' + key)
    for key in ('override_cpus', 'override_memory_mb', 'override_storage_mb', 'override_gpus', 'override_tpu',
                'kwargs', 'extra_allowed_hosts', 'extra_docker_compose'):
        if env.get(key) is not None and env.get(key) != [] and env.get(key) != {}:
            raise ValueError('unexpected official environment override: ' + key)
    if any(verifier.get(k) for k in ('override_timeout_sec', 'max_timeout_sec', 'env')):
        raise ValueError('unexpected official verifier override')
    if any(raw.get(k) for k in ('extra_instructions', 'extra_instruction_paths', 'artifacts')):
        raise ValueError('unexpected official task instructions/artifacts override')
    mounts = env.get('mounts') or []
    if replay:
        if mounts:
            raise ValueError('Pro replay received model/runtime mounts')
        return
    expected = {'/ctxpress-runtime': request['package'], '/cxbin': request['bindir'], '/ctxpress-method': request['profiles']}
    seen = set()
    for mount in mounts:
        target = mount.get('target'); source = mount.get('source')
        if target in seen or mount.get('type') != 'bind' or (mount.get('bind') or {}).get('create_host_path') is not False:
            raise ValueError('official mount contract differs')
        seen.add(target)
        if target in expected:
            if source != expected[target] or mount.get('read_only') is not True:
                raise ValueError('official runtime/input mount differs')
        elif target == '/ctxpress-channel':
            if not isinstance(source, str) or not Path(source).is_absolute() or mount.get('read_only') is not True:
                raise ValueError('official model channel mount differs')
        elif request.get('framework') == 'pier' and target in ('/logs/agent', '/logs/artifacts'):
            if source != str(folder / project / target.rsplit('/', 1)[1]) or mount.get('read_only', False):
                raise ValueError('Pier author logs/artifacts mount differs')
        else:
            raise ValueError('unexpected official trial mount')
    if not set(expected) | {'/ctxpress-channel'} <= seen:
        raise ValueError('official runtime/input mounts missing')
    if request.get('framework') == 'pier' and not {'/logs/agent', '/logs/artifacts'} <= seen:
        raise ValueError('Pier author artifact mounts missing')


def _pro_agent_name(request, evidence):
    """Read the inherited author name from already verified frozen tooling.

    pro_trial.capture_factory instruments LockedCodex without replacing name().
    Inspect its literal declaration without importing or executing author code;
    the common binding has verified this entire input tree against the plan.
    """
    root = Path(request['official'])
    source = _common()._read(root / 'pro_tooling/locked_codex.py', root, evidence, document=False)
    try:
        module = ast.parse(source)
    except SyntaxError as error:
        raise UnsupportedVariant('Pro LockedCodex name declaration is not readable') from error
    classes = [node for node in module.body if isinstance(node, ast.ClassDef) and node.name == 'LockedCodex']
    if len(classes) == 1 and not classes[0].decorator_list and len(classes[0].bases) == 1:
        base = classes[0].bases[0]
        methods = [node for node in classes[0].body if isinstance(node, ast.FunctionDef) and node.name == 'name']
        if isinstance(base, ast.Name) and base.id == 'Codex' and len(methods) == 1:
            method = methods[0]
            if (len(method.decorator_list) == 1 and isinstance(method.decorator_list[0], ast.Name) and
                    method.decorator_list[0].id == 'staticmethod' and not method.args.posonlyargs and
                    not method.args.args and not method.args.kwonlyargs and not method.args.vararg and
                    not method.args.kwarg and len(method.body) == 1 and isinstance(method.body[0], ast.Return)):
                value = method.body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
                    return value.value
    raise UnsupportedVariant('Pro LockedCodex requires a static literal author name declaration')


def _trial(raw, request, folder, project, task_name, evidence, *, replay=False):
    c = _common(); root = folder / project
    if (raw.get('test_only') or raw.get('task_name') != task_name or raw.get('trial_name') != project or
            (raw.get('task_id') or {}).get('path') != request['task'] or raw.get('trial_uri') != root.as_uri() or
            not raw.get('started_at') or not raw.get('finished_at')):
        raise ValueError('official report task/trial identity or lifecycle differs')
    info = raw.get('agent_info') or {}
    expected_name = _pro_agent_name(request, evidence) if not replay and request['benchmark'] == 'swe-bench-pro' else 'codex'
    if (not replay and (info.get('name') != expected_name or info.get('version') != request['binary_version'] or
                       (info.get('model_info') or {}).get('name') != request['model'])):
        raise ValueError('official Agent model/binary identity differs')
    for value in (raw.get('config') or {}, c._read(root / 'config.json', folder, evidence)):
        _config(value, request, folder, project, replay=replay)
    exception = raw.get('exception_info')
    if exception and (not isinstance(exception, dict) or exception.get('exception_type') not in
                      ('AgentTimeoutError', 'NonZeroAgentExitCodeError') or replay):
        raise ValueError('official trial infrastructure/grading exception')
    return exception


def _owner(request, folder, project, images, role, evidence, *, expected_sha=None, compose_root=None):
    c = _common()
    row = c._read(folder / ('resources-harbor-' + project + '.json'), folder, evidence, expected_sha)
    if (row.get('schema') != 'ctxpress.eval.harbor_resources' or row.get('version') != 1 or
            row.get('project') != project or row.get('label') != request['label'] or row.get('images') != images or
            row.get('role', 'agent') != role or row.get('cleaned') is not True or
            row.get('credentials_may_exist') is not False or row.get('phase') not in ('stopped', 'recovered') or
            not row.get('daemon_id') or role == 'verifier' and row.get('channel') is not None):
        raise ValueError('official phase image/ownership/checked cleanup differs')
    if request['benchmark'] == 'swe-bench-pro' and row.get('pro_base_commit') != request['pro_base_commit']:
        raise ValueError('Pro phase base repository differs')
    root = compose_root or folder / request['project']
    compose = root / ('ctxpress-compose-' + role + '.json' if request['benchmark'] != 'swe-bench-pro' else 'ctxpress-compose-agent.json')
    if not row.get('compose_sha256'):
        raise ValueError('recorded actual Compose hash missing')
    document = c._read(compose, folder, evidence, row['compose_sha256'])
    services = document.get('services') or {}
    if {name: value.get('image') for name, value in services.items()} != images or any(v.get('build') for v in services.values()):
        raise ValueError('actual Compose service/image set differs')
    from ctxpress.core import toml
    task_root = Path(request['task'])
    task_config = toml.loads(c._read(task_root / 'task.toml', task_root, evidence, document=False).decode())
    limits = task_config.get('environment', {})
    if role == 'verifier' and request['benchmark'] != 'swe-bench-pro':
        limits = (task_config.get('verifier') or {}).get('environment') or limits
    main = services['main']
    deployed = ((main.get('deploy') or {}).get('resources') or {}).get('limits') or {}
    # Modern Harbor uses service cpus/mem_limit; Pier emits the equivalent
    # deploy.resources.limits. Check every declared representation, so a
    # conflicting second limit cannot be hidden by selecting one fallback.
    cpu_values = [v for v in (main.get('cpus'), deployed.get('cpus')) if v is not None]
    memory_values = [v for v in (main.get('mem_limit'), deployed.get('memory')) if v is not None]
    def cpu_equal(value):
        return (c._number(value) or isinstance(value, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value)) and float(value) == limits['cpus']
    if ('cpus' in limits and (not cpu_values or not all(cpu_equal(v) for v in cpu_values)) or
            'memory_mb' in limits and (not memory_values or any(str(v) != str(limits['memory_mb'] * 1024 * 1024) for v in memory_values))):
        raise ValueError('actual Compose CPU/memory differs from frozen task resources')
    gpu_ids = request.get('gpu_device_ids' if role == 'agent' else 'verifier_gpu_device_ids')
    if row.get('gpu_device_ids') != gpu_ids:
        raise ValueError('official phase GPU allocation differs')
    return row


def _artifacts(records, folder, root, evidence, *, regrade_project=None):
    c = _common(); documents = c._records(records, folder, evidence)
    for name, record in records.items():
        base = folder if regrade_project and (name.startswith(regrade_project + '/') or name in ('pro-regrade.json', 'pro-submission/model.patch')) else root
        if c._path(record['path'], folder) != c._path(base / name, folder):
            raise ValueError('official artifact trial name/path differs')
        if name.endswith('.json'):
            raw = json.loads(documents[name])
            if isinstance(raw, dict) and raw.get('test_only'):
                raise ValueError('test_only official artifact')
    # Addition/removal of unrecorded results is evidence drift too.
    actual = {p.relative_to(root).as_posix() for sub in ('artifacts', 'verifier') for p in (root / sub).rglob('*') if p.is_file()}
    expected = {k for k in records if k.startswith(('artifacts/', 'verifier/'))}
    if actual != expected:
        raise ValueError('official artifact coverage changed')
    return documents


def _rewards(documents, rewards):
    c = _common()
    if 'verifier/reward.json' in documents:
        if c._object(json.loads(documents['verifier/reward.json'])) != rewards:
            raise ValueError('original reward artifact differs from official report')
    elif 'verifier/reward.txt' not in documents:
        raise ValueError('original verifier reward artifact missing')
    if 'verifier/reward.txt' in documents:
        value = float(documents['verifier/reward.txt'].decode().strip())
        if not c._number(value) or rewards.get('reward') != value:
            raise ValueError('original scalar reward differs from official report')


def _versions(request, evidence):
    c = _common(); root = Path(request['official'])
    from ctxpress.core import toml
    source = 'pier' if request.get('framework') == 'pier' else 'harbor'
    raw = c._read(root / source / 'pyproject.toml', root, evidence, document=False)
    version = toml.loads(raw.decode())['project']['version']
    distribution = 'datacurve-pier' if source == 'pier' else 'harbor'
    matches = list((root / 'dependencies').glob(('datacurve_pier' if source == 'pier' else 'harbor') + '-*.dist-info/METADATA'))
    if len(matches) != 1:
        raise ValueError('official framework distribution identity missing/ambiguous')
    text = c._read(matches[0], root, evidence, document=False).decode()
    if not re.search(r'^Version: ' + re.escape(version) + r'\s*$', text, re.M) or not re.search(r'^Name: ' + distribution + r'\s*$', text, re.M):
        raise ValueError('official source/distribution versions differ')
    return dict(framework=source, version=version, runtime=request['runtime'])


def quality(spec, result, folder, evidence):
    c = _common(); request = c._read(folder / 'harbor-request.json', folder, evidence)
    deep = request.get('framework') == 'pier'; pro = request['benchmark'] == 'swe-bench-pro'
    if not deep and request.get('harbor_api') != 'modern':
        raise UnsupportedVariant('legacy Harbor trial has no complete retained author artifact chain; modern/Pier evidence required')
    if not deep and not pro and request.get('separate_verifier') is not True:
        raise UnsupportedVariant('shared Harbor verifier cannot prove independent author grading; separate verifier required')
    grade = c._object(result.get('grade'))
    if (grade.get('infra_invalid') is not False or grade.get('valid_rewards') is not True or
            grade.get('test_only') or grade.get('error') or grade.get('failure_kind')):
        raise ValueError('missing or invalid official named rewards')
    rewards = grade.get('rewards')
    if not isinstance(rewards, dict) or not rewards or any(not isinstance(k, str) or not k or not c._number(v) for k, v in rewards.items()):
        raise ValueError('official reward missing, boolean or nonfinite')
    if (deep or pro) and rewards.get('reward') not in (0, 1):
        raise ValueError('author binary reward is invalid/error sentinel')
    state = c._read(folder / 'harbor-worker-result.json', folder, evidence)
    for key in ('separate_verifier', 'official_artifacts', 'agent_report', 'regrade_report', 'submission', 'pro_version', 'fresh_regrade',
                'harbor_api', 'network_policy', 'gpu', 'verifier_gpu'):
        if state.get(key) != result.get(key):
            raise ValueError('official worker/result evidence differs: ' + key)
    if state.get('stop') != result.get('stop') or state.get('calls') != result.get('calls'):
        raise ValueError('official worker stop/calls differ')
    project = request['project']; root = folder / project
    grader = 'ctxp-hb-' + hashlib.sha256((project + (':pro-regrade' if pro else ':verifier')).encode()).hexdigest()[:24]
    report_path = folder / (grader if pro else project) / 'result.json'
    if c._path(grade['report'], folder) != report_path or c._path(result['official_report'], folder) != report_path:
        raise ValueError('authoritative report is not this trial/regrade')
    if not grade.get('report_sha256'):
        raise ValueError('official report hash missing')
    report = c._read(report_path, folder, evidence, grade['report_sha256'])
    task_name = spec['task']['initial_state'].get('pier_task_name', spec['task']['initial_state'].get('official_task_name', spec['task']['id']))
    exception = _trial(report, request, folder, grader if pro else project, task_name, evidence, replay=pro)
    if ((report.get('verifier_result') or {}).get('rewards') != rewards or grade.get('trial_name') != report.get('trial_name') or
            grade.get('agent_exception') != exception):
        raise ValueError('official trial rewards/exception differ from recorded grade')
    records = result.get('official_artifacts')
    documents = _artifacts(records, folder, root, evidence, regrade_project=grader if pro else None)
    if 'artifacts/manifest.json' not in documents:
        raise ValueError('original author artifact manifest missing')
    if pro:
        _pro(spec, result, request, report, folder, project, grader, evidence, documents)
        authoritative = {k[len(grader)+1:]: v for k, v in documents.items() if k.startswith(grader + '/')}
        actual = {p.relative_to(folder / grader).as_posix() for sub in ('artifacts', 'verifier') for p in (folder / grader / sub).rglob('*') if p.is_file()}
        if actual != set(authoritative):
            raise ValueError('Pro replay artifact coverage changed')
        _rewards(authoritative, rewards)
    else:
        separation = result.get('separate_verifier') or {}
        required = dict(agent_project=project, verifier_project=grader, agent_image=request['images']['main'],
                        verifier_image=request['grading_image'], author_collect=True, checked_cleanup=True)
        required.update(patch_only_transfer=True) if deep else required.update(author_artifact_handler=True, verifier_started=True,
                                                                           bundled_tests=request['verifier_bundled_tests'])
        if any(separation.get(k) != v for k, v in required.items()):
            raise ValueError('official independent verifier evidence differs')
        owners = [_owner(request, folder, project, request['images'], 'agent', evidence),
                  _owner(request, folder, grader, {'main': request['grading_image']}, 'verifier', evidence)]
        if owners[0]['daemon_id'] != owners[1]['daemon_id']:
            raise ValueError('official phases used different Docker daemons')
        if not deep and report.get('verifier_environment_mode') != 'separate':
            raise ValueError('official report used shared verifier')
        _rewards(documents, rewards)
        if deep:
            patch = documents.get('artifacts/model.patch')
            # Author permits a missing/empty patch; transfer must faithfully
            # preserve that case rather than manufacture a submission.
            staging = list(root.glob('submission-*'))
            if len(staging) != 1 or any(p.name != 'model.patch' for p in staging[0].iterdir()):
                raise ValueError('Pier patch-only staging missing/ambiguous')
            staged = staging[0] / 'model.patch'
            if staged.exists():
                if patch is None or c._read(staged, folder, evidence, document=False) != patch:
                    raise ValueError('Pier staged patch differs from author artifact')
            elif patch is not None:
                raise ValueError('Pier staged patch missing')
    versions = _versions(request, evidence)
    return {'rewards.' + name: value for name, value in rewards.items()}, dict(kind='author_named_rewards', rewards=rewards,
        versions=versions, dataset=spec['task']['evaluation']['dataset'], independent_verifier=True,
        authoritative_phase='fresh_regrade' if pro else 'official_verifier', benchmark_version='v2' if pro else None,
        published_protocol_reproduced=False if pro else None,
        scope='native numeric author rewards; no implicit solved Boolean or cross-family quality scale')


def _pro(spec, result, request, report, folder, project, grader, evidence, documents):
    c = _common()
    if result.get('pro_version') != 'v2' or spec['task']['initial_state'].get('pro_version') != 'v2':
        raise ValueError('Pro comparison requires declared V2 semantics')
    if 'pro-regrade.json' not in documents or 'pro-submission/model.patch' not in documents:
        raise ValueError('recorded Pro replay/submission artifacts missing')
    record = c._object(json.loads(documents['pro-regrade.json']))
    expected = dict(schema='ctxpress.eval.pro_regrade', version=1, task_id=spec['task']['id'], benchmark_version='v2',
        base_commit=spec['task']['initial_state']['base_commit'], agent_project=project, regrade_project=grader,
        agent_image=request['images']['main'], regrade_image=request['grading_image'], checked_agent_cleanup=True,
        regrade_model_calls=0, published_protocol_reproduced=False)
    if any(record.get(k) != v or type(record.get(k)) is not type(v) for k, v in expected.items()):
        raise ValueError('Pro fresh replay identity/base/version/lifecycle differs')
    grade = result['grade']
    grade_evidence = dict(record, path=str(folder / 'pro-regrade.json'), sha256=hashlib.sha256(documents['pro-regrade.json']).hexdigest())
    if (result.get('fresh_regrade') != record or grade.get('fresh_regrade') != grade_evidence or
            grade.get('authoritative_phase') != 'fresh_regrade' or grade.get('published_protocol_reproduced') is not False):
        raise ValueError('Pro recorded grade is not the independently regraded result')
    for key in ('agent_report', 'regrade_report', 'submission'):
        if record.get(key) != result.get(key):
            raise ValueError('Pro recorded phase/submission differs')
    if record['regrade_report'] != {'path':str(folder / grader / 'result.json'), 'sha256':grade['report_sha256']}:
        raise ValueError('Pro authoritative regrade report binding differs')
    primary = record['agent_report']
    if c._path(primary['path'], folder) != folder / project / 'result.json':
        raise ValueError('Pro captured Agent report path differs')
    agent = c._read(primary['path'], folder, evidence, primary['sha256'])
    task_name = spec['task']['initial_state'].get('official_task_name', spec['task']['id'])
    _trial(agent, request, folder, project, task_name, evidence)
    if agent.get('verifier_result') is not None:
        raise ValueError('Pro Agent sandbox performed grading')
    replay_usage = report.get('agent_result') or {}
    if (replay_usage.get('model_usage') or any(replay_usage.get(k) for k in
            ('n_input_tokens', 'n_output_tokens', 'n_cache_tokens', 'cost_usd')) or
            (folder / grader / 'agent' / 'ctxpress-requests.jsonl').exists() and
            c._read(folder / grader / 'agent' / 'ctxpress-requests.jsonl', folder, evidence, document=False).strip()):
        raise ValueError('Pro replay reports model usage despite zero-model contract')
    owners = [_owner(request, folder, project, request['images'], 'agent', evidence,
                     expected_sha=record['agent_resources_sha256'], compose_root=folder / project),
              _owner(request, folder, grader, {'main':request['grading_image']}, 'verifier', evidence,
                     expected_sha=record['regrade_resources_sha256'], compose_root=folder / grader)]
    if owners[0]['daemon_id'] != owners[1]['daemon_id']:
        raise ValueError('Pro regrade resource owner differs')
    if c._read(folder / project / 'agent' / 'model.patch', folder, evidence, record['submission']['sha256'], document=False) != documents['pro-submission/model.patch']:
        raise ValueError('Pro patch capture/transfer differs')
    separation = result.get('separate_verifier') or {}
    if (separation.get('agent_image') != record['agent_image'] or separation.get('verifier_image') != record['regrade_image'] or
            any(separation.get(k) is not True for k in ('checked_cleanup', 'author_patch_replay', 'verifier_started'))):
        raise ValueError('Pro independent replay evidence differs')
