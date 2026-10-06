"""Modern official API contracts; fixtures never call Docker, a model or an installer."""
import asyncio, copy, dataclasses, json, shlex, sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from ctxpress import benchmarks
from ctxpress.benchmarks import harbor_driver, harbor_protocol
from ctxpress.benchmarks.harbor_environment import compose_output
from ctxpress.harness import eval_environment, eval_plan, task_resources
from ctxpress.harness import harbor_modern
from test_harbor_driver import prepared, PROJECT, IMAGE
from test_harbor_gpu import A, QUERY
from test_harbor_codex import OfficialFixture, EnvironmentFixture

GRADER = 'sha256:' + 'c'*64


def test_current_harbor_config_resolver_preserves_shared_and_bundled_separate_modes():
    seen=[]
    def resolve(config, step):
        seen.append((config,step))
        return config.get('verifier_environment')
    definition=harbor_modern.verifier_definition(SimpleNamespace(resolve_effective_verifier_env_config=resolve))
    assert definition({},None) is None
    assert definition({'verifier_environment':{}},None).bundled_tests is True
    assert seen==[({},None),({'verifier_environment':{}},None)]
    with pytest.raises(ValueError,match='resolver'):
        harbor_modern.verifier_definition(SimpleNamespace())


def test_phase_mount_checks_accept_typed_official_service_volumes():
    mount=SimpleNamespace(source='/trial/verifier',target='/logs/verifier')
    assert harbor_modern.mount_value(mount,'source')=='/trial/verifier'
    assert harbor_modern.mount_value(mount,'target')=='/logs/verifier'


def test_official_phase_plans_use_the_declared_private_network_without_mutating_author_policy():
    @dataclasses.dataclass(frozen=True)
    class Plan:
        agent_env_baseline: object
        agent_phase: object
        verifier_env_baseline: object
        verifier_phase: object
    public=EnvConfig(network_mode='public',allowed_hosts=[])
    offline=EnvConfig(network_mode='no-network',allowed_hosts=[])
    original=Plan(public,offline,None,public)
    class Trial:
        def _network_plan(self, step=None):return original
    resolver=Trial._network_plan
    with harbor_modern.private_network_plans(Trial):
        projected=Trial()._network_plan()
        assert projected.agent_env_baseline.network_mode=='no-network'
        assert projected.agent_phase.network_mode=='no-network'
        assert projected.verifier_phase.network_mode=='no-network'
        assert projected.verifier_env_baseline is None
        assert original.agent_env_baseline.network_mode=='public'
    assert Trial._network_plan is resolver
    public.allowed_hosts=['example.invalid']
    with pytest.raises(ValueError,match='allowlists'):
        with harbor_modern.private_network_plans(Trial):Trial()._network_plan()
    assert Trial._network_plan is resolver


def modern_lock(plan):
    lock = copy.deepcopy(plan['task_resources'])
    for name in ('src/harbor/trial/single_step.py', 'src/harbor/models/task/verifier_mode.py',
                 'src/harbor/environments/capabilities.py'):
        lock['trees']['harbor']['files'][name] = '0'*64
    return lock


def test_modern_planning_requires_the_actual_source_api_and_phase_resources(tmp_path):
    _, plan, _, config, _, job, _ = prepared(tmp_path)
    found = copy.deepcopy(job['task']); lock = modern_lock(plan)
    found['initial_state']['verifier_environment'] = {'gpus':1, 'gpu_types':['H100']}
    found['evaluation']['verifier'] = {'environment_mode':'separate', 'collect':[{'command':'author capture'}]}
    record = lock['tasks'][found['id']]
    record['images']['grading']['verifier']['id'] = GRADER
    assert any('verifier GPU' in value for value in harbor_driver.requirements(config, [found], lock))
    record['verifier_gpu_device_ids'] = [A]
    assert harbor_driver.requirements(config, [found], lock) == []
    assert harbor_protocol.api(lock) == 'modern'
    assert any('separate verifier' in value for value in harbor_driver.requirements(config, [found], plan['task_resources']))
    lock['trees']['harbor']['files'].pop('src/harbor/trial/single_step.py')
    assert harbor_protocol.api(lock) == 'unsupported'
    assert any('transitional' in value for value in harbor_driver.requirements(config, [found], lock))


@pytest.mark.parametrize('change', ['steps','windows','tpu','allowlist','grader-services'])
def test_unsupported_modern_tasks_have_explicit_plan_requirements(tmp_path, change):
    _,plan,_,config,_,job,_=prepared(tmp_path); found=copy.deepcopy(job['task']); lock=modern_lock(plan)
    if change=='steps':found['initial_state']['steps']=[{'name':'step-one'}]
    if change=='windows':found['initial_state']['environment']['os']='windows'
    if change=='tpu':found['initial_state']['environment']['tpu']={'type':'tpu'}
    if change=='allowlist':found['initial_state']['environment']['network_mode']='allowlist'
    if change=='grader-services':
        found['initial_state']['verifier_environment']={}
        found['inputs'].append({'path':str(tmp_path/'tests/docker-compose.yaml')})
    assert harbor_driver.requirements(config,[found],lock)


@pytest.mark.parametrize('config,expected', [({'environment':{'gpus':1},'verifier':{'environment_mode':'separate'}},{'gpus':1}),
    ({'environment':{'gpus':1},'verifier':{'environment':{}}},{}), ({'environment':{},'verifier':{}},None)])
def test_separate_resource_inheritance_distinguishes_an_explicit_empty_environment(config,expected):
    assert harbor_protocol.verifier_environment(config)==expected


def test_phase_resource_capture_binds_grader_gpus_before_image_inspection(tmp_path,monkeypatch):
    from test_task_resources import capture_spec
    root,_,spec=capture_spec(tmp_path)
    (root/'task-one/task.toml').write_text('[environment]\ngpus=0\n[verifier.environment]\ngpus=1\ngpu_types=["H100"]\n', encoding='utf-8')
    selected=benchmarks.get('terminal-bench').task_instances(root)
    monkeypatch.setattr(eval_environment,'image',lambda value:pytest.fail('inspected invalid phase resources'))
    with pytest.raises(ValueError,match='GPU UUIDs'):task_resources.capture(spec,selected)
    spec['tasks'][selected[0]['id']]['verifier_gpu_device_ids']=[A]
    monkeypatch.setattr(eval_environment,'image',lambda value:{'id':IMAGE})
    lock=task_resources.capture(spec,selected)
    assert lock['tasks'][selected[0]['id']]['verifier_gpu_device_ids']==[A]
    assert harbor_protocol.claimed_gpus(dict(gpu_device_ids=None,verifier_gpu_device_ids=[A]))==[A]
    assert harbor_protocol.claimed_gpus(dict(gpu_device_ids=[A],verifier_gpu_device_ids=[A.lower()]))==[A]


def test_scheduler_reserves_grader_only_devices_without_consuming_cpu_slots():
    from ctxpress.harness.evaluation import _runnable
    def row(name,**resources):return dict(id=name,spec=json.dumps({'resources':resources}))
    live=[row('grading',verifier_gpu_device_ids=[A])]
    pending=[row('overlap',gpu_device_ids=[A]),row('grader-overlap',verifier_gpu_device_ids=[A]),row('cpu')]
    assert [item['id'] for item in _runnable(pending,live,3)]==['cpu']


def frozen_modern(tmp_path):
    from ctxpress.harness import eval_inputs, eval_trees, evaluation, task as task_api
    cfg,plan,_,_,_,job,_=prepared(tmp_path)
    root=Path(cfg['environment']['data'])
    (root/'task-one/task.toml').write_text('[environment]\ngpus=0\n[verifier]\nenvironment_mode="separate"\n[verifier.environment]\ngpus=1\ngpu_types=["H100"]\n', encoding='utf-8')
    found=benchmarks.get('terminal-bench').task_instances(root)[0]
    sources=tmp_path/'harbor'
    for name in ('src/harbor/trial/single_step.py', 'src/harbor/models/task/verifier_mode.py',
                 'src/harbor/environments/capabilities.py'):
        path=sources/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# modern API fixture\n', encoding='utf-8')
    lock=copy.deepcopy(plan['task_resources']);lock.pop('sha256')
    names=[path.relative_to(sources).as_posix() for path in sources.rglob('*') if path.is_file()]
    lock['trees']['harbor']=eval_trees.capture(sources,names,folder='unused')['tree']
    record=lock['tasks'][found['id']];record['task_sha256']=task_resources.task_digest(found)
    record['verifier_gpu_device_ids']=[A]
    Path(cfg['environment']['resources']).write_text(json.dumps(task_resources.seal(lock)), encoding='utf-8')
    plan=eval_plan.compile_plan(cfg);assert not plan['missing_environment_files']
    directory=evaluation.prepare(plan,tmp_path/'modern-run')
    effective,paths=eval_inputs.execution(plan,directory/'inputs');job=plan['jobs'][0]
    return plan,directory,effective,job,task_api.remap(job['task'],paths)


def test_prepare_dispatches_frozen_modern_api_and_separate_gpu_phase(tmp_path,monkeypatch):
    plan,directory,config,job,found=frozen_modern(tmp_path)
    credentials=tmp_path/'auth.json';credentials.write_text('synthetic fixture', encoding='utf-8')
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE',str(credentials))
    monkeypatch.setattr(harbor_driver.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(stdout='codex-cli fixture-version\n'))
    monkeypatch.setattr(harbor_driver,'docker',lambda *args:pytest.fail('prepare contacted Docker'))
    (directory/'attempt').mkdir()
    request,_=harbor_driver.prepare(found,job['method'],config,job,directory/'attempt','plan-job')
    assert request['harbor_api']=='modern' and request['separate_verifier']
    assert request['gpu_device_ids'] is None and request['verifier_gpu_device_ids']==[A]
    assert request['grading_image']==IMAGE and not request['verifier_bundled_tests']
    assert 'auth' not in json.dumps(request)
    actual=harbor_modern.trial_config(request,lambda **kwargs:kwargs,tmp_path/'channel')
    assert actual['agent']['import_path']=='ctxpress.harness.harbor_modern:CtxpressCodex'
    assert len(actual['environment']['mounts'])==4 and all(mount['read_only'] for mount in actual['environment']['mounts'])


def test_reports_keep_grader_gpu_binding_without_agent_devices(tmp_path):
    from ctxpress.harness import evaluation
    from ctxpress.harness.eval_report import report, write_report
    from ctxpress.benchmarks import harbor_gpu
    _,directory,_,job,_=frozen_modern(tmp_path)
    evidence=harbor_gpu.observed(QUERY,[A],['H100'])
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET status='completed',attempt=1,result=? WHERE id=?",(json.dumps(dict(test_only=True,
            verifier_gpu=evidence,network_policy='ctxpress_private_services_model_socket_only',harbor_api='single_step',
            protocol='ctxpress_comparison',real_run_verified=False,grade={'rewards':{'reward':0.25},'valid_rewards':True})),job['id']))
    write_report(directory);result=report(directory);actual=result['methods'][0]['jobs'][0]
    assert actual['verifier_gpu_device_ids']==[A] and actual['verifier_gpu']==evidence
    assert not actual.get('gpu_device_ids') and actual['harbor_api']=='single_step'
    markup=(directory/'report.html').read_text(encoding='utf-8')
    assert 'Verifier' in markup and A in markup and 'NVIDIA H100' in markup


@pytest.mark.parametrize('streaming',[False,True])
def test_compose_transfer_drains_large_binary_stdin_and_both_output_streams(streaming):
    async def run():
        payload=b'\x00\xfffixture'*20000
        command='import sys; data=sys.stdin.buffer.read(); sys.stderr.buffer.write(b"error"*30000); sys.stdout.buffer.write(data)'
        process=await asyncio.create_subprocess_exec(sys.executable,'-c',command,stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        chunks=[]
        async def output(text,name):chunks.append((text,name))
        stdout,stderr=await asyncio.wait_for(compose_output(process,payload,output if streaming else None),10)
        assert stdout==payload and stderr==b'error'*30000 and process.returncode==0
        if streaming:
            assert ''.join(text for text,name in chunks if name=='stdout')==payload.decode(errors='replace')
            assert ''.join(text for text,name in chunks if name=='stderr')=='error'*30000
    asyncio.run(run())


class EnvConfig(SimpleNamespace):
    def model_copy(self,update,deep=False):
        value=copy.deepcopy(self) if deep else copy.copy(self)
        value.__dict__.update(update); return value


@pytest.mark.parametrize('separate,bundled,gpu,sidecar,cleanup_failure', [
    (False,False,False,False,False),(True,False,False,False,False),
    (True,True,True,False,False),(True,True,False,True,False),(True,True,False,False,True)])
def test_modern_trial_create_preserves_author_collection_and_fresh_grader_lifecycle(
        tmp_path,monkeypatch,separate,bundled,gpu,sidecar,cleanup_failure):
    events=[]; trial_root=tmp_path/PROJECT; task=tmp_path/'task-one'
    for name in ('environment','tests'): (task/name).mkdir(parents=True)
    (task/'tests/test.sh').write_text('author verifier', encoding='utf-8')
    class Agent(OfficialFixture):
        def __init__(self,logs_dir,**kwargs):super().__init__(**kwargs); self.logs_dir=logs_dir
        def render_instruction(self,instruction):return 'official prompt: '+instruction
        def _build_effective_config(self):return {'mcp_servers':{'fixture':{'command':'fixture tool','args':['literal $(x)']}}}
    class Docker(EnvironmentFixture):
        def __init__(self,**kw):
            super().__init__(); self.session_id=kw['session_id'];self.environment_dir=kw['environment_dir']
            self.trial_paths=kw['trial_paths'];self.task_env_config=kw['task_env_config'];self._mounts=kw['mounts']
            self._env_vars=SimpleNamespace(prebuilt_image_name=self.task_env_config.docker_image)
            self.default_user='agent' if self.ctxpress_role=='agent' else None
            events.append(('mounts',self.ctxpress_role,self._mounts))
        @property
        def _docker_compose_paths(self):return []
        def _validate_daemon_mode(self):pass
        async def _validate_image_os(self,image):pass
        def _write_mounts_compose_file(self):return task/'mounts.json'
        def _write_resources_compose_file(self):events.append(('limits',self.ctxpress_role));return None
        def _write_env_compose_file(self):events.append(('startup-env',self.ctxpress_role));return None
        def _cleanup_mounts_compose_file(self):pass
        def _cleanup_resources_compose_file(self):pass
        def _cleanup_env_compose_file(self):pass
        def _mount_targets(self,**kwargs):return []
        async def ensure_dirs(self,targets):pass
        async def _upload_environment_dir_after_start(self):events.append(('upload-environment',self.ctxpress_role))
        async def _chown_to_host_user(self,*args,**kwargs):events.append(('chown',self.ctxpress_role))
        async def stop_service(self,service):events.append(('stop-service',service))
        async def exec(self,command,**kwargs):
            if command.startswith('nvidia-smi'):return SimpleNamespace(return_code=0,stdout=QUERY)
            if command.startswith('sha256sum'):
                name=shlex.split(command)[-1].removeprefix('/tests/')
                return SimpleNamespace(return_code=0,stdout=eval_plan.file_sha256(task/'tests'/name)+'  /tests/'+name)
            return await super().exec(command,**kwargs)
    def environment(role,session,mounts):
        instance=harbor_modern.CtxpressDocker(session_id=session,trial_paths=SimpleNamespace(trial_dir=trial_root),
            environment_dir=task/('tests' if role=='verifier' and bundled else 'environment'),environment_name='task-one',
            task_env_config=EnvConfig(docker_image=None,gpus=int(gpu and role=='verifier'),gpu_types=['H100'] if gpu else None),
            mounts=mounts)
        async def compose(args,**kwargs):
            if args[0]=='config':return SimpleNamespace(stdout=json.dumps({'services':{'main':{'image':'moving'}}}))
            events.append((args[0],role))
            if cleanup_failure and args[0]=='down' and role=='agent':raise RuntimeError('cleanup failed')
            return SimpleNamespace(return_code=0)
        instance._run_docker_compose_command=compose;return instance
    class Trial:
        def __init__(self,*args):pytest.fail('called abstract Trial constructor')
        @classmethod
        async def create(cls,config):
            events.append('official-create')
            for name in ('agent','verifier','artifacts'):(trial_root/name).mkdir(parents=True)
            instance=SimpleNamespace(task=SimpleNamespace(config={},paths={}))
            instance.agent=harbor_modern.CtxpressCodex(logs_dir=trial_root/'agent',model_name='fixture-model')
            mounts=config['environment']['mounts']+[dict(type='bind',source=str(trial_root/'verifier'),target='/logs/verifier')]
            instance.agent_environment=environment('agent',PROJECT+'__env',mounts)
            async def run():
                await instance.agent_environment.start();await instance.agent.setup(instance.agent_environment)
                events.append('agent');await instance.agent.run('instruction',instance.agent_environment,{})
                events.append('author-collect');(trial_root/'artifacts/outcome.bin').write_bytes(b'author evidence')
                if sidecar:await instance.agent_environment.stop_service('main')
                if separate:
                    try:await instance.agent_environment.stop()
                    except RuntimeError:pass  # Actual Trial records this; factory must still block grader.
                    grader=environment('verifier',PROJECT+'__verifier__trial',
                        [dict(type='bind',source=str(trial_root/'verifier'),target='/logs/verifier')])
                    try:
                        await grader.start();events.append('author-artifact-transfer');events.append('official-verifier')
                    finally:await grader.stop()
                else:
                    events.append('official-verifier');await instance.agent_environment.stop()
                return SimpleNamespace(exception_info=None)
            instance.run=run;return instance
    request=dict(project=PROJECT,label='plan-job',images={'main':IMAGE},grading_image=GRADER,separate_verifier=separate,
        verifier_bundled_tests=bundled,verifier_gpu_device_ids=[A] if gpu else None,
        folder=str(tmp_path),task=str(task),package=str(tmp_path/'package'),bindir=str(tmp_path/'bin'),profiles=str(tmp_path/'profiles'),
        method={'class':'NoCompaction'},model='fixture-model',reasoning='medium',binary_version='0.159.0-alpha.12.1',
        compact_limit=230000,upstream='https://provider.invalid/v1',run={'max_calls':2,'timeout':99,'grade':True})
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE','/runtime-only/auth.json')
    monkeypatch.setattr(eval_environment,'image',lambda image:{'id':image})
    monkeypatch.setattr(asyncio.BaseEventLoop,'add_signal_handler',lambda *args:None)
    monkeypatch.setattr(asyncio.BaseEventLoop,'remove_signal_handler',lambda *args:None)
    official=(Agent,RuntimeError,Docker,SimpleNamespace,lambda **kwargs:kwargs,Trial,
        lambda config,paths:SimpleNamespace(bundled_tests=bundled) if separate else None,SimpleNamespace)
    execution=harbor_modern.run_trial(request,official,tmp_path/'channel',{'daemon_id':'fixture-daemon'})
    if cleanup_failure:
        with pytest.raises(RuntimeError,match='shutdown'):asyncio.run(execution)
        assert ('up','verifier') not in events and 'official-verifier' not in events;return
    asyncio.run(execution)
    assert events[0]=='official-create'
    if separate:
        assert events.index('author-collect')<events.index(('down','agent'))<events.index(('up','verifier'))<events.index('official-verifier')
        for event in events:
            if isinstance(event,tuple) and event[0]=='mounts':
                assert not any(mount['target']=='/logs/verifier' for mount in event[2]) if event[1]=='agent' else len(event[2])==1
    result=json.loads((tmp_path/'harbor-worker-result.json').read_text(encoding='utf-8'))
    assert result['harbor_api']=='single_step' and result['network_policy']=='ctxpress_private_services_model_socket_only'
    assert 'artifacts/outcome.bin' in result['official_artifacts']
    if gpu:assert result['verifier_gpu']['devices'][0]['uuid']==A and result['gpu'] is None
    records=[json.loads(path.read_text(encoding='utf-8')) for path in tmp_path.glob('resources-harbor-*.json')]
    assert all(record['cleaned'] and not record['credentials_may_exist'] for record in records)
    if sidecar:assert events.index(('chown','agent'))<events.index(('stop-service','main'))<events.index(('down','agent'))
