"""Dispatch frozen local tasks through Harbor's official Trial and verifier.

This module uses only stdlib and ctxpress. The official framework is imported
in an isolated child with explicitly captured source and dependency trees.
"""
from __future__ import annotations
import copy, json, os, re, shutil, signal, subprocess, tempfile, time, urllib.parse, uuid
from pathlib import Path
from ctxpress.harness.jobs import environment as eval_environment, plan as eval_plan, resources as task_resources
from ctxpress.core import processes
from ctxpress.live.telemetry import summary
from ctxpress.benchmarks.harbor import codex_hook as harbor_codex, gpu as harbor_gpu, protocol as harbor_protocol

SCHEMA = 'ctxpress.eval.harbor_resources'
WORKER = Path(__file__).with_name('worker.py')


def requirements(config, tasks, lock):
    if config.get('benchmark') == 'swe-bench-pro':
        from ctxpress.benchmarks.pro.protocol import requirements as pro_requirements
        return pro_requirements(config,tasks,lock)
    if config.get('benchmark') == 'deep-swe':
        from ctxpress.benchmarks.deepswe.adapter import requirements as deep_requirements
        return deep_requirements(config, tasks, lock)
    missing = []
    if not lock:
        return ['Harbor: captured harbor/dependencies trees and pinned Python runtime']
    if not lock.get('runtime'):
        missing.append('Harbor: pinned Python runtime')
    else:
        runtime = lock['runtime']
        if runtime.get('platform') != 'linux':
            missing.append('Harbor socket execution requires a pinned Linux Python runtime')
        version = re.match(r'([0-9]+)\.([0-9]+)', runtime.get('version', ''))
        if not version or tuple(map(int,version.groups())) < (3,12):
            missing.append('Harbor official runtime requires Python 3.12 or later')
    sources = lock['trees'].get('harbor', {}).get('files', {})
    for name in ('pyproject.toml', 'src/harbor/__init__.py', 'src/harbor/trial/trial.py',
                 'src/harbor/agents/installed/codex.py', 'src/harbor/environments/docker/docker.py'):
        if name not in sources:
            missing.append('Harbor source tree: ' + name)
    dependencies = lock['trees'].get('dependencies', {}).get('files', {})
    if not any(re.fullmatch(r'harbor-[^/]+\.dist-info/METADATA', name) for name in dependencies):
        missing.append('Harbor dependencies tree: real harbor dist-info metadata and transitive dependencies')
    api = harbor_protocol.api(lock)
    if api == 'unsupported':
        missing.append('Harbor frozen API is transitional; capture a legacy Trial or modern SingleStepTrial runtime')
    if api == 'modern':
        for name in ('src/harbor/trial/single_step.py', 'src/harbor/models/task/verifier_mode.py',
                     'src/harbor/environments/capabilities.py'):
            if name not in sources:
                missing.append('Harbor source tree: ' + name)
    for task in tasks:
        verifier = task['evaluation'].get('verifier', {})
        separate = ('verifier_environment' in task['initial_state'] or verifier.get('environment_mode') == 'separate' or
                    verifier.get('environment') is not None)
        if api != 'modern' and (separate or verifier.get('collect')):
            missing.append('Harbor selected runtime does not implement separate verifier/collect protocol: ' + task['id'])
        if task['initial_state'].get('steps'):
            missing.append('Harbor multi-step tasks require a dedicated execution protocol: ' + task['id'])
        environment = task['initial_state']['environment']
        gpu = harbor_gpu.requirements(environment)
        record = lock['tasks'][task['id']]
        if gpu['count'] and not record.get('gpu_device_ids'):
            missing.append('Harbor GPU resource binding: declare ' + str(gpu['count']) + ' full NVIDIA device UUIDs for ' + task['id'])
        try:
            harbor_protocol.validate_verifier_gpus(task, record, require=True)
        except ValueError as error:
            missing.append('Harbor verifier GPU binding: ' + str(error) + ': ' + task['id'])
        images = record['images']
        if api == 'modern' and separate:
            if set(images['grading']) != {'verifier'}:
                missing.append('Harbor separate phase requires exactly one grading.verifier image: ' + task['id'])
            if any(Path(item['path']).parent.name == 'tests' and Path(item['path']).name == 'docker-compose.yaml'
                   for item in task['inputs']):
                missing.append('Harbor separate verifier Compose services are not yet supported: ' + task['id'])
        elif any(image['id'] != images['agent']['id'] for image in images['grading'].values()):
            missing.append('Harbor verifier runs in the task environment; separate grading images need another protocol: ' + task['id'])
        if api == 'modern':
            for phase in (environment, task['initial_state'].get('verifier_environment', {})):
                if phase.get('os', 'linux') != 'linux' or phase.get('tpu'):
                    missing.append('Harbor comparison supports Linux CPU/NVIDIA tasks only: ' + task['id'])
                if phase.get('network_mode') == 'allowlist' or phase.get('allowed_hosts'):
                    missing.append('Harbor task network allowlists require an additional provider: ' + task['id'])
        revision = task['evaluation']['dataset']['revision']
        if revision and revision != lock['release']:
            missing.append('Harbor resource release differs from dataset provenance: ' + task['id'])
        if config['scope'] == 'benchmark' and not revision:
            missing.append('Harbor benchmark scope requires explicit dataset provenance: ' + task['id'])
    return missing


def task_directory(task):
    bindings = [item for item in task['inputs'] if item['role'] == 'runtime' and Path(item['path']).name == 'task.toml']
    if len(bindings) != 1:
        raise ValueError('Harbor task must bind one frozen task.toml')
    root = Path(bindings[0]['path']).resolve().parent
    if root.name != task['id']:
        raise ValueError('frozen Harbor task directory differs from its task ID')
    # Opaque initial_state still records the original location. Execution must
    # derive its local task path from remapped, verified inputs instead.
    for item in task['inputs']:
        if eval_plan.file_sha256(item['path']) != item['sha256']:
            raise ValueError('frozen Harbor task input changed')
    return root


def method_inputs(entry, folder):
    entry = copy.deepcopy(entry)
    folder = Path(folder)
    folder.mkdir()
    def visit(value):
        args = value.get('args') or {}
        field = {'CostModel':'profile', 'AutoCostModel':'policy'}.get(value.get('class'))
        if field and args.get(field) and not isinstance(args[field], dict):
            source = Path(args[field]); digest = eval_plan.file_sha256(source)
            target = folder / (digest + '.json')
            shutil.copyfile(source, target)
            if eval_plan.file_sha256(target) != digest:
                raise ValueError('frozen method input changed during copy')
            args[field] = '/ctxpress-method/' + target.name
        if isinstance(args.get('inner'), dict):
            visit(args['inner'])
        for child in args.get('methods', []):
            visit(child)
    visit(entry)
    return entry


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=60,
                            stdin=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError('managed Harbor Docker operation failed')
    return result.stdout


def owned_resources(record, kind):
    project = record['project']
    arguments = ('ps', '-aq') if kind == 'container' else (kind, 'ls', '-q')
    identifiers = docker(*arguments, '--filter', 'label=com.docker.compose.project=' + project).split()
    records = []
    for identifier in identifiers:
        value = json.loads(docker(kind, 'inspect', identifier))[0]
        labels = value.get('Config', {}).get('Labels') if kind == 'container' else value.get('Labels')
        labels = labels or {}
        if (labels.get('com.docker.compose.project') != project or labels.get('ctxpress.run') != record['label'] or
                labels.get('ctxpress.managed') != 'true'):
            raise ValueError('Harbor recovery found resources without matching ownership')
        if kind == 'container':
            service = labels.get('com.docker.compose.service')
            if service not in record['images'] or value.get('Image') != record['images'][service]:
                raise ValueError('Harbor recovery service or image identity changed')
        records.append(value)
    return records


def cleanup_channel(record):
    if record.get('channel') is None and record.get('role') == 'verifier' and record.get('credentials_may_exist') is False:
        return
    channel = Path(record['channel'])
    if not channel.exists():
        return
    if (channel.is_symlink() or channel.parent != Path(tempfile.gettempdir()).resolve() or
            not channel.name.startswith(record['project'] + '-channel-')):
        raise ValueError('Harbor recovery channel path is not owned')
    owner = json.loads((channel / 'owner.json').read_text(encoding='utf-8'))
    if owner != dict(project=record['project'], label=record['label']):
        raise ValueError('Harbor recovery channel owner changed')
    if any(path.name not in ('owner.json', 'model.sock') for path in channel.iterdir()):
        raise ValueError('Harbor recovery channel contains undeclared files')
    (channel / 'model.sock').unlink(missing_ok=True)
    (channel / 'owner.json').unlink()
    channel.rmdir()


def recover(path, label):
    path = Path(path)
    record = json.loads(path.read_text(encoding='utf-8'))
    if (record.get('schema') != SCHEMA or record.get('version') != 1 or record.get('label') != label or
            not re.fullmatch(r'ctxp-hb-[0-9a-f]{24}', record.get('project', '')) or
            not isinstance(record.get('images'), dict) or 'main' not in record['images'] or
            any(not re.fullmatch(r'sha256:[0-9a-f]{64}', value) for value in record['images'].values())):
        raise ValueError('invalid Harbor recovery journal or owner')
    if record.get('cleaned'):
        cleanup_channel(record)
        return
    if type(record.get('pid')) is int and processes.alive(record['pid'], record.get('identity')):
        raise ValueError('Harbor worker is still running; recovery cannot interrupt another attempt')
    if docker('info', '--format', '{{.ID}}').strip() != record.get('daemon_id'):
        raise ValueError('Harbor recovery Docker daemon differs from the trial')
    # Inspect all objects before the first mutation, including auxiliary objects.
    containers = owned_resources(record, 'container')
    networks = owned_resources(record, 'network')
    volumes = owned_resources(record, 'volume')
    for value in containers:
        if value['Config']['Labels']['com.docker.compose.service'] != 'main' or record.get('credentials_may_exist') is False:
            continue
        identifier = value['Id']
        # Stop every exec descendant; restart only the task's idle service so
        # credentials can be removed from its writable layer before deletion.
        docker('stop', '-t', '5', identifier)
        docker('start', identifier)
        docker('exec', '--user', 'root', identifier, 'sh', '-c',
               'rm -f /ctxpress-private/codex/auth.json; test ! -e /ctxpress-private/codex/auth.json && test ! -L /ctxpress-private/codex/auth.json')
        docker('stop', '-t', '5', identifier)
    for value in containers:
        docker('rm', '-f', '-v', value['Id'])
    for value in networks:
        docker('network', 'rm', value['Id'])
    for value in volumes:
        docker('volume', 'rm', value['Name'])
    record.update(cleaned=True, phase='recovered')
    eval_plan.atomic_json(path, record)
    cleanup_channel(record)


def prepare(task, entry, config, job, folder, label):
    folder = Path(folder).resolve()
    environment = config['environment']
    catalog = harbor_codex.catalog_input(environment, config['model'], config['reasoning'])
    lock, _ = task_resources.read(environment['resources'], task['benchmark'], [job['task']])
    if job.get('resources') != lock['tasks'][task['id']]:
        raise ValueError('Harbor job resources differ from the frozen manifest')
    missing = requirements(config, [job['task']], lock)
    if missing:
        raise ValueError('; '.join(missing))
    official = Path(environment['official_root']).resolve()
    for key, tree in lock['trees'].items():
        actual = eval_environment.workspace(official / key)
        if not eval_environment.same_tree(actual, tree):
            raise ValueError('frozen Harbor official input tree changed: ' + key)
    runtime = lock['runtime']
    if eval_plan.file_sha256(runtime['python']) != runtime['sha256']:
        raise ValueError('pinned Harbor Python changed')
    upstream = environment.get('upstream') or 'https://chatgpt.com/backend-api/codex'
    if environment.get('via'):
        from ctxpress.harness.runtime.connect_proxy import proxy_address
        proxy_address(environment['via'])
    url = urllib.parse.urlsplit(upstream)
    if url.scheme != 'https' or not url.hostname or url.username is not None or url.password is not None or url.fragment:
        raise ValueError('Harbor model upstream must be an HTTPS URL without credentials')
    target = ('[' + url.hostname + ']' if ':' in url.hostname else url.hostname) + ':' + str(url.port or 443)
    binary = Path(environment['bindir']) / 'codex'
    from ctxpress.harness.runtime import codex_binary
    readiness = codex_binary.preflight(binary.parent)
    version = 'codex-cli ' + (readiness['version'] or codex_binary.version(binary))
    match = re.fullmatch(r'codex-cli\s+(\S+)', version)
    if not match:
        raise ValueError('pinned Codex binary returned an invalid version')
    auth = os.environ.get('CTXPRESS_CODEX_AUTH_FILE')
    if not auth or not Path(auth).is_file():
        raise ValueError('set CTXPRESS_CODEX_AUTH_FILE to existing credentials; auth is never frozen in the plan')
    project = 'ctxp-hb-' + uuid.uuid4().hex[:24]
    package = Path(__file__).resolve().parents[3]
    profiles = folder / 'method-inputs'
    request = dict(schema='ctxpress.eval.harbor_trial', version=1, project=project, label=label,
        task=str(task_directory(task)), task_id=task['id'], benchmark=task['benchmark'],
        method=method_inputs(entry, profiles), model=config['model'], reasoning=config['reasoning'],
        run=config['run'], compact_limit=job['compact_limit'], binary_version=match.group(1),
        bindir=str(binary.parent), package=str(package), official=str(official), profiles=str(profiles),
        folder=str(folder), runtime=runtime, images={'main':job['resources']['images']['agent']['id'],
            **{key:value['id'] for key,value in job['resources']['images'].get('services', {}).items()}},
        upstream=upstream, target=target, via=environment.get('via'), gpu_device_ids=job['resources'].get('gpu_device_ids'), **catalog)
    if task['benchmark'] == 'deep-swe':
        request['framework'] = 'pier'
        request['grading_image'] = job['resources']['images']['grading']['verifier']['id']
    elif harbor_protocol.api(lock) == 'modern':
        request['harbor_api'] = 'modern'
        request['separate_verifier'] = 'verifier_environment' in task['initial_state']
        if request['separate_verifier']:
            request['grading_image'] = job['resources']['images']['grading']['verifier']['id']
            request['verifier_gpu_device_ids'] = job['resources'].get('verifier_gpu_device_ids')
            request['verifier_bundled_tests'] = bool((task['evaluation']['verifier'].get('environment') or {}).get('docker_image') or
                (Path(request['task'])/'tests'/'Dockerfile').is_file())
    if task['benchmark']=='swe-bench-pro':
        request.update(pro_version='v2',pro_base_commit=task['initial_state']['base_commit'],
                       grading_image=job['resources']['images']['grading']['verifier']['id'])
    return request, str(Path(auth).resolve())


def available_gpus(ids):
    """Retained containers keep their claim even after their parent lock exits."""
    if not ids:
        return
    selected = {item.casefold() for item in ids}
    for identifier in docker('ps', '-aq', '--filter', 'label=ctxpress.managed=true').split():
        value = json.loads(docker('container', 'inspect', identifier))[0]
        labels = value.get('Config', {}).get('Labels') or {}
        existing = labels.get('ctxpress.gpu.devices', '')
        if selected & {item.casefold() for item in existing.split(',')}:
            raise RuntimeError('GPU devices belong to a retained ctxpress container; recover its owning attempt before running another job')
        for allocation in value.get('HostConfig', {}).get('DeviceRequests') or []:
            capabilities = allocation.get('Capabilities') or []
            if allocation.get('Driver') != 'nvidia' and not any('gpu' in group for group in capabilities):
                continue
            bound = allocation.get('DeviceIDs') or []
            if not bound or any(not harbor_gpu.UUID.fullmatch(item) for item in bound) or selected & {item.casefold() for item in bound}:
                raise RuntimeError('GPU devices overlap a retained ctxpress container allocation; recover its owning attempt first')


def execute(adapter, task, entry, config, job, *, paths, folder, label):
    folder = Path(folder).resolve(); folder.mkdir(parents=True, exist_ok=True)
    request, auth = prepare(task, entry, config, job, folder, label)
    request_path = folder / 'harbor-request.json'
    eval_plan.atomic_json(request_path, request)
    # Source/dependency imports are checked before Docker access or model relay.
    command = [request['runtime']['python'], '-I', '-S', '-B', str(WORKER), str(request_path)]
    child_env = {key:os.environ[key] for key in ('PATH','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CONFIG',
        'XDG_RUNTIME_DIR','SYSTEMROOT','WINDIR','TEMP','TMP','HOME') if key in os.environ}
    child_env['CTXPRESS_CODEX_AUTH_FILE'] = auth
    started = time.monotonic()
    with (folder / 'harbor-worker.log').open('wb') as output:
        subprocess.run(command + ['--check'], env=child_env, stdin=subprocess.DEVNULL,
                       stdout=output, stderr=subprocess.STDOUT, check=True, timeout=60)
        ids = harbor_protocol.claimed_gpus(request)
        daemon_id = docker('info', '--format', '{{.ID}}').strip() if ids else None
        if daemon_id is not None:
            request['gpu_daemon_id'] = daemon_id
            eval_plan.atomic_json(request_path, request)
        def gpu_progress(phase):
            eval_plan.atomic_json(folder/'gpu-reservation.json', dict(schema='ctxpress.eval.gpu_reservation', version=1,
                label=label, phase=phase, device_ids=ids, daemon_id=daemon_id))
        with harbor_gpu.reservation(ids, daemon_id, gpu_progress if daemon_id is not None else None):
            available_gpus(ids)
            process = subprocess.Popen(command, env=child_env, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            try:
                # Harbor applies separate setup, Agent and verifier timeouts;
                # waiting for hardware does not consume the Agent timeout.
                code = process.wait()
            except BaseException:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait()
                raise
            finally:
                for journal in folder.glob('resources-harbor-*.json'):
                    recover(journal, label)
    if code:
        raise RuntimeError('official Harbor worker failed; see the local worker log')
    state = json.loads((folder / 'harbor-worker-result.json').read_text(encoding='utf-8'))
    report = Path(state['official_report']) if state.get('official_report') else folder / request['project'] / 'result.json'
    logs = folder / request['project'] / 'agent' / 'ctxpress-requests.jsonl'
    rows = []
    if logs.is_file():
        for line in logs.read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    telemetry = summary(str(logs))
    grade = adapter.read_grade(task, report) if config['run']['grade'] else None
    result = dict(task=task, benchmark=adapter.describe(), method=entry, model=config['model'], reasoning=config['reasoning'],
        stop=state['stop'], seconds=round(time.monotonic()-started, 1), calls=state['calls'], requests=telemetry['requests'],
        usage=telemetry, rewrites=rows, grade=grade, proxy_log=str(logs), official_report=str(report),
        binary_version=request['binary_version'], gpu=state.get('gpu'), protocol='ctxpress_comparison', real_run_verified=False)
    if catalog := harbor_codex.check_catalog(request):
        result['model_catalog'] = catalog
    for field in ('separate_verifier', 'official_artifacts', 'verifier_gpu', 'network_policy', 'harbor_api',
                  'agent_report','regrade_report','submission','pro_version','fresh_regrade'):
        if field in state:
            result[field] = state[field]
    from ctxpress.harness.runtime import execution_health
    return execution_health.retain(result)
