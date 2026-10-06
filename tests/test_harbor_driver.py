"""Frozen inputs, owned recovery and official trial dispatch; no models or daemon."""
import asyncio, copy, json, shutil, sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from ctxpress import benchmarks
from ctxpress.benchmarks.harbor import driver as harbor_driver
from ctxpress.harness.jobs import environment as eval_environment, inputs as eval_inputs, plan as eval_plan, trees as eval_trees, queue as evaluation, task, resources as task_resources
from ctxpress.core import artifacts as artifact_io
from ctxpress.benchmarks.harbor.worker import run_trial, trial_config
from test_original_benchmarks import terminal_data

IMAGE = 'sha256:' + 'a'*64
PROJECT = 'ctxp-hb-' + 'b'*24


def prepared(tmp_path):
    root = terminal_data(tmp_path)
    (root/'dataset_manifest.json').write_text(json.dumps(dict(dataset='terminal-bench',revision='fixture-v1')), encoding='utf-8')
    adapter = benchmarks.get('terminal-bench'); found = adapter.task_instances(root)[0]
    binary = tmp_path/'bin'; binary.mkdir(); (binary/'codex').write_bytes(b'fixture cli')
    sources = tmp_path/'harbor'; dependencies = tmp_path/'dependencies'; sources.mkdir(); dependencies.mkdir()
    names = ['pyproject.toml','src/harbor/__init__.py','src/harbor/trial/trial.py',
             'src/harbor/agents/installed/codex.py','src/harbor/environments/docker/docker.py']
    for name in names:
        path = sources/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text('# fixture\n', encoding='utf-8')
    metadata = dependencies/'harbor-0.1.45.dist-info/METADATA';metadata.parent.mkdir();metadata.write_text('Name: harbor\nVersion: 0.1.45\n', encoding='utf-8')
    trees = {key:eval_trees.capture(path, names if key=='harbor' else ['harbor-0.1.45.dist-info/METADATA'],folder='unused')['tree']
             for key,path in [('harbor',sources),('dependencies',dependencies)]}
    runtime = dict(python=str(Path(sys.executable).resolve()),sha256=eval_plan.file_sha256(sys.executable),
                   version=sys.version,platform='linux')  # Declared Linux runner fixture; no official execution occurs.
    record = dict(task_sha256=task_resources.task_digest(found),images=dict(agent={'id':IMAGE},grading={'verifier':{'id':IMAGE}}))
    lock = task_resources.seal(dict(schema=task_resources.SCHEMA,version=1,benchmark='terminal-bench',release='fixture-v1',
        tasks={found['id']:record},trees=trees,runtime=runtime))
    resources = tmp_path/'resources.json';resources.write_text(json.dumps(lock), encoding='utf-8')
    cfg = dict(schema='ctxpress.eval',version=1,benchmark='terminal-bench',start_mode='task_start',scope='benchmark',
        model='fixture-model',backend='codex_docker',tasks=[found['id']],environment=dict(data=str(root),bindir=str(binary),resources=str(resources)),
        methods=[{'class':'NoCompaction'}])
    plan=eval_plan.compile_plan(cfg);directory=evaluation.prepare(plan,tmp_path/'run')
    assert not plan['missing_environment_files']
    effective,paths=eval_inputs.execution(plan,directory/'inputs');job=plan['jobs'][0]
    return cfg,plan,directory,effective,paths,job,task.remap(job['task'],paths)


def test_prepare_executes_only_remapped_tasks_after_originals_are_removed(tmp_path,monkeypatch):
    cfg,plan,directory,effective,paths,job,frozen=prepared(tmp_path)
    shutil.rmtree(Path(cfg['environment']['data']));shutil.rmtree(tmp_path/'harbor');shutil.rmtree(tmp_path/'dependencies')
    credentials=tmp_path/'auth.json';credentials.write_text('synthetic credentials fixture', encoding='utf-8')
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE',str(credentials))
    monkeypatch.setattr(harbor_driver.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout='codex-cli fixture-version\n'))
    monkeypatch.setattr(harbor_driver,'docker',lambda *a:pytest.fail('prepare contacted Docker'))
    folder=directory/'attempt';folder.mkdir()
    request,auth=harbor_driver.prepare(frozen,job['method'],effective,job,folder,'plan-job')
    assert request['task'].startswith(str(directory/'inputs/task-data'))
    assert not Path(frozen['initial_state']['task_directory']).exists()
    assert request['images']=={'main':IMAGE} and request['runtime']==plan['task_resources']['runtime']
    assert auth==str(credentials) and 'auth' not in json.dumps(request)
    config=trial_config(request,lambda **kw:kw,tmp_path/'channel')
    assert config['task']=={'path':request['task']} and config['verifier']=={'disable':False}
    assert config['agent']['override_timeout_sec']==effective['run']['timeout']
    assert config['agent']['import_path']=='ctxpress.benchmarks.harbor.worker:CtxpressCodex'
    assert all(volume['read_only'] and '/tests' not in volume['source'] for volume in config['environment']['mounts_json'])


@pytest.mark.parametrize('change',['source','metadata','runtime','platform','python-version','gpu','grading','release'])
def test_plan_lists_concrete_unprepared_protocol_requirements(tmp_path,change):
    _,plan,_,config,_,job,_=prepared(tmp_path)
    lock=copy.deepcopy(plan['task_resources']);found=copy.deepcopy(job['task'])
    if change=='source':lock['trees']['harbor']['files'].pop('src/harbor/trial/trial.py')
    if change=='metadata':lock['trees']['dependencies']['files']={}
    if change=='runtime':lock.pop('runtime')
    if change=='platform':lock['runtime']['platform']='win32'
    if change=='python-version':lock['runtime']['version']='3.11.10 fixture'
    if change=='gpu':found['initial_state']['environment']['gpus']=1
    if change=='grading':lock['tasks'][found['id']]['images']['grading']['verifier']['id']='sha256:'+'c'*64
    if change=='release':lock['release']='another'
    assert harbor_driver.requirements(config,[found],lock)


def journal(tmp_path):
    record=dict(schema=harbor_driver.SCHEMA,version=1,label='plan-job',project=PROJECT,images={'main':IMAGE},
        daemon_id='fixture-daemon',cleaned=False,channel=str(tmp_path/'absent-channel'))
    path=tmp_path/'resources-harbor-fixture.json';artifact_io.atomic_json(path,record)
    return path,record


def container(record,**labels):
    return dict(Id='fixture-container',Image=IMAGE,Config={'Labels':{
        'com.docker.compose.project':record['project'],'com.docker.compose.service':'main',
        'ctxpress.run':record['label'],'ctxpress.managed':'true',**labels}})


def docker_fixture(record, value, calls):
    def run(*args):
        calls.append(args)
        if args[0]=='info':return 'fixture-daemon\n'
        if args[:2]==('ps','-aq'):return 'fixture-container\n'
        if args[:2]==('container','inspect'):return json.dumps([value])
        if args[0] in ('network','volume') and args[1]=='ls':return ''
        return ''
    return run


def test_recovery_verifies_ownership_before_mutating_or_deleting_any_resource(tmp_path,monkeypatch):
    path,record=journal(tmp_path);calls=[]
    monkeypatch.setattr(harbor_driver,'docker',docker_fixture(record,container(record,**{'ctxpress.run':'another-job'}),calls))
    with pytest.raises(ValueError,match='ownership'):harbor_driver.recover(path,'plan-job')
    assert all(args[0] in ('info','ps','container') for args in calls)
    assert not json.loads(path.read_text(encoding='utf-8'))['cleaned']


def test_recovery_removes_credentials_before_container_and_keeps_images(tmp_path,monkeypatch):
    path,record=journal(tmp_path);calls=[]
    monkeypatch.setattr(harbor_driver,'docker',docker_fixture(record,container(record),calls))
    harbor_driver.recover(path,'plan-job')
    operations=[args[0] for args in calls]
    assert operations[-5:]==['stop','start','exec','stop','rm']
    assert 'test ! -L /ctxpress-private/codex/auth.json' in calls[-3][-1]
    assert not any(args[0]=='image' for args in calls)
    assert json.loads(path.read_text(encoding='utf-8'))['cleaned']
    calls.clear();harbor_driver.recover(path,'plan-job');assert not calls


def test_gpu_start_failure_without_uploaded_credentials_does_not_require_a_restart(tmp_path,monkeypatch):
    path,record=journal(tmp_path);record['credentials_may_exist']=False;artifact_io.atomic_json(path,record)
    calls=[];base=docker_fixture(record,container(record),calls)
    def docker(*args):
        if args[0] in ('start','exec'):pytest.fail('required GPU startup to remove credentials that were never uploaded')
        return base(*args)
    monkeypatch.setattr(harbor_driver,'docker',docker)
    harbor_driver.recover(path,'plan-job')
    assert calls[-1][0]=='rm' and json.loads(path.read_text(encoding='utf-8'))['cleaned']


@pytest.mark.parametrize('exception',['AgentTimeoutError','NonZeroAgentExitCodeError'])
def test_valid_official_reward_survives_agent_failure_without_claiming_resolution(tmp_path,exception):
    root=terminal_data(tmp_path);adapter=benchmarks.get('terminal-bench');found=adapter.task_instances(root)[0]
    report=tmp_path/'result.json';report.write_text(json.dumps(dict(task_name=found['id'],
        verifier_result={'rewards':{'accuracy':0.5}},exception_info={'exception_type':exception})), encoding='utf-8')
    grade=adapter.read_grade(found,report)
    assert grade['valid_rewards'] and grade['resolved'] is None and grade['agent_exception']['exception_type']==exception


@pytest.mark.parametrize('gpu',[False,True])
def test_worker_invokes_trial_with_owned_hooks_and_verifier_after_agent(tmp_path,monkeypatch,gpu):
    from ctxpress.benchmarks.harbor import worker as harbor_worker
    from test_harbor_codex import OfficialFixture as AgentBase,EnvironmentFixture
    from test_harbor_gpu import A,QUERY
    events=[]
    class Agent(AgentBase):
        def __init__(self,logs_dir,**kwargs):
            super().__init__(**kwargs);self.logs_dir=logs_dir
        async def run(self,instruction,environment,context):
            events.append('agent');return await super().run(instruction,environment,context)
    class Docker(EnvironmentFixture):
        def __init__(self,folder,**kwargs):
            super().__init__();self.session_id=PROJECT;self.environment_dir=folder/'environment'
            self.trial_paths=SimpleNamespace(trial_dir=folder)
            self.task_env_config=SimpleNamespace(docker_image=IMAGE,gpus=int(gpu),gpu_types=['H100'] if gpu else None)
            self._env_vars=SimpleNamespace(prebuilt_image_name=IMAGE)
            self._mounts_json=None
        @property
        def _docker_compose_paths(self):return []
        async def _chown_to_host_user(self,*args,**kwargs):pass
        async def exec(self,command,**kwargs):
            if command.startswith('nvidia-smi '):
                events.append('gpu-check');return SimpleNamespace(stdout=QUERY,return_code=0)
            return await super().exec(command,**kwargs)
    class Trial:
        def __init__(self,config):
            events.append(('config',config))
            self._agent=harbor_worker.CtxpressCodex(logs_dir=tmp_path/PROJECT/'agent',model_name='fixture-model')
            self._environment=harbor_worker.CtxpressDocker(tmp_path/PROJECT)
            async def compose(args,**kw):
                if args[0]=='config':return SimpleNamespace(stdout=json.dumps(dict(services={'main':{'image':IMAGE}})))
                events.append(args[0]);return SimpleNamespace(return_code=0)
            self._environment._run_docker_compose_command=compose
        async def run(self):
            (tmp_path/PROJECT).mkdir()
            await self._environment.start();await self._agent.setup(self._environment)
            await self._agent.run('official instruction',self._environment,{})
            assert self._agent.ctxpress_credentials_cleaned
            events.append('official-verifier')
            await self._environment.stop()
            return SimpleNamespace(exception_info=None)
    request=dict(project=PROJECT,label='plan-job',images={'main':IMAGE},folder=str(tmp_path),package=str(tmp_path/'package'),
        bindir=str(tmp_path/'bin'),profiles=str(tmp_path/'profiles'),task=str(tmp_path/'task'),method={'class':'NoCompaction'},
        model='fixture-model',reasoning='medium',binary_version='0.159.0-alpha.12.1',compact_limit=230000,
        upstream='https://provider.invalid/v1',run={'max_calls':2,'timeout':99,'grade':True})
    if gpu:request['gpu_device_ids']=[A]
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE','/runtime-only/auth.json')
    monkeypatch.setattr(eval_environment,'image',lambda name:{'id':name})
    monkeypatch.setattr(asyncio.BaseEventLoop,'add_signal_handler',lambda *a:None)
    monkeypatch.setattr(asyncio.BaseEventLoop,'remove_signal_handler',lambda *a:None)
    asyncio.run(run_trial(request,(Agent,SimpleNamespace,RuntimeError,Docker,SimpleNamespace,lambda **kw:kw,Trial),
        tmp_path/'channel',dict(daemon_id='fixture-daemon')))
    assert events[1:]==['up',*(['gpu-check'] if gpu else []),'agent','official-verifier','down']
    assert events[0][1]['verifier']=={'disable':False}
    owner=json.loads(next(tmp_path.glob('resources-harbor-*.json')).read_text(encoding='utf-8'))
    assert owner['cleaned'] and owner['compose_sha256'] and owner['credentials_may_exist'] is False
    state=json.loads((tmp_path/'harbor-worker-result.json').read_text(encoding='utf-8'))
    assert state['stop']=='completed' and state['calls']==0
    if gpu:assert state['gpu']['devices'][0]['uuid']==A and owner['gpu']==state['gpu']
