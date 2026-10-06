"""Versioned Pro data and fresh official-trial composition; all runtime IO is synthetic."""
import asyncio, copy, json, shutil, sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from ctxpress import benchmarks
from ctxpress.benchmarks.harbor import driver as harbor_driver
from ctxpress.harness.runtime import socket_bridge
from ctxpress.benchmarks.pro import protocol as pro_protocol
from ctxpress.harness.jobs import environment as eval_environment, inputs as eval_inputs, plan as eval_plan, trees as eval_trees, queue as evaluation, task, resources as task_resources
from ctxpress.benchmarks.harbor import modern as harbor_modern, worker as harbor_worker
from ctxpress.benchmarks.pro import trial as pro_trial
from test_harbor_codex import OfficialFixture, EnvironmentFixture
from test_harbor_modern import EnvConfig
from test_original_benchmarks import terminal_data
from test_harbor_driver import PROJECT, IMAGE

COMMIT='d'*40
GRADER='sha256:'+'c'*64


def data(tmp_path,subset='public'):
    root=terminal_data(tmp_path);directory=root/'task-one'
    (directory/'task.toml').write_text('[task]\nname="swebench-pro/task-one"\n[environment]\nos="linux"\n[agent]\nnetwork_mode="no-network"\n', encoding='utf-8')
    for name in ('run_script.sh','parser.py','config.json','test_patch.patch'):
        (directory/'tests'/name).write_text('hidden official test input', encoding='utf-8')
    manifest=dict(dataset='swe-bench-pro',revision='fixture-v2',benchmark_version='v2',subset=subset,base_commits={'task-one':COMMIT})
    (root/'dataset_manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    if subset=='hard51':(root/'hard51_ids.txt').write_text('task-one\n', encoding='utf-8')
    return root


@pytest.mark.parametrize('subset',['public','hard51'])
def test_pro_catalog_keeps_version_subset_base_and_private_tests(tmp_path,subset):
    root=data(tmp_path,subset);adapter=benchmarks.get('swe-bench-pro');found=adapter.task_instances(root)[0]
    assert found['initial_state']['base_commit']==COMMIT and found['initial_state']['pro_version']=='v2'
    assert found['evaluation']['dataset']['subset']==subset and found['evaluation']['kind']=='official-pro-v2-fresh-regrade'
    assert 'hidden' not in adapter.agent_instruction(found)
    assert all(item['role']=='grading' for item in found['inputs'] if Path(item['path']).parent.name=='tests')
    assert not any('/solution/' in item['path'] for item in found['inputs'])
    assert not adapter.describe()['real_run_verified'] and adapter.describe()['implemented_versions']==['v1','v2']


@pytest.mark.parametrize('change',['v1','subset','base','network','tests','hard-list'])
def test_pro_version_and_protocol_mismatches_fail_at_discovery(tmp_path,change):
    root=data(tmp_path,'hard51');path=root/'dataset_manifest.json';meta=json.loads(path.read_text(encoding='utf-8'))
    if change=='v1':meta['benchmark_version']='v1'
    if change=='subset':meta['subset']='unknown'
    if change=='base':meta['base_commits']['task-one']='main'
    if change=='network':(root/'task-one/task.toml').write_text('[agent]\nnetwork_mode="public"\n', encoding='utf-8')
    if change=='tests':(root/'task-one/tests/test_patch.patch').unlink()
    if change=='hard-list':(root/'hard51_ids.txt').write_text('task-one\ntask-one\n', encoding='utf-8')
    path.write_text(json.dumps(meta), encoding='utf-8')
    with pytest.raises(ValueError):benchmarks.get('swe-bench-pro').task_instances(root)


def prepared(tmp_path,modern=True):
    root=data(tmp_path);adapter=benchmarks.get('swe-bench-pro');found=adapter.task_instances(root)[0]
    binary=tmp_path/'bin';binary.mkdir();(binary/'codex').write_bytes(b'fixture CLI')
    names=['pyproject.toml','src/harbor/__init__.py','src/harbor/trial/trial.py',
        'src/harbor/agents/installed/codex.py','src/harbor/environments/docker/docker.py']
    if modern:names+=['src/harbor/trial/single_step.py','src/harbor/models/task/verifier_mode.py','src/harbor/environments/capabilities.py']
    trees={}
    for key,files in {'harbor':names,'pro_tooling':['locked_codex.py','patch_replay.py'],
                      'dependencies':['harbor-0.22.0.dist-info/METADATA']}.items():
        directory=tmp_path/key
        for name in files:
            path=directory/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# fixture input\n', encoding='utf-8')
        trees[key]=eval_trees.capture(directory,files,folder='unused')['tree']
    runtime=dict(python=str(Path(sys.executable).resolve()),sha256=eval_plan.file_sha256(sys.executable),version=sys.version,platform='linux')
    resource=dict(task_sha256=task_resources.task_digest(found),images=dict(agent={'id':IMAGE},grading={'verifier':{'id':GRADER}}))
    lock=task_resources.seal(dict(schema=task_resources.SCHEMA,version=1,benchmark='swe-bench-pro',release='fixture-v2',
        tasks={found['id']:resource},trees=trees,runtime=runtime))
    resources=tmp_path/'resources.json';resources.write_text(json.dumps(lock), encoding='utf-8')
    config=dict(schema='ctxpress.eval',version=1,benchmark='swe-bench-pro',start_mode='task_start',scope='benchmark',model='fixture-model',
        backend='codex_docker',tasks=[found['id']],environment=dict(data=str(root),bindir=str(binary),resources=str(resources)),
        methods=[{'class':'NoCompaction'}],run=dict(timeout=3000,grade=True))
    plan=eval_plan.compile_plan(config);assert not plan['missing_environment_files']
    directory=evaluation.prepare(plan,tmp_path/'run');effective,paths=eval_inputs.execution(plan,directory/'inputs');job=plan['jobs'][0]
    return config,plan,directory,effective,job,task.remap(job['task'],paths)


@pytest.mark.parametrize('modern',[True,False])
def test_dispatch_uses_frozen_tasks_tooling_and_two_pinned_images(tmp_path,monkeypatch,modern):
    config,plan,directory,effective,job,found=prepared(tmp_path,modern)
    for name in ('data','harbor','pro_tooling','dependencies','bin'):
        target=tmp_path/name
        if target.exists():shutil.rmtree(target)
    auth=tmp_path/'auth.json';auth.write_text('fixture auth', encoding='utf-8');monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE',str(auth))
    monkeypatch.setattr(harbor_driver.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='codex-cli fixture-version\n'))
    attempt=directory/'attempt';attempt.mkdir()
    req,_=harbor_driver.prepare(found,job['method'],effective,job,attempt,'plan-job')
    assert req['pro_version']=='v2' and req['pro_base_commit']==COMMIT and req['grading_image']==GRADER
    assert req['images']=={'main':IMAGE} and 'hidden' not in json.dumps(req)
    assert ('harbor_api' in req)==modern and not plan['benchmark']['real_run_verified']


@pytest.mark.parametrize('change',['tooling','metadata','grading','services','release'])
def test_pro_planning_requires_complete_frozen_protocol_inputs(tmp_path,change):
    _,plan,_,config,job,found=prepared(tmp_path);lock=copy.deepcopy(plan['task_resources'])
    if change=='tooling':lock['trees']['pro_tooling']['files'].pop('patch_replay.py')
    if change=='metadata':lock['trees']['dependencies']['files']={}
    if change=='grading':lock['tasks'][found['id']]['images']['grading']={}
    if change=='services':lock['tasks'][found['id']]['images']['services']={'other':{'id':GRADER}}
    if change=='release':lock['release']='different'
    assert pro_protocol.requirements(config,[found],lock)


@pytest.mark.parametrize('change',['different-commit','dirty','missing'])
def test_repository_gate_runs_before_credentials_and_checks_the_declared_base(change):
    class Environment:
        async def exec(self,command,**kwargs):
            if change=='missing':return SimpleNamespace(return_code=1,stdout='')
            value='/app' if '--show-toplevel' in command else ('c'*40 if change=='different-commit' else COMMIT)
            if 'status --porcelain' in command:value=' M fixture.py' if change=='dirty' else ''
            return SimpleNamespace(return_code=0,stdout=value)
    with pytest.raises(ValueError):asyncio.run(pro_trial.check_repository(Environment(),COMMIT))


@pytest.mark.parametrize('patch_text,timeout', [('agent patch\n',False),('',False),('partial patch\n',True)])
@pytest.mark.parametrize('modern',[True,False])
def test_full_pro_pipeline_uses_native_guards_capture_and_clean_replay(tmp_path,monkeypatch,patch_text,timeout,modern):
    root=data(tmp_path);found=benchmarks.get('swe-bench-pro').task_instances(root)[0];events=[];configs=[];envs=[]
    class Codex(OfficialFixture):
        def __init__(self,logs_dir,**kwargs):super().__init__(**kwargs);self.logs_dir=logs_dir
        def render_instruction(self,instruction):return 'author prompt: '+instruction
        def _build_effective_config(self):return {}
        async def run(self,instruction,environment,context):
            if not modern and timeout:await asyncio.sleep(1)
            return await super().run(instruction,environment,context)
    class Locked(Codex):pass
    class PatchReplay:
        def __init__(self,logs_dir,source_job,patch_name,**kwargs):self.logs_dir=logs_dir
        async def setup(self,environment):pass
        async def run(self,instruction,environment,context):
            source=self._find_patch('task-one');events.append(('replay-patch',source.read_text(encoding='utf-8')))
            assert any(event=='agent-down' for event in events)
            await environment.upload_file(source,'/tmp/replay.patch')
        def _find_patch(self,*args):pytest.fail('replay searched external jobs')
    class Docker(EnvironmentFixture):
        def __init__(self,**kwargs):
            super().__init__();self.session_id=kwargs['session_id'];self.environment_dir=kwargs['environment_dir']
            self.trial_paths=kwargs['trial_paths'];self.task_env_config=kwargs['task_env_config'];self._mounts=kwargs['mounts']
            self._env_vars=SimpleNamespace(prebuilt_image_name=self.task_env_config.docker_image);self.default_user=None
            self.phase='agent' if self.session_id==PROJECT else 'regrade';envs.append(self)
        @property
        def _docker_compose_paths(self):return []
        def _validate_daemon_mode(self):pass
        async def _validate_image_os(self,image):pass
        def _write_resources_compose_file(self):return None
        def _write_env_compose_file(self):return None
        def _write_mounts_compose_file(self):return root/'mounts.json'
        def _cleanup_mounts_compose_file(self):pass
        def _cleanup_resources_compose_file(self):pass
        def _cleanup_env_compose_file(self):pass
        def _mount_targets(self,**kwargs):return []
        async def ensure_dirs(self,*args):pass
        async def _upload_environment_dir_after_start(self):pass
        async def _chown_to_host_user(self,*args,**kwargs):pass
        async def exec(self,command,**kwargs):
            events.append((self.phase,'exec',command))
            if command.startswith('git -C '):
                value='/app' if '--show-toplevel' in command else ('' if 'status --porcelain' in command else COMMIT)
                return SimpleNamespace(return_code=0,stdout=value)
            if command.endswith('; fixture-author-capture'):
                assert any('rm -f /ctxpress-private/codex/auth.json' in str(event) for event in events)
                (self.trial_paths.trial_dir/'agent/model.patch').write_text(patch_text, encoding='utf-8');events.append('captured-after-cleanup')
            if timeout and command.startswith('set -o pipefail; '):await asyncio.sleep(1)
            return await super().exec(command,**kwargs)
    class Trial:
        def __init__(self,config):self.__dict__.update(vars(self.build(config)))
        @classmethod
        async def create(cls,config):
            return cls.build(config)
        @classmethod
        def build(cls,config):
            configs.append(config);project=config['trial_name'];trial_root=tmp_path/project
            for name in ('agent','verifier','artifacts'):(trial_root/name).mkdir(parents=True)
            (trial_root/'config.json').write_text(json.dumps({'task':{'path':str(root/'task-one')}}), encoding='utf-8')
            instance=SimpleNamespace(task=SimpleNamespace(config={},paths={}))
            runner=harbor_modern if modern else harbor_worker
            instance.agent=runner.CtxpressCodex(logs_dir=trial_root/'agent',model_name=config['agent']['model_name'])
            mounts=config['environment']['mounts' if modern else 'mounts_json']+[dict(type='bind',source=str(trial_root/name),target='/logs/'+name) for name in ('agent','verifier','artifacts')]
            instance.agent_environment=runner.CtxpressDocker(session_id=project+'__env' if modern else project,
                trial_paths=SimpleNamespace(trial_dir=trial_root),environment_dir=root/'task-one/environment',environment_name='task-one',
                task_env_config=EnvConfig(docker_image=None,gpus=0,gpu_types=None),mounts=mounts)
            async def compose(args,**kwargs):
                if args[0]=='config':return SimpleNamespace(stdout=json.dumps({'services':{'main':{'image':'moving'}}}))
                if args[0]=='down':events.append('agent-down' if project==PROJECT else 'regrade-down')
                return SimpleNamespace(return_code=0)
            instance.agent_environment._run_docker_compose_command=compose
            instance._agent=instance.agent;instance._environment=instance.agent_environment
            async def run():
                exception=None
                try:
                    await instance.agent_environment.start();await instance.agent.setup(instance.agent_environment)
                    try:
                        if project==PROJECT and timeout:
                            await asyncio.wait_for(instance.agent.run('task',instance.agent_environment,{}),.01)
                        else:await instance.agent.run('task',instance.agent_environment,{})
                    except asyncio.TimeoutError:exception=SimpleNamespace(exception_type='AgentTimeoutError')
                    reward=None if config['verifier']['disable'] else {'reward':int(bool(patch_text))}
                    (trial_root/'result.json').write_text(json.dumps(dict(task_name='swebench-pro/task-one',trial_name=project,
                        verifier_result={'rewards':reward},exception_info=None)), encoding='utf-8')
                    return SimpleNamespace(exception_info=exception)
                finally:await instance.agent_environment.stop()
            instance.run=run;return instance
    request=dict(project=PROJECT,label='plan-job',images={'main':IMAGE},grading_image=GRADER,harbor_api='modern',
        pro_version='v2',pro_base_commit=COMMIT,benchmark='swe-bench-pro',task_id='task-one',separate_verifier=False,
        folder=str(tmp_path),task=str(root/'task-one'),package=str(tmp_path/'package'),bindir=str(tmp_path/'bin'),profiles=str(tmp_path/'profiles'),
        method={'class':'NoCompaction'},model='fixture-model',reasoning='medium',binary_version='0.159.0-alpha.12.1',
        compact_limit=230000,upstream='https://fixture.invalid/responses',run=dict(timeout=3000,max_calls=10,grade=True))
    official=dict(runtime=(Codex,RuntimeError,Docker,SimpleNamespace,lambda **kwargs:kwargs,Trial,lambda *args:None,SimpleNamespace),
        locked=Locked,capture='fixture-author-capture',replay=PatchReplay)
    if not modern:
        request.pop('harbor_api');official['runtime']=(Codex,SimpleNamespace,RuntimeError,Docker,SimpleNamespace,lambda **kwargs:kwargs,Trial)
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE','/runtime-only/auth.json')
    monkeypatch.setattr(eval_environment,'image',lambda identity:{'id':identity})
    monkeypatch.setattr(asyncio.BaseEventLoop,'add_signal_handler',lambda *args:None)
    monkeypatch.setattr(asyncio.BaseEventLoop,'remove_signal_handler',lambda *args:None)
    asyncio.run(pro_trial.run_trial(request,official,tmp_path/'channel',{'daemon_id':'fixture-daemon'}))
    state=json.loads((tmp_path/'harbor-worker-result.json').read_text(encoding='utf-8'))
    assert state['stop']==('timeout' if timeout else 'completed') and state['fresh_regrade']['regrade_model_calls']==0
    assert configs[0]['verifier']['disable'] and not configs[1]['verifier']['disable']
    assert configs[1]['environment']['mounts' if modern else 'mounts_json']==[] and configs[1]['agent']['model_name']=='replay'
    assert events.index('captured-after-cleanup')<events.index('agent-down')<events.index(('replay-patch',patch_text))
    assert envs[0].task_env_config.docker_image==IMAGE and envs[1].task_env_config.docker_image==GRADER
    assert envs[1].uploads==[(tmp_path/'pro-submission/model.patch','/tmp/replay.patch')]
    assert not any(mount['target'] in ('/cxbin','/ctxpress-runtime','/ctxpress-channel') for mount in envs[1]._mounts)
    grade=benchmarks.get('swe-bench-pro').read_grade(found,state['official_report'])
    assert grade['resolved']==bool(patch_text) and grade['authoritative_phase']=='fresh_regrade'
    socket_bridge.cleanup_channel(json.loads((tmp_path/('resources-harbor-'+state['fresh_regrade']['regrade_project']+'.json')).read_text(encoding='utf-8')))
    before=Path(state['official_report']).read_bytes();Path(state['official_report']).write_bytes(before+b' ')
    assert benchmarks.get('swe-bench-pro').read_grade(found,state['official_report'])['resolved'] is None


@pytest.mark.parametrize('evidence',[None,[]])
def test_raw_pro_reward_without_fresh_regrade_evidence_is_not_authoritative(tmp_path,evidence):
    root=data(tmp_path);adapter=benchmarks.get('swe-bench-pro');found=adapter.task_instances(root)[0]
    path=tmp_path/'trial/result.json';path.parent.mkdir();path.write_text(json.dumps(dict(task_name='swebench-pro/task-one',verifier_result={'rewards':{'reward':1}})), encoding='utf-8')
    if evidence is not None:(tmp_path/'pro-regrade.json').write_text(json.dumps(evidence), encoding='utf-8')
    grade=adapter.read_grade(found,path)
    assert grade['resolved'] is None and not grade['valid_rewards']


@pytest.mark.parametrize('change',[None,'origin','parent','capture','replay-api'])
def test_frozen_tooling_loader_rejects_incompatible_agent_and_replay_before_resources(tmp_path,monkeypatch,change):
    class Base:pass
    class Locked(Base):pass
    class Replay:
        def __init__(self,source_job,patch_name):pass
        def _find_patch(self,*args):pass
        async def setup(self,*args):pass
        async def run(self,*args):pass
    root=tmp_path/'pro_tooling'
    locked=SimpleNamespace(__file__=str(root/'locked_codex.py'),LockedCodex=Locked,_CAPTURE='author capture')
    replay=SimpleNamespace(__file__=str(root/'patch_replay.py'),PatchReplayAgent=Replay)
    if change=='origin':locked.__file__=str(tmp_path/'other/locked_codex.py')
    if change=='parent':locked.LockedCodex=object
    if change=='capture':locked._CAPTURE=''
    if change=='replay-api':Replay.__init__=lambda self,source_job:None
    monkeypatch.setattr(pro_trial.importlib,'import_module',lambda name:locked if name=='locked_codex' else replay)
    original_path=list(sys.path);monkeypatch.setattr(sys,'path',original_path)
    if change is None:
        result=pro_trial.load({'official':str(tmp_path)},(Base,))
        assert result['locked'] is Locked and result['replay'] is Replay
    else:
        with pytest.raises(ValueError,match='incompatible'):pro_trial.load({'official':str(tmp_path)},(Base,))
