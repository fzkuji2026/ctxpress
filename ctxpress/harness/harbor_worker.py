"""Isolated entrypoint for a frozen official Harbor Trial; no installers or pulls."""
from __future__ import annotations
import argparse, asyncio, importlib.metadata, json, os, signal, sys, tempfile, threading
from pathlib import Path


def load(request):
    package = Path(__file__).resolve().parents[2]
    if str(package) != request['package']:
        raise ValueError('Harbor worker is not from the declared ctxpress runtime')
    runtime = request['runtime']
    if (Path(sys.executable).resolve() != Path(runtime['python']).resolve() or
            sys.version != runtime['version'] or sys.platform != runtime['platform']):
        raise ValueError('Harbor Python version/platform differs from the captured runtime')
    if not sys.platform.startswith('linux'):
        raise ValueError('Harbor socket execution requires a Linux host')
    official = Path(request['official'])
    framework = request.get('framework', 'harbor')
    if framework not in ('harbor', 'pier'):
        raise ValueError('unknown official task framework')
    sys.path[:0] = [str(package), str(official / framework / 'src'), str(official / 'dependencies')]
    from ctxpress.core import toml
    from ctxpress.harness import eval_plan
    if eval_plan.file_sha256(sys.executable) != runtime['sha256']:
        raise ValueError('Harbor Python binary changed')
    from ctxpress.benchmarks.harbor_codex import check_catalog
    check_catalog(request, probe=True)
    if framework == 'pier':
        from ctxpress.harness.pier_trial import load as load_pier
        return load_pier(official)
    import harbor
    expected = toml.load(official / 'harbor' / 'pyproject.toml')['project']['version']
    if (Path(harbor.__file__).resolve() != official / 'harbor' / 'src' / 'harbor' / '__init__.py' or
            importlib.metadata.version('harbor') != expected):
        raise ValueError('Harbor source and package metadata do not match')
    if request.get('harbor_api') == 'modern':
        from ctxpress.harness.harbor_modern import load as load_modern
        result=load_modern(official)
        if request.get('benchmark')=='swe-bench-pro':
            from ctxpress.harness.pro_trial import load as load_pro
            return load_pro(request,result)
        return result
    from harbor.agents.installed.codex import Codex
    from harbor.agents.installed.base import ExecInput, NonZeroAgentExitCodeError
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.environments.base import ExecResult
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial
    result=Codex, ExecInput, NonZeroAgentExitCodeError, DockerEnvironment, ExecResult, TrialConfig, Trial
    if request.get('benchmark')=='swe-bench-pro':
        from ctxpress.harness.pro_trial import load as load_pro
        return load_pro(request,result)
    return result


def trial_config(request, config_class, channel):
    from ctxpress.benchmarks.harbor_codex import catalog_mounts
    mounts = []
    for source, target in ((request['package'], '/ctxpress-runtime'), (request['bindir'], '/cxbin'),
                           (request['profiles'], '/ctxpress-method'), (str(channel), '/ctxpress-channel')):
        mounts.append(dict(type='bind', source=source, target=target, read_only=True, bind={'create_host_path':False}))
    mounts.extend(catalog_mounts(request))
    if request.get('pro_replay'):mounts=[]
    return config_class(task={'path':request['task']}, trial_name=request['project'], trials_dir=request['folder'],
        timeout_multiplier=1.0, agent={'import_path':'ctxpress.harness.harbor_worker:CtxpressCodex',
            'model_name':request['model'], 'override_timeout_sec':request['run']['timeout']},
        environment={'import_path':'ctxpress.harness.harbor_worker:CtxpressDocker',
                     'force_build':False, 'delete':True, 'mounts_json':mounts},
        verifier={'disable':not request['run']['grade']})


def resource_record(request, channel, daemon_id):
    from ctxpress.core import processes
    from ctxpress.benchmarks.harbor_codex import check_catalog
    catalog = check_catalog(request)
    return dict(schema='ctxpress.eval.harbor_resources', version=1, project=request['project'],
        label=request['label'], images=request['images'], daemon_id=daemon_id,
        channel=str(channel) if channel is not None else None, cleaned=False, phase='prepared', credentials_may_exist=False,
        **({'role':'verifier'} if request.get('pro_replay') else {}),
        **({'model_catalog':catalog} if catalog and not request.get('pro_replay') else {}),
        gpu_device_ids=request.get('gpu_device_ids'), pid=os.getpid(), identity=processes.identity(os.getpid()))


async def run_trial(request, official, channel, journal,agent_factory=None):
    from ctxpress.benchmarks import harbor_codex, harbor_environment
    from ctxpress.harness import eval_plan
    Codex, ExecInput, LimitError, Docker, ExecResult, Config, Trial = official
    owner = resource_record(request, channel, journal['daemon_id'])
    path = Path(request['folder']) / ('resources-harbor-' + request['project'] + '.json')
    eval_plan.atomic_json(path, owner)
    trial = None
    async def cleanup(environment):
        if trial is not None:
            await trial._agent.cleanup_credentials(environment)
    def update(phase, environment):
        owner.update(phase=phase, cleaned=phase == 'stopped')
        if environment._ctxpress_guard_path:
            owner['compose_sha256'] = eval_plan.file_sha256(environment._ctxpress_guard_path)
        if environment.ctxpress_gpu_evidence is not None:
            owner['gpu'] = environment.ctxpress_gpu_evidence
        eval_plan.atomic_json(path, owner)
    def credentials(may_exist):
        owner['credentials_may_exist'] = may_exist
        eval_plan.atomic_json(path, owner)
    trial_root = Path(request['folder']) / request['project']
    binds = {request['package']:True, request['bindir']:True, request['profiles']:True,
             str(Path(request['task'])/'environment'):True,
             str(channel):True, str(trial_root/'agent'):False, str(trial_root/'verifier'):False,
             str(trial_root/'artifacts'):False}
    if request.get('pro_replay'):
        binds={str(Path(request['task'])/'environment'):True,
               **{str(trial_root/name):False for name in ('agent','verifier','artifacts')}}
    for mount in harbor_codex.catalog_mounts(request):
        binds[mount['source']] = True
    global CtxpressCodex, CtxpressDocker
    settings=dict(method=request['method'], profiles=request.get('profiles'), model=request['model'],
        reasoning=request['reasoning'], binary_version=request['binary_version'], compact_limit=request['compact_limit'],
        upstream=request['upstream'], auth_file=os.environ['CTXPRESS_CODEX_AUTH_FILE'],
        max_calls=request['run']['max_calls'], **harbor_codex.catalog_settings(request))
    CtxpressCodex = agent_factory(Codex,ExecInput,settings,LimitError,credentials) if agent_factory else \
        harbor_codex.framework(Codex,ExecInput,settings,limit_error=LimitError,credential_state=credentials)
    environment_settings = dict(images=request['images'], bind_roots=binds, run_label=request['label'])
    if request.get('gpu_device_ids'):
        environment_settings['gpu_device_ids'] = request['gpu_device_ids']
    guarded = harbor_environment.framework(Docker, ExecResult,
        environment_settings, cleanup, journal=update)
    class CtxpressDocker(guarded):
        async def start(self,force_build=False):
            await super().start(force_build=force_build)
            if request.get('pro_base_commit'):
                from ctxpress.harness.pro_trial import check_repository
                await check_repository(self,request['pro_base_commit'])
                owner['pro_base_commit']=request['pro_base_commit'];eval_plan.atomic_json(path,owner)
    # The official factories import these custom classes by module path.
    sys.modules['ctxpress.harness.harbor_worker'] = sys.modules[__name__]
    trial = Trial(trial_config(request, Config, channel))
    execution = asyncio.create_task(trial.run())
    loop = asyncio.get_running_loop()
    for action in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(action, execution.cancel)
    try:
        result = await execution
    finally:
        for action in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(action)
    if trial._environment._ctxpress_started or not owner['cleaned']:
        raise RuntimeError('Harbor returned without verified environment cleanup')
    progress = harbor_codex.CallProgress(trial_root / 'agent' / 'sessions')
    calls, _ = progress.update()
    exception = result.exception_info.exception_type if result.exception_info else None
    stop = trial._agent.ctxpress_stop or ('timeout' if exception == 'AgentTimeoutError' else
        'agent_error' if exception == 'NonZeroAgentExitCodeError' else 'trial_error' if exception else 'completed')
    state = dict(stop=stop, calls=calls)
    if trial._environment.ctxpress_gpu_evidence is not None:
        state['gpu'] = trial._environment.ctxpress_gpu_evidence
    eval_plan.atomic_json(Path(request['folder'])/'harbor-worker-result.json', state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request'); parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    request = json.loads(Path(args.request).read_text(encoding='utf-8'))
    if request.get('schema') != 'ctxpress.eval.harbor_trial' or request.get('version') != 1:
        raise ValueError('invalid frozen Harbor trial request')
    official = load(request)
    if args.check:
        distribution = 'datacurve-pier' if request.get('framework') == 'pier' else 'harbor'
        print(json.dumps(dict(imports='verified', framework=distribution, version=importlib.metadata.version(distribution), model_calls=0)))
        return
    from ctxpress.harness import connect_proxy, eval_plan, socket_bridge
    from ctxpress.benchmarks.harbor_driver import docker, cleanup_channel
    daemon_id = docker('info', '--format', '{{.ID}}').strip()
    if request.get('gpu_daemon_id') and request['gpu_daemon_id'] != daemon_id:
        raise ValueError('Docker daemon changed after GPU reservation')
    channel = Path(tempfile.mkdtemp(prefix=request['project']+'-channel-'))
    channel.chmod(0o755)
    eval_plan.atomic_json(channel/'owner.json', dict(project=request['project'], label=request['label']))
    resource_path = Path(request['folder']) / ('resources-harbor-' + request['project'] + '.json')
    eval_plan.atomic_json(resource_path, resource_record(request, channel, daemon_id))
    server, thread = None, None
    try:
        template = connect_proxy.make_server('127.0.0.1', 0, [request['target']], via=request['via'])
        handler = template.RequestHandlerClass; template.server_close()
        server = socket_bridge.unix_server(channel/'model.sock', handler)
        (channel/'model.sock').chmod(0o666)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        runner = run_trial
        if request.get('benchmark')=='swe-bench-pro':
            from ctxpress.harness.pro_trial import run_trial as runner
        elif request.get('framework') == 'pier':
            from ctxpress.harness.pier_trial import run_trial as runner
        elif request.get('harbor_api') == 'modern':
            from ctxpress.harness.harbor_modern import run_trial as runner
        asyncio.run(runner(request, official, channel, dict(daemon_id=daemon_id)))
    finally:
        if server:
            if thread and thread.is_alive():
                server.shutdown(); thread.join(timeout=5)
            server.server_close()
        # A retained container still references this bind source. Keep its
        # directory until recovery removes credentials and the container; a
        # missing source can otherwise prevent Docker start/exec for cleanup.
        if json.loads(resource_path.read_text(encoding='utf-8')).get('cleaned'):
            cleanup_channel(dict(channel=str(channel), project=request['project'], label=request['label']))


if __name__ == '__main__':
    main()
