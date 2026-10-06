"""DeepSWE/Pier contract fixtures, never actual benchmark or model scores."""
import asyncio, copy, json, shutil, sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from ctxpress import benchmarks
from ctxpress.benchmarks import deep_swe, harbor_driver, pier_codex
from ctxpress.harness import eval_inputs, eval_plan, eval_trees, evaluation, task, task_resources
from ctxpress.harness.pier_trial import submission, trial_config
from test_original_benchmarks import terminal_data
from test_harbor_driver import IMAGE, PROJECT

GRADER = 'sha256:' + 'c'*64
CONFIG = '''schema_version = "1.3"
artifacts = ["/logs/artifacts/model.patch"]
[task]
name = "datacurve/task-one"
[environment]
docker_image = "prepared:agent"
gpus = 0
cpus = 2
memory_mb = 8192
[verifier]
environment_mode = "separate"
network_mode = "no-network"
timeout_sec = 1800
[verifier.environment]
cpus = 2
memory_mb = 8192
[[verifier.collect]]
command = "cd /app && git diff --binary base HEAD > /logs/artifacts/model.patch"
timeout_sec = 300
'''


def data(tmp_path):
    root = terminal_data(tmp_path)
    (root/'dataset_manifest.json').write_text(json.dumps(dict(dataset='deep-swe', revision='v1.1-fixture')), encoding='utf-8')
    (root/'task-one'/'task.toml').write_text(CONFIG, encoding='utf-8')
    (root/'task-one'/'tests'/'Dockerfile').write_text('FROM prepared:agent\nCOPY . /tests/\n', encoding='utf-8')
    return root


def prepared(tmp_path):
    root = data(tmp_path); found = benchmarks.get('deep-swe').task_instances(root)[0]
    binary = tmp_path/'bin'; binary.mkdir(); (binary/'codex').write_bytes(b'fixture cli')
    sources = tmp_path/'pier'; dependencies = tmp_path/'dependencies'; sources.mkdir(); dependencies.mkdir()
    names = ['pyproject.toml', 'src/pier/__init__.py', 'src/pier/trial/trial.py',
             'src/pier/trial/artifact_handler.py', 'src/pier/models/task/verifier_mode.py',
             'src/pier/agents/installed/codex.py', 'src/pier/environments/docker/docker.py']
    for name in names:
        path = sources/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('# fixture\n', encoding='utf-8')
    meta = 'datacurve_pier-0.3.1.dist-info/METADATA'
    path = dependencies/meta; path.parent.mkdir(); path.write_text('Name: datacurve-pier\nVersion: 0.3.1\n', encoding='utf-8')
    trees = {key:eval_trees.capture(path, names if key == 'pier' else [meta], folder='unused')['tree']
             for key,path in [('pier',sources), ('dependencies',dependencies)]}
    runtime = dict(python=str(Path(sys.executable).resolve()), sha256=eval_plan.file_sha256(sys.executable),
                   version=sys.version, platform='linux')  # Declared runtime fixture only.
    record = dict(task_sha256=task_resources.task_digest(found),
                  images=dict(agent={'id':IMAGE}, grading={'verifier':{'id':GRADER}}))
    lock = task_resources.seal(dict(schema=task_resources.SCHEMA, version=1, benchmark='deep-swe',
        release='v1.1-fixture', tasks={found['id']:record}, trees=trees, runtime=runtime))
    resources = tmp_path/'resources.json'; resources.write_text(json.dumps(lock), encoding='utf-8')
    config = dict(schema='ctxpress.eval', version=1, benchmark='deep-swe', start_mode='task_start', scope='benchmark',
        model='fixture-model', backend='codex_docker', tasks=[found['id']],
        environment=dict(data=str(root), bindir=str(binary), resources=str(resources)), methods=[{'class':'NoCompaction'}])
    plan = eval_plan.compile_plan(config); directory = evaluation.prepare(plan, tmp_path/'run')
    assert not plan['missing_environment_files']
    effective, paths = eval_inputs.execution(plan, directory/'inputs'); job = plan['jobs'][0]
    return config, plan, directory, effective, paths, job, task.remap(job['task'], paths)


def test_catalog_distinguishes_pier_name_and_freezes_author_hooks_without_solution(tmp_path):
    root = data(tmp_path); found = benchmarks.get('deep-swe').task_instances(root)[0]
    assert found['id'] == 'task-one' and found['initial_state']['pier_task_name'] == 'datacurve/task-one'
    assert found['evaluation']['kind'] == 'official-pier-separate-verifier'
    assert not any('/solution/' in item['path'] for item in found['inputs'])
    assert benchmarks.get('deep-swe').describe()['execution_supported']
    assert not benchmarks.get('deep-swe').describe()['real_run_verified']


@pytest.mark.parametrize('change', ['manifest','shared','hooks','sidecar','artifact','dockerfile','name','steps','windows'])
def test_incompatible_deep_swe_tasks_are_not_relabelled_as_supported(tmp_path,change):
    root = data(tmp_path); path = root/'task-one'/'task.toml'; text = CONFIG
    if change == 'manifest': (root/'dataset_manifest.json').unlink()
    if change == 'shared': text = text.replace('environment_mode = "separate"', 'environment_mode = "shared"')
    if change == 'hooks': text = text.split('[[verifier.collect]]')[0]
    if change == 'sidecar': text += 'service = "database"\n'
    if change == 'artifact': text = text.replace('/logs/artifacts/model.patch', '/app')
    if change == 'dockerfile': (root/'task-one'/'tests'/'Dockerfile').unlink()
    if change == 'name': text = text.replace('datacurve/task-one','datacurve/another')
    if change == 'steps': text += '\n[[steps]]\nname = "step1"\n'
    if change == 'windows': text = text.replace('[environment]', '[environment]\nos = "windows"')
    path.write_text(text, encoding='utf-8')
    with pytest.raises(ValueError): benchmarks.get('deep-swe').task_instances(root)


def test_prepare_binds_separate_images_after_original_dataset_and_pier_are_removed(tmp_path, monkeypatch):
    config, plan, directory, effective, paths, job, frozen = prepared(tmp_path)
    shutil.rmtree(config['environment']['data']); shutil.rmtree(tmp_path/'pier'); shutil.rmtree(tmp_path/'dependencies')
    credentials = tmp_path/'auth.json'; credentials.write_text('synthetic fixture credential', encoding='utf-8')
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE', str(credentials))
    monkeypatch.setattr(harbor_driver.subprocess, 'run', lambda *a,**kw:SimpleNamespace(stdout='codex-cli fixture-version\n'))
    monkeypatch.setattr(harbor_driver, 'docker', lambda *a:pytest.fail('prepare contacted Docker'))
    folder = directory/'attempt'; folder.mkdir()
    request, auth = harbor_driver.prepare(frozen, job['method'], effective, job, folder, 'plan-job')
    assert request['framework'] == 'pier' and request['grading_image'] == GRADER
    assert request['images'] == {'main':IMAGE} and 'auth' not in json.dumps(request)
    assert request['task'].startswith(str(directory/'inputs'/'task-data'))
    cfg = trial_config(request, lambda **kw:kw, tmp_path/'channel')
    mounts = cfg['environment']['mounts']
    assert all(volume['target'] != '/logs/verifier' and '/tests' not in volume['source'] for volume in mounts)
    assert cfg['agent']['import_path'] == 'ctxpress.harness.pier_trial:CtxpressCodex'


@pytest.mark.parametrize('change', ['source','metadata','linux','grading','same-image','services','release','gpu'])
def test_plan_has_concrete_missing_requirements(tmp_path,change):
    _,plan,_,config,_,job,_ = prepared(tmp_path)
    lock = copy.deepcopy(plan['task_resources']); found = copy.deepcopy(job['task'])
    if change == 'source': lock['trees']['pier']['files'].pop('src/pier/trial/trial.py')
    if change == 'metadata': lock['trees']['dependencies']['files'] = {}
    if change == 'linux': lock['runtime']['platform'] = 'win32'
    if change == 'grading': lock['tasks'][found['id']]['images']['grading'] = {'other':{'id':GRADER}}
    if change == 'same-image': lock['tasks'][found['id']]['images']['grading']['verifier']['id'] = IMAGE
    if change == 'services': lock['tasks'][found['id']]['images']['services'] = {'db':{'id':GRADER}}
    if change == 'release': lock['release'] = 'other'
    if change == 'gpu': found['initial_state']['verifier_environment']['gpus'] = 1
    assert deep_swe.requirements(config,[found],lock)


@pytest.mark.parametrize('reward,resolved,invalid', [(0,False,False),(1,True,False),(-1,None,True),(0.5,None,None)])
def test_binary_reward_semantics_and_author_crash_sentinel(tmp_path,reward,resolved,invalid):
    root = data(tmp_path); adapter = benchmarks.get('deep-swe'); found = adapter.task_instances(root)[0]
    report = tmp_path/'result.json'
    rewards = dict(reward=reward,f2p=0.75,p2p=1.0,f2p_total=4,f2p_passed=3)
    report.write_text(json.dumps(dict(task_name='datacurve/task-one',verifier_result={'rewards':rewards},exception_info=None)), encoding='utf-8')
    verifier = tmp_path/'verifier'; verifier.mkdir(); (verifier/'ctrf.json').write_text('{"results":{"tests":[]}}', encoding='utf-8')
    grade = adapter.read_grade(found,report)
    assert grade['resolved'] is resolved and grade['infra_invalid'] is invalid
    assert grade['rewards'] == rewards and 'ctrf.json' in grade['official_artifacts']
    report.write_text(json.dumps(dict(task_name='datacurve/other',verifier_result={'rewards':rewards})), encoding='utf-8')
    assert not adapter.read_grade(found,report).get('valid_rewards')


def test_transfer_filters_unrelated_artifacts_and_retains_original_patch_and_empty_submission(tmp_path):
    root = tmp_path/'original'; root.mkdir(); (root/'model.patch').write_text('committed patch\n', encoding='utf-8')
    (root/'unrelated.txt').write_text('agent-created file', encoding='utf-8')
    target = submission(root,tmp_path/'submission')
    assert sorted(path.name for path in target.iterdir()) == ['model.patch']
    assert (root/'unrelated.txt').exists()
    (root/'model.patch').unlink()
    assert not list(submission(root,tmp_path/'empty').iterdir())


def test_pier_bridge_preserves_prompt_hooks_and_raises_official_agent_failure(tmp_path):
    from test_harbor_codex import OfficialFixture, EnvironmentFixture, settings
    class Agent(OfficialFixture):
        def name(self): return 'codex'
        def render_instruction(self,instruction): return 'official-rendered: ' + instruction
    class Environment(EnvironmentFixture):
        async def exec(self,command,**kwargs):
            if 'ctxpress.harness.agent_process' in command and '--stop' not in command:
                self.commands.append((command,kwargs))
                return SimpleNamespace(return_code=4)
            return await super().exec(command,**kwargs)
    class AgentError(RuntimeError): pass
    credential_states = []
    cls = pier_codex.framework(Agent,SimpleNamespace,settings(),AgentError,credential_states.append)
    agent = cls(); agent.logs_dir = tmp_path/'logs'
    env = Environment(); asyncio.run(agent.setup(env))
    assert agent.install_spec().steps == [dict(run='/cxbin/codex --version',user='agent')]
    with pytest.raises(AgentError): asyncio.run(agent.run('task instruction',env,{}))
    commands = '\n'.join(command for command,kw in env.commands)
    assert 'official-rendered: task instruction' in commands and 'register fixture MCP' in commands
    assert agent.ctxpress_credentials_cleaned and credential_states == [True,False]


def test_standard_harbor_plan_rejects_new_collect_or_separate_protocol(tmp_path):
    from test_harbor_driver import prepared as harbor_prepared
    _,plan,_,config,_,job,_ = harbor_prepared(tmp_path)
    found = copy.deepcopy(job['task']); found['evaluation']['verifier']['environment_mode'] = 'separate'
    assert any('separate verifier' in item for item in harbor_driver.requirements(config,[found],plan['task_resources']))


@pytest.mark.parametrize('cleanup_failure', [False,True])
def test_pier_worker_uses_fresh_grader_after_collect_and_checked_agent_stop(tmp_path,monkeypatch,cleanup_failure):
    from ctxpress.harness import pier_trial
    from ctxpress.harness import eval_environment
    from test_harbor_codex import OfficialFixture, EnvironmentFixture
    events = []
    root = data(tmp_path)/'task-one'
    trial_root = tmp_path/PROJECT
    class Agent(OfficialFixture):
        def __init__(self,logs_dir,**kwargs):
            super().__init__(**kwargs); self.logs_dir = logs_dir
        def name(self): return 'codex'
        def render_instruction(self,instruction): return instruction
    class Docker(EnvironmentFixture):
        def __init__(self,**kw):
            super().__init__(); self.session_id = kw['session_id']; self.trial_paths = kw['trial_paths']
            self.environment_dir = kw['environment_dir']; self.task_env_config = kw['task_env_config']
            self._env_vars = SimpleNamespace(prebuilt_image_name=IMAGE); self._mounts_json = kw.get('mounts_json')
        @property
        def _docker_compose_paths(self): return []
        def _write_mounts_compose_file(self):
            path = self.trial_paths.trial_dir/'mounts.json'; path.write_text('{}', encoding='utf-8'); return path
        def _write_resources_compose_file(self):
            events.append(('resource-limits',self.ctxpress_role)); return root/'task.toml'
        def _cleanup_resources_compose_file(self): pass
        async def _chown_to_host_user(self,*args,**kw): pass
        async def exec(self,command,**kw):
            if command.startswith('sha256sum -- '):
                import shlex
                name = shlex.split(command)[-1].removeprefix('/tests/')
                events.append(('verifier-input-check',name))
                return SimpleNamespace(return_code=0,stdout=eval_plan.file_sha256(root/'tests'/name)+'  /tests/'+name)
            return await super().exec(command,**kw)
    def environment(role,session_id,mounts):
        env = pier_trial.CtxpressDocker(environment_dir=root/('environment' if role=='agent' else 'tests'),
            environment_name='datacurve/task-one',session_id=session_id,trial_paths=SimpleNamespace(trial_dir=trial_root),
            task_env_config=SimpleNamespace(docker_image=IMAGE,gpus=0),mounts_json=mounts)
        async def compose(args,**kw):
            if args[0]=='config':
                return SimpleNamespace(stdout=json.dumps(dict(services={'main':{'image':IMAGE}})))
            events.append((args[0],role))
            if cleanup_failure and role=='agent' and args[0]=='down': raise RuntimeError('cleanup failed')
            return SimpleNamespace(return_code=0)
        env._run_docker_compose_command = compose
        return env
    class Trial:
        def __init__(self,config):
            self.config=config; self._task=SimpleNamespace(config=SimpleNamespace())
            for name in ('agent','verifier','artifacts'):
                (trial_root/name).mkdir(parents=True,exist_ok=True)
            self._agent=pier_trial.CtxpressCodex(logs_dir=trial_root/'agent',model_name='fixture-model')
            self._environment=environment('agent',PROJECT,config['environment']['mounts'])
        @classmethod
        async def create(cls,config): return cls(config)
        async def _verify_with_separate_environment(self,env_config,**kwargs):
            events.append('official-separate-verifier')
            grader=environment('verifier',PROJECT+'__verifier__trial',
                [dict(type='bind',source=str(trial_root/'verifier'),target='/logs/verifier')])
            try:
                await grader.start()
                assert sorted(path.name for path in kwargs['artifacts_dir'].iterdir())==['model.patch']
                events.append('author-grade')
                (trial_root/'verifier'/'reward.json').write_text('{"reward":1}', encoding='utf-8')
            finally:
                await grader.stop()
        async def run(self):
            await self._environment.start(); await self._agent.setup(self._environment)
            events.append('agent')
            await self._agent.run('official instruction',self._environment,{})
            events.append('author-collect')
            (trial_root/'artifacts'/'model.patch').write_text('committed author patch\n', encoding='utf-8')
            (trial_root/'artifacts'/'other.txt').write_text('excluded submission artifact', encoding='utf-8')
            try:
                await self._environment.stop(delete=False)
            except RuntimeError:
                pass  # The actual official Trial records this error; guard must still block grading.
            await self._verify_with_separate_environment(None,artifacts_dir=trial_root/'artifacts')
            return SimpleNamespace(exception_info=None)
    request=dict(project=PROJECT,label='plan-job',images={'main':IMAGE},grading_image=GRADER,
        folder=str(tmp_path),package=str(tmp_path/'package'),bindir=str(tmp_path/'bin'),profiles=str(tmp_path/'profiles'),
        task=str(root),method={'class':'NoCompaction'},model='fixture-model',reasoning='medium',
        binary_version='0.159.0-alpha.12.1',compact_limit=230000,upstream='https://provider.invalid/v1',
        run={'max_calls':2,'timeout':99,'grade':True})
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE','/runtime-only/auth.json')
    monkeypatch.setattr(eval_environment,'image',lambda name:{'id':name})
    monkeypatch.setattr(asyncio.BaseEventLoop,'add_signal_handler',lambda *a:None)
    monkeypatch.setattr(asyncio.BaseEventLoop,'remove_signal_handler',lambda *a:None)
    official=(Agent,SimpleNamespace,RuntimeError,Docker,SimpleNamespace,lambda **kw:kw,Trial,
              lambda config:SimpleNamespace(value='separate'))
    execution=pier_trial.run_trial(request,official,tmp_path/'channel',dict(daemon_id='fixture-daemon'))
    if cleanup_failure:
        with pytest.raises(RuntimeError,match='cleanup'): asyncio.run(execution)
        assert 'author-grade' not in events and ('up','verifier') not in events
        return
    asyncio.run(execution)
    assert events.index('author-collect') < events.index(('down','agent')) < events.index(('up','verifier')) < events.index('author-grade')
    assert ('resource-limits','agent') in events and ('resource-limits','verifier') in events
    assert any(isinstance(event,tuple) and event[0]=='verifier-input-check' for event in events)
    records=[json.loads(path.read_text(encoding='utf-8')) for path in tmp_path.glob('resources-harbor-*.json')]
    assert len(records)==2 and all(record['cleaned'] and record['credentials_may_exist'] is False for record in records)
    assert {record['images']['main'] for record in records}=={IMAGE,GRADER}
    result=json.loads((tmp_path/'harbor-worker-result.json').read_text(encoding='utf-8'))
    assert result['separate_verifier']['checked_cleanup'] and 'artifacts/model.patch' in result['official_artifacts']
    assert (trial_root/'ctxpress-compose-agent.json').is_file() and (trial_root/'ctxpress-compose-verifier.json').is_file()


def test_report_preserves_separate_verifier_and_artifact_evidence(tmp_path):
    from ctxpress.harness.eval_report import write_report
    _,plan,directory,_,_,job,_ = prepared(tmp_path)
    separate=dict(agent_image=IMAGE,verifier_image=GRADER,checked_cleanup=True)
    evidence={'verifier/reward.json':{'path':'fixture','sha256':'a'*64}}
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET status='running',attempt=1 WHERE id=?", (job['id'],))
    evaluation.set_result(directory,job['id'],1,result=dict(test_only=True,protocol='ctxpress_comparison',
        real_run_verified=False,separate_verifier=separate,official_artifacts=evidence))
    write_report(directory)
    result=json.loads((directory/'report.json').read_text(encoding='utf-8')); actual=result['methods'][0]['jobs'][0]
    assert actual['separate_verifier']==separate and actual['official_artifacts']==evidence
    html=(directory/'report.html').read_text(encoding='utf-8')
    assert '独立官方评分环境' in html and 'ctxpress_comparison' in html and GRADER in html


def test_grader_recovery_has_no_credential_restart_or_model_channel(tmp_path,monkeypatch):
    from test_harbor_driver import journal,container,docker_fixture
    path,record=journal(tmp_path)
    record.update(role='verifier',channel=None,credentials_may_exist=False,images={'main':GRADER})
    eval_plan.atomic_json(path,record)
    value=container(record);value['Image']=GRADER
    calls=[];monkeypatch.setattr(harbor_driver,'docker',docker_fixture(record,value,calls))
    harbor_driver.recover(path,'plan-job')
    assert json.loads(path.read_text(encoding='utf-8'))['cleaned']
    assert calls[-1][0]=='rm' and not any(call[0] in ('exec','start') for call in calls)
    calls.clear();harbor_driver.recover(path,'plan-job');assert not calls
