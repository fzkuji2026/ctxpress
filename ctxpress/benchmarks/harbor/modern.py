"""Frozen modern Harbor lifecycle, with independently owned verifier resources.

Only single-step Linux tasks are accepted. Official Trial.create, collect hooks,
ArtifactHandler and VerifierFactory own task semantics; no installers run.
"""
from __future__ import annotations
import asyncio, contextlib, dataclasses, hashlib, os, shlex, signal, sys
from pathlib import Path
from types import SimpleNamespace


def verifier_definition(module):
    definition = getattr(module, 'resolve_verifier_environment_definition', None)
    if callable(definition):
        return definition
    resolve = getattr(module, 'resolve_effective_verifier_env_config', None)
    if not callable(resolve):
        raise ValueError('frozen Harbor lacks a supported verifier environment resolver')
    # Harbor 0.23 resolves the config and always builds separate verifiers from
    # tests/, then calls VerifierFactory with skip_tests_upload=True.
    def definition(config, paths):
        return SimpleNamespace(bundled_tests=True) if resolve(config, None) is not None else None
    return definition


def mount_value(mount, field):
    return mount.get(field) if isinstance(mount, dict) else getattr(mount, field, None)


@contextlib.contextmanager
def private_network_plans(trial_class):
    original = getattr(trial_class, '_network_plan', None)
    if not callable(original):
        yield
        return
    def resolve(instance, *args, **kwargs):
        plan = original(instance, *args, **kwargs)
        updates = {}
        for field in ('agent_env_baseline', 'agent_phase', 'verifier_env_baseline', 'verifier_phase'):
            policy = getattr(plan, field)
            if policy is None:
                continue
            mode = getattr(policy.network_mode, 'value', policy.network_mode)
            if mode not in ('public', 'no-network') or policy.allowed_hosts:
                raise ValueError('ctxpress private services cannot implement Harbor task allowlists')
            updates[field] = policy.model_copy(update={
                'network_mode':type(policy.network_mode)('no-network'), 'allowed_hosts':[]}, deep=True)
        # Project the author's phase plan onto the already declared ctxpress
        # private-network protocol. No egress sidecar or policy switch is needed.
        return dataclasses.replace(plan, **updates)
    trial_class._network_plan = resolve
    try:
        yield
    finally:
        trial_class._network_plan = original


def load(official):
    from harbor.agents.installed.codex import Codex
    from harbor.agents.installed.base import NonZeroAgentExitCodeError
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.environments.base import ExecResult
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial
    from harbor.trial.single_step import SingleStepTrial
    from harbor.models.task import verifier_mode
    from harbor.environments.capabilities import EnvironmentCapabilities
    required = ((Trial, 'create'), (Trial, '_run_collect_hooks'), (Trial, '_run_separate_verifier'),
                (SingleStepTrial, '_run'), (Codex, '_build_effective_config'), (Codex, 'render_instruction'),
                (DockerEnvironment, '_write_env_compose_file'), (DockerEnvironment, '_upload_environment_dir_after_start'))
    if any(not callable(getattr(cls, name, None)) for cls, name in required):
        raise ValueError('frozen Harbor lacks the supported SingleStepTrial entrypoints')
    return Codex, NonZeroAgentExitCodeError, DockerEnvironment, ExecResult, TrialConfig, Trial, verifier_definition(verifier_mode), EnvironmentCapabilities


def trial_config(request, config_class, channel):
    from ctxpress.harness.runtime.codex_agent import catalog_mounts
    mounts = [dict(type='bind', source=source, target=target, read_only=True, bind={'create_host_path':False})
        for source, target in ((request['package'], '/ctxpress-runtime'), (request['bindir'], '/cxbin'),
                              (request['profiles'], '/ctxpress-method'), (str(channel), '/ctxpress-channel'))]
    mounts.extend(catalog_mounts(request))
    if request.get('pro_replay'):mounts=[]
    return config_class(task={'path':request['task']}, trial_name=request['project'], trials_dir=request['folder'],
        timeout_multiplier=1.0, agent={'import_path':'ctxpress.benchmarks.harbor.modern:CtxpressCodex',
            'model_name':request['model'], 'override_timeout_sec':request['run']['timeout']},
        environment={'import_path':'ctxpress.benchmarks.harbor.modern:CtxpressDocker',
                     'force_build':False, 'delete':True, 'mounts':mounts},
        verifier={'disable':not request['run']['grade']})


async def run_trial(request, official, channel, journal,agent_factory=None):
    from ctxpress.harness.runtime import codex_agent
    from ctxpress.benchmarks.harbor import environment as harbor_environment, modern_codex as harbor_modern_codex
    from ctxpress.harness.jobs import plan as eval_plan
    from ctxpress.benchmarks.harbor.worker import resource_record
    Codex, LimitError, Docker, ExecResult, Config, Trial, definition, Caps = official
    project = request['project']
    separate = bool(request.get('separate_verifier'))
    grader_project = 'ctxp-hb-' + hashlib.sha256((project + ':verifier').encode()).hexdigest()[:24]
    trial_root = Path(request['folder']) / project
    records = {'agent':resource_record(request, channel, journal['daemon_id'])}
    records['agent']['role'] = 'verifier' if request.get('pro_replay') else 'agent'
    if separate:
        records['verifier'] = dict(records['agent'], project=grader_project, images={'main':request['grading_image']},
            channel=None, role='verifier', gpu_device_ids=request.get('verifier_gpu_device_ids'))
        records['verifier'].pop('model_catalog', None)
    environments = {}
    def persist(role):
        record = records[role]
        eval_plan.atomic_json(Path(request['folder']) / ('resources-harbor-' + record['project'] + '.json'), record)
    for role in records:
        persist(role)
    trial = None
    def credentials(may_exist):
        records['agent']['credentials_may_exist'] = may_exist
        persist('agent')
    async def cleanup(environment):
        if (environment.ctxpress_role == 'agent' and trial is not None and
                not trial.agent.ctxpress_credentials_cleaned):
            await trial.agent.cleanup_credentials(environment)
    def update(phase, environment):
        role = environment.ctxpress_role
        record = records[role]
        record.update(phase=phase, cleaned=phase == 'stopped')
        if environment._ctxpress_guard_path:
            record['compose_sha256'] = eval_plan.file_sha256(environment._ctxpress_guard_path)
        if environment.ctxpress_gpu_evidence is not None:
            record['gpu'] = environment.ctxpress_gpu_evidence
        persist(role)

    global CtxpressCodex, CtxpressDocker
    settings=dict(method=request['method'], profiles=request.get('profiles'), model=request['model'],
        reasoning=request['reasoning'], binary_version=request['binary_version'], compact_limit=request['compact_limit'],
        upstream=request['upstream'], auth_file=os.environ['CTXPRESS_CODEX_AUTH_FILE'],
        max_calls=request['run']['max_calls'], **codex_agent.catalog_settings(request))
    CtxpressCodex = agent_factory(Codex,None,settings,LimitError,credentials) if agent_factory else \
        harbor_modern_codex.framework(Codex,settings,LimitError,credentials)

    class CompatibleDocker(Docker):
        @staticmethod
        def _requires_egress_control(**kwargs):
            # ctxpress applies private Compose networks before up. Do not let
            # Harbor build/probe an undeclared egress sidecar or probe image.
            return False
        @property
        def capabilities(self):
            return Caps(gpus=True, disable_internet=True, mounted=True, docker_compose=True)

    class CtxpressDocker:
        @classmethod
        def resource_capabilities(cls):
            return CompatibleDocker.resource_capabilities()
        @classmethod
        def preflight(cls):
            return CompatibleDocker.preflight()
        def __new__(cls, *args, **kwargs):
            if args:
                raise ValueError('modern Harbor environment requires keyword inputs')
            incoming = kwargs['session_id']
            if incoming == project + '__env':
                role = 'agent'
            elif separate and incoming == project + '__verifier__trial':
                role = 'verifier'
            else:
                raise ValueError('unexpected modern Harbor phase identity')
            if role == 'verifier' and (not records['agent']['cleaned'] or records['agent']['credentials_may_exist']):
                raise RuntimeError('verifier requires checked Agent shutdown and credential deletion')
            if role in environments and environments[role]._ctxpress_started:
                raise RuntimeError('previous phase environment remains running')
            images = records[role]['images']
            kwargs['session_id'] = records[role]['project']
            kwargs['task_env_config'] = kwargs['task_env_config'].model_copy(update={'docker_image':images['main']}, deep=True)
            roots = {str(Path(request['task'])/'environment'):True}
            if role == 'agent':
                if separate:
                    # Official Trial supplies a shared-verifier log mount even
                    # in separate mode. It is not an Agent input in this protocol.
                    kwargs['mounts'] = [mount for mount in kwargs.get('mounts', [])
                        if not (mount_value(mount, 'target') == '/logs/verifier' and
                            Path(mount_value(mount, 'source') or '').resolve() == (trial_root/'verifier').resolve())]
                roots.update({request['package']:True, request['bindir']:True, request['profiles']:True,
                    str(channel):True, str(trial_root/'agent'):False, str(trial_root/'artifacts'):False})
                if request.get('pro_replay'):
                    roots={str(Path(request['task'])/'environment'):True,
                        str(trial_root/'agent'):False,str(trial_root/'artifacts'):False}
                if not separate:
                    roots[str(trial_root/'verifier')] = False
                for mount in codex_agent.catalog_mounts(request):
                    roots[mount['source']] = True
            else:
                roots.update({str(Path(request['task'])/'tests'):True, str(trial_root/'verifier'):False})
                mounts = kwargs.get('mounts')
                if (not isinstance(mounts, list) or len(mounts) != 1 or mount_value(mounts[0], 'target') != '/logs/verifier' or
                        Path(mount_value(mounts[0], 'source') or '').resolve() != (trial_root/'verifier').resolve()):
                    raise ValueError('independent verifier received unexpected runtime mounts')
            settings = dict(images=images, bind_roots=roots, run_label=request['label'],
                            compose_name='ctxpress-compose-' + role + '.json')
            ids = records[role].get('gpu_device_ids')
            if ids is not None:
                settings['gpu_device_ids'] = ids
            guarded = harbor_environment.framework(CompatibleDocker, ExecResult, settings, cleanup, journal=update)
            class Environment(guarded):
                ctxpress_role = role
                _ctxpress_main_stopped = False
                async def stop_service(self, service):
                    if service == 'main':
                        await cleanup(self)
                        await self._chown_to_host_user('/logs', recursive=True)
                    await super().stop_service(service)
                    if service == 'main':
                        self._ctxpress_main_stopped = True
                async def _chown_to_host_user(self, *args, **kwargs):
                    if not self._ctxpress_main_stopped:
                        return await super()._chown_to_host_user(*args, **kwargs)
                async def start(self, force_build=False):
                    self._validate_daemon_mode()
                    await self._validate_image_os(images['main'])
                    self._resources_compose_path = self._write_resources_compose_file()
                    self._env_compose_path = self._write_env_compose_file()
                    await super().start(force_build=force_build)
                    await self.ensure_dirs(self._mount_targets(writable_only=True))
                    await self._upload_environment_dir_after_start()
                    if request.get('pro_base_commit'):
                        from ctxpress.benchmarks.pro.trial import check_repository
                        await check_repository(self,request['pro_base_commit'])
                        records[role]['pro_base_commit']=request['pro_base_commit'];persist(role)
                    if role == 'verifier' and request.get('verifier_bundled_tests'):
                        for path in sorted((Path(request['task'])/'tests').rglob('*')):
                            if not path.is_file() or path.name in ('Dockerfile', 'docker-compose.yaml'):
                                continue
                            relative = path.relative_to(Path(request['task'])/'tests').as_posix()
                            result = await self.exec(command='sha256sum -- ' + shlex.quote('/tests/' + relative),
                                                     timeout_sec=30, user='root')
                            if result.return_code or (result.stdout or '').split()[:1] != [eval_plan.file_sha256(path)]:
                                raise ValueError('prepared verifier differs from frozen author tests')
                async def stop(self, delete=True):
                    await super().stop(delete=delete)
                    self._cleanup_mounts_compose_file()
                    self._cleanup_resources_compose_file()
                    self._cleanup_env_compose_file()
            environment = Environment(**kwargs)
            environments[role] = environment
            return environment

    sys.modules['ctxpress.benchmarks.harbor.modern'] = sys.modules[__name__]
    # Unlike Pier, modern Trial.create selects its own concrete subclass.
    with private_network_plans(Trial):
        trial = await Trial.create(trial_config(request, Config, channel))
        actual = definition(trial.task.config, trial.task.paths)
        if bool(actual is not None) != separate or actual is not None and actual.bundled_tests != request['verifier_bundled_tests']:
            raise ValueError('official verifier definition differs from captured phase contract')
        execution = asyncio.create_task(trial.run())
        loop = asyncio.get_running_loop()
        for action in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(action, execution.cancel)
        try:
            result = await execution
        finally:
            for action in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(action)
    if any(environment._ctxpress_started for environment in environments.values()) or not records['agent']['cleaned']:
        raise RuntimeError('Harbor returned without checked environment cleanup')
    if separate and request['run']['grade'] and 'verifier' in environments and not records['verifier']['cleaned']:
        raise RuntimeError('Harbor returned without checked verifier cleanup')
    progress = codex_agent.CallProgress(trial_root/'agent'/'sessions')
    calls, _ = progress.update()
    exception = result.exception_info.exception_type if result.exception_info else None
    stop = trial.agent.ctxpress_stop or ('timeout' if exception == 'AgentTimeoutError' else
        'agent_error' if exception == 'NonZeroAgentExitCodeError' else 'trial_error' if exception else 'completed')
    state = dict(stop=stop, calls=calls, gpu=environments['agent'].ctxpress_gpu_evidence,
        network_policy='ctxpress_private_services_model_socket_only', harbor_api='single_step',
        official_artifacts={path.relative_to(trial_root).as_posix():dict(path=str(path.resolve()), sha256=eval_plan.file_sha256(path))
            for folder in ('artifacts', 'verifier') for path in sorted((trial_root/folder).rglob('*'))
            if path.is_file() and not path.is_symlink()})
    if separate:
        state['verifier_gpu'] = environments['verifier'].ctxpress_gpu_evidence if 'verifier' in environments else None
        state['separate_verifier'] = dict(agent_project=project, verifier_project=grader_project,
            agent_image=request['images']['main'], verifier_image=request['grading_image'],
            author_collect=True, author_artifact_handler=True, bundled_tests=actual.bundled_tests,
            verifier_started='verifier' in environments, checked_cleanup=True)
    eval_plan.atomic_json(Path(request['folder'])/'harbor-worker-result.json', state)
