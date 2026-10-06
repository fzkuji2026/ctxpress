"""Official Pier Trial hooks for DeepSWE's independent verifier containers."""
from __future__ import annotations
import asyncio, hashlib, importlib.metadata, os, re, shlex, shutil, signal, sys
from pathlib import Path


def load(official):
    from ctxpress.core import toml
    import pier
    expected = toml.load(official / 'pier' / 'pyproject.toml')['project']['version']
    version = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', expected)
    if (Path(pier.__file__).resolve() != official / 'pier' / 'src' / 'pier' / '__init__.py' or
            importlib.metadata.version('datacurve-pier') != expected or not version or
            tuple(map(int, version.groups())) <= (0, 3, 0)):
        raise ValueError('DeepSWE requires matching official Pier source/metadata newer than 0.3.0')
    from pier.agents.installed.codex import Codex
    from pier.agents.installed.base import NonZeroAgentExitCodeError
    from pier.models.agent.install import AgentInstallSpec
    from pier.environments.docker.docker import DockerEnvironment
    from pier.environments.base import ExecResult
    from pier.models.trial.config import TrialConfig
    from pier.trial.trial import Trial
    from pier.models.task.verifier_mode import resolve_task_verifier_mode
    # Eagerly require the author entrypoints before creating a relay/container.
    if not callable(getattr(Trial, 'create', None)) or not callable(getattr(Trial, '_run_collect_hooks', None)):
        raise ValueError('Pier runtime lacks official collect/separate-verifier interfaces')
    return Codex, AgentInstallSpec, NonZeroAgentExitCodeError, DockerEnvironment, ExecResult, TrialConfig, Trial, resolve_task_verifier_mode


def trial_config(request, config_class, channel):
    from ctxpress.benchmarks.harbor.codex_hook import catalog_mounts
    root = Path(request['folder']) / request['project']
    mounts = [dict(type='bind', source=source, target=target, read_only=True, bind={'create_host_path':False})
        for source, target in ((request['package'], '/ctxpress-runtime'), (request['bindir'], '/cxbin'),
                              (request['profiles'], '/ctxpress-method'), (str(channel), '/ctxpress-channel'))]
    mounts.extend(catalog_mounts(request))
    # Agent receives its own logs/artifacts, never the verifier logs or tests.
    mounts += [dict(type='bind', source=str(root / name), target='/logs/' + name,
                    bind={'create_host_path':False}) for name in ('agent', 'artifacts')]
    return config_class(task={'path':request['task']}, trial_name=request['project'], trials_dir=request['folder'],
        timeout_multiplier=1.0, agent={'import_path':'ctxpress.benchmarks.deepswe.pier_trial:CtxpressCodex',
            'model_name':request['model'], 'override_timeout_sec':request['run']['timeout']},
        environment={'import_path':'ctxpress.benchmarks.deepswe.pier_trial:CtxpressDocker',
                     'force_build':False, 'delete':True, 'mounts':mounts},
        verifier={'disable':not request['run']['grade']})


def submission(root, target):
    """Retain original author artifacts; transfer only the declared patch."""
    root, target = Path(root), Path(target)
    target.mkdir()
    patch = root / 'model.patch'
    if patch.is_symlink():
        raise ValueError('DeepSWE submission cannot be a symbolic link')
    if patch.exists():
        if not patch.is_file():
            raise ValueError('DeepSWE submission must be a regular patch file')
        from ctxpress.harness.jobs import plan as eval_plan
        digest = eval_plan.file_sha256(patch)
        shutil.copyfile(patch, target / patch.name)
        if eval_plan.file_sha256(target / patch.name) != digest:
            raise ValueError('DeepSWE submission changed during transfer')
    # A missing/empty patch is scored by the author as the pristine base state.
    return target


async def run_trial(request, official, channel, journal):
    from ctxpress.benchmarks.harbor import codex_hook as harbor_codex, environment as harbor_environment
    from ctxpress.benchmarks.deepswe import pier_codex
    from ctxpress.harness.jobs import plan as eval_plan
    from ctxpress.core import processes
    from ctxpress.benchmarks.harbor.worker import resource_record
    Codex, Install, LimitError, Docker, ExecResult, Config, Trial, mode = official
    project = request['project']
    grader_project = 'ctxp-hb-' + hashlib.sha256((project + ':verifier').encode()).hexdigest()[:24]
    trial_root = Path(request['folder']) / project
    records, environments = {}, {}
    agent_record = resource_record(request, channel, journal['daemon_id'])
    agent_record['role'] = 'agent'
    records['agent'] = agent_record
    records['verifier'] = dict(schema=agent_record['schema'], version=1, project=grader_project,
        label=request['label'], images={'main':request['grading_image']}, daemon_id=journal['daemon_id'],
        channel=None, role='verifier', cleaned=False, phase='prepared', credentials_may_exist=False,
        pid=os.getpid(), identity=processes.identity(os.getpid()))
    def persist(role):
        record = records[role]
        eval_plan.atomic_json(Path(request['folder']) / ('resources-harbor-' + record['project'] + '.json'), record)
    for role in records:
        persist(role)
    trial = None
    def credentials(may_exist):
        agent_record['credentials_may_exist'] = may_exist
        persist('agent')
    async def cleanup(environment):
        if environment.ctxpress_role == 'agent' and trial is not None:
            await trial._agent.cleanup_credentials(environment)
    def update(phase, environment):
        role = environment.ctxpress_role
        record = records[role]
        record.update(phase=phase, cleaned=phase == 'stopped')
        if environment._ctxpress_guard_path:
            record['compose_sha256'] = eval_plan.file_sha256(environment._ctxpress_guard_path)
        persist(role)

    global CtxpressCodex, CtxpressDocker
    CtxpressCodex = pier_codex.framework(Codex, Install, dict(method=request['method'], profiles=request.get('profiles'), model=request['model'],
        reasoning=request['reasoning'], binary_version=request['binary_version'], compact_limit=request['compact_limit'],
        upstream=request['upstream'], auth_file=os.environ['CTXPRESS_CODEX_AUTH_FILE'],
        max_calls=request['run']['max_calls'], **harbor_codex.catalog_settings(request)), LimitError, credentials)

    class CtxpressDocker:
        """Factory selects an owned environment for the official phase."""
        def __new__(cls, *args, **kwargs):
            if args:
                raise ValueError('Pier environment factory requires explicit keyword inputs')
            incoming = kwargs['session_id']
            if incoming == project:
                role = 'agent'
            elif incoming == project + '__verifier__trial':
                role = 'verifier'
            else:
                raise ValueError('unexpected Pier verifier session identity')
            if role == 'verifier' and (not agent_record['cleaned'] or agent_record['credentials_may_exist']):
                raise RuntimeError('DeepSWE verifier cannot start before checked Agent shutdown and credential deletion')
            # A retry may reuse the same official verifier identity only after
            # the previous environment was fully stopped.
            if role in environments and environments[role]._ctxpress_started:
                raise RuntimeError('Pier environment from a previous phase is still running')
            kwargs['session_id'] = records[role]['project']
            kwargs['agent_install_spec'] = None
            kwargs['network_allowlist'] = None
            roots = {str(Path(request['task']) / ('environment' if role == 'agent' else 'tests')):True}
            if role == 'agent':
                roots.update({request['package']:True, request['bindir']:True, request['profiles']:True,
                              str(channel):True, str(trial_root/'agent'):False, str(trial_root/'artifacts'):False})
                for mount in harbor_codex.catalog_mounts(request):
                    roots[mount['source']] = True
                images = request['images']
            else:
                roots[str(trial_root/'verifier')] = False
                images = {'main':request['grading_image']}
                # Pier supplies only verifier logs for its separate environment.
                mounts = kwargs.get('mounts_json')
                if (not isinstance(mounts, list) or len(mounts) != 1 or
                        mounts[0].get('target') != '/logs/verifier' or
                        Path(mounts[0].get('source', '')).resolve() != (trial_root/'verifier').resolve()):
                    raise ValueError('DeepSWE verifier received unexpected runtime mounts')
            settings = dict(images=images, bind_roots=roots, run_label=request['label'],
                            compose_name='ctxpress-compose-' + role + '.json')
            guarded = harbor_environment.framework(Docker, ExecResult, settings, cleanup, journal=update)
            class Environment(guarded):
                ctxpress_role = role
                async def start(self, force_build=False):
                    # Pier's CPU/memory resource overrides are generated before
                    # the common guard; bypassing its installer must not lose them.
                    self._resources_compose_path = self._write_resources_compose_file()
                    await super().start(force_build=force_build)
                    if role == 'verifier':
                        for path in sorted((Path(request['task'])/'tests').rglob('*')):
                            if not path.is_file() or path.name == 'Dockerfile':
                                continue
                            relative = path.relative_to(Path(request['task'])/'tests').as_posix()
                            result = await self.exec(command='sha256sum -- ' + shlex.quote('/tests/' + relative),
                                                     timeout_sec=30, user='root')
                            if (result.return_code or (result.stdout or '').split()[:1] != [eval_plan.file_sha256(path)]):
                                raise ValueError('prepared DeepSWE verifier image differs from frozen author tests')
                def _write_mounts_compose_file(self):
                    source = super()._write_mounts_compose_file()
                    target = source.with_name('ctxpress-mounts-' + role + '.json')
                    shutil.copyfile(source, target)
                    return target
                async def stop(self, delete=True):
                    await super().stop(delete=delete)
                    self._cleanup_resources_compose_file()
            environment = Environment(**kwargs)
            environments[role] = environment
            return environment

    class OfficialTrial(Trial):
        async def _verify_with_separate_environment(self, env_config, **kwargs):
            if not agent_record['cleaned'] or agent_record['credentials_may_exist']:
                raise RuntimeError('DeepSWE Agent cleanup was not verified')
            # Official artifact handling also transfers the conventional drop
            # directory. Give it a staging directory containing only model.patch.
            target = trial_root / ('submission-' + str(len(list(trial_root.glob('submission-*')))))
            kwargs['artifacts_dir'] = submission(kwargs['artifacts_dir'], target)
            return await super()._verify_with_separate_environment(env_config, **kwargs)

    sys.modules['ctxpress.benchmarks.deepswe.pier_trial'] = sys.modules[__name__]
    trial = await OfficialTrial.create(trial_config(request, Config, channel))
    if str(mode(trial._task.config).value) != 'separate':
        raise ValueError('official Pier resolved DeepSWE to a shared verifier')
    execution = asyncio.create_task(trial.run())
    loop = asyncio.get_running_loop()
    for action in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(action, execution.cancel)
    try:
        result = await execution
    finally:
        for action in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(action)
    if any(environment._ctxpress_started for environment in environments.values()):
        raise RuntimeError('Pier returned without checked environment cleanup')
    if not agent_record['cleaned'] or (request['run']['grade'] and not records['verifier']['cleaned']):
        raise RuntimeError('Pier returned without checked Agent/verifier cleanup')
    progress = harbor_codex.CallProgress(trial_root/'agent'/'sessions')
    calls, _ = progress.update()
    exception = result.exception_info.exception_type if result.exception_info else None
    stop = trial._agent.ctxpress_stop or ('timeout' if exception == 'AgentTimeoutError' else
        'agent_error' if exception == 'NonZeroAgentExitCodeError' else 'trial_error' if exception else 'completed')
    artifacts = {path.relative_to(trial_root).as_posix():dict(path=str(path.resolve()), sha256=eval_plan.file_sha256(path))
        for folder in ('artifacts', 'verifier') for path in sorted((trial_root/folder).rglob('*'))
        if path.is_file() and not path.is_symlink()}
    eval_plan.atomic_json(Path(request['folder'])/'harbor-worker-result.json', dict(stop=stop, calls=calls,
        separate_verifier=dict(agent_project=project, verifier_project=grader_project,
            agent_image=request['images']['main'], verifier_image=request['grading_image'],
            author_collect=True, patch_only_transfer=True, checked_cleanup=True), official_artifacts=artifacts))
