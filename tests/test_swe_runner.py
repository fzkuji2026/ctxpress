"""SWE-bench dispatch/ownership contracts; no official scores, models or Docker."""
import asyncio, copy, json, shutil, subprocess, sys, threading
from pathlib import Path
from types import SimpleNamespace, ModuleType
import pytest
from ctxpress import benchmarks
from ctxpress.benchmarks import swe_driver, swe_protocol
from ctxpress.harness import eval_environment, eval_inputs, eval_plan, eval_trees, evaluation, task, task_resources
from ctxpress.harness import swe_containers, swe_trial

AGENT='sha256:'+'a'*64
GRADER='sha256:'+'c'*64
PROJECT='ctxp-sw-'+'b'*24
COMMIT='d'*40


def prepared(tmp_path, mode='prepared', variant='swe-bench-verified'):
    data=tmp_path/'data';data.mkdir()
    row=dict(instance_id='owner__repo-1',repo='owner/repo',base_commit=COMMIT,problem_statement='Repair the parser.',
        patch='private reference patch',test_patch='private author test patch',version='1',
        FAIL_TO_PASS=['tests.test_parser'],PASS_TO_PASS=[],image='prepared:instance',
        eval_script='author test script',log_parser='author parser',eval_type='unit')
    (data/'instances.jsonl').write_text(json.dumps(row)+'\n', encoding='utf-8')
    adapter=benchmarks.get(variant)
    (data/'dataset_manifest.json').write_text(json.dumps(dict(dataset=adapter.describe()['suite'],revision='fixture-v1')), encoding='utf-8')
    found=adapter.task_instances(data)[0]
    binary=tmp_path/'bin';binary.mkdir();(binary/'codex').write_bytes(b'fixture CLI')
    names=['pyproject.toml','swebench.egg-info/PKG-INFO','swebench/__init__.py','swebench/harness/run_evaluation.py',
           'swebench/harness/grading.py','swebench/harness/docker_utils.py']
    names+=['swebench/harness/test_spec.py'] if mode=='legacy' else ['swebench/types.py','swebench/image_builder/constants.py']
    definitions={'swebench':names,'dependencies':['swebench-2.1.0.dist-info/METADATA']}
    if mode=='legacy':definitions['test_specs']=[found['id']+'.json']
    trees={}
    for key,files in definitions.items():
        root=tmp_path/key
        for name in files:
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# fixture input\n', encoding='utf-8')
        trees[key]=eval_trees.capture(root,files,folder='unused')['tree']
    runtime=dict(python=str(Path(sys.executable).resolve()),sha256=eval_plan.file_sha256(sys.executable),version=sys.version,platform='linux')
    record=dict(task_sha256=task_resources.task_digest(found),images={'agent':{'id':AGENT},'grading':{'verifier':{'id':GRADER}}})
    if mode=='prepared':record['verifier_cap_add']=['SYS_ADMIN']
    lock=task_resources.seal(dict(schema=task_resources.SCHEMA,version=1,benchmark=variant,release='fixture-v1',
        tasks={found['id']:record},trees=trees,runtime=runtime))
    resource=tmp_path/'resources.json';resource.write_text(json.dumps(lock), encoding='utf-8')
    cfg=dict(schema='ctxpress.eval',version=1,benchmark=variant,start_mode='task_start',scope='benchmark',model='fixture-model',
        backend='codex_docker',tasks=[found['id']],environment=dict(data=str(data),bindir=str(binary),resources=str(resource)),
        methods=[{'class':'NoCompaction'}],run={'grading_timeout':23})
    plan=eval_plan.compile_plan(cfg);assert plan['missing_environment_files']==[]
    directory=evaluation.prepare(plan,tmp_path/'run');effective,paths=eval_inputs.execution(plan,directory/'inputs');job=plan['jobs'][0]
    return cfg,plan,directory,effective,job,task.remap(job['task'],paths)


@pytest.mark.parametrize('mode',['legacy','prepared'])
@pytest.mark.parametrize('variant',['swe-bench','swe-bench-lite','swe-bench-verified'])
def test_each_variant_dispatches_only_frozen_inputs_after_originals_are_removed(tmp_path,monkeypatch,mode,variant):
    cfg,plan,directory,effective,job,frozen=prepared(tmp_path,mode,variant)
    for name in ('data','swebench','dependencies','test_specs','bin'):
        if (tmp_path/name).exists():shutil.rmtree(tmp_path/name)
    credentials=tmp_path/'auth.json';credentials.write_text('runtime fixture', encoding='utf-8')
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE',str(credentials))
    monkeypatch.setattr(swe_driver.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(stdout='codex-cli fixture-version\n'))
    request,auth=swe_driver.prepare(frozen,job['method'],effective,job,directory/'attempt','plan-job')
    assert request['api']==mode and request['agent_image']==AGENT and request['grading_image']==GRADER
    assert request['task']['initial_state']==dict(repo='owner/repo',base_commit=COMMIT,problem_statement='Repair the parser.')
    assert 'private reference' not in json.dumps(request) and 'private author test' not in json.dumps(request)
    assert 'auth' not in json.dumps(request) and auth==str(credentials.resolve())
    assert request['run']['grading_timeout']==23 and request['project'].startswith('ctxp-sw-')
    assert not plan['benchmark']['real_run_verified']
    if mode=='prepared':assert request['verifier_cap_add']==['SYS_ADMIN']


@pytest.mark.parametrize('change',['source','metadata','runtime','platform','python','api','release','grader','caps','services','legacy-spec'])
def test_planning_lists_missing_protocol_resources_before_a_worker_can_start(tmp_path,change):
    mode='legacy' if change=='legacy-spec' else 'prepared'
    _,plan,_,config,job,_=prepared(tmp_path,mode)
    lock=copy.deepcopy(plan['task_resources']);found=job['task']
    if change=='source':lock['trees']['swebench']['files'].pop('swebench/harness/grading.py')
    if change=='metadata':lock['trees']['dependencies']['files']={}
    if change=='runtime':lock.pop('runtime')
    if change=='platform':lock['runtime']['platform']='win32'
    if change=='python':lock['runtime']['version']='3.9.19'
    if change=='api':lock['trees']['swebench']['files'].pop('swebench/types.py')
    if change=='release':lock['release']='other release'
    if change=='grader':lock['tasks'][found['id']]['images']['grading']={'unknown':{'id':GRADER}}
    if change=='caps':lock['tasks'][found['id']].pop('verifier_cap_add')
    if change=='services':lock['tasks'][found['id']]['images']['services']={'sidecar':{'id':GRADER}}
    if change=='legacy-spec':lock['trees']['test_specs']['files']={}
    assert swe_protocol.requirements(config,[found],lock)


@pytest.mark.parametrize('value',[['NET_ADMIN'],['SYS_ADMIN','SYS_ADMIN'],'SYS_ADMIN',None])
def test_capture_rejects_undeclared_capabilities_before_any_image_inspection(tmp_path,monkeypatch,value):
    _,plan,_,_,job,_=prepared(tmp_path)
    lock=plan['task_resources']
    spec=dict(schema='ctxpress.eval.task_resource_spec',version=1,benchmark=job['task']['benchmark'],release=lock['release'],
        tasks={job['task']['id']:dict(agent_image=AGENT,grading_images={'verifier':GRADER},verifier_cap_add=value)},
        trees={key:dict(root=tree['root'],files=list(tree['files'])) for key,tree in lock['trees'].items()})
    monkeypatch.setattr(eval_environment,'image',lambda *args:pytest.fail('inspected image with invalid capability declaration'))
    with pytest.raises(ValueError,match='capabilities'):task_resources.capture(spec,[job['task']])


class NotFound(Exception):pass


def request(tmp_path,mode='prepared'):
    return dict(project=PROJECT,label='plan-job',folder=str(tmp_path),agent_image=AGENT,grading_image=GRADER,
        _not_found=NotFound,api=mode,verifier_cap_add=['SYS_ADMIN'] if mode=='prepared' else [],
        task={'id':'owner__repo-1','initial_state':{'base_commit':COMMIT,'problem_statement':'Repair.'}},
        model='fixture/model',package=str(tmp_path/'package'),bindir=str(tmp_path/'bin'),profiles=str(tmp_path/'profiles'),
        run={'grade':True,'timeout':2,'max_calls':3,'grading_timeout':23})


class Container:
    def __init__(self,client,options):
        self.client,self.options=client,options;self.id='fixture-'+options['name']
        self.attrs={'Config':{'Labels':options['labels']},'Image':options['image'],'Name':'/'+options['name']}
    def reload(self):pass
    def start(self):self.client.events.append(('start',self.options['name']))
    def remove(self,**kwargs):
        self.client.events.append(('remove',self.options['name']));self.client.live.pop(self.options['name'])
    def exec_run(self,args,**kwargs):
        self.client.events.append(('exec',args[-1]))
        output=(self.client.commit+'\n').encode() if args[-1]=='git rev-parse HEAD' else b''
        return SimpleNamespace(exit_code=0,output=output)


class Client:
    _ctxpress_not_found=NotFound
    def __init__(self):
        self.events=[];self.live={};self.commit=COMMIT
        self.images=SimpleNamespace(get=lambda identity:SimpleNamespace(id=identity,attrs={'Os':'linux'}))
        self.containers=SimpleNamespace(create=self.create,get=self.get)
    def create(self,**options):
        self.events.append(('create',options));value=Container(self,options);self.live[options['name']]=value;return value
    def get(self,name):
        if name not in self.live:raise NotFound(name)
        return self.live[name]


@pytest.mark.parametrize('mode',['legacy','prepared'])
def test_author_grader_owns_application_and_scoring_in_a_clean_separate_container(tmp_path,monkeypatch,mode):
    req=request(tmp_path,mode);client=Client();agent=swe_containers.Owner(req,client,'agent','daemon');agent.cleanup()
    grader=swe_containers.Owner(req,client,'verifier','daemon')
    spec=SimpleNamespace(repo='owner/repo',version='1',platform='linux/amd64')
    if mode=='legacy':
        fake=ModuleType('types');fake.MAP_REPO_VERSION_TO_SPECS={'owner/repo':{'1':{'execute_test_as_nonroot':True,'nano_cpus':2000000000}}}
        # SimpleNamespace is defined in types; inject only the official mapping fixture.
        monkeypatch.setattr(sys.modules['types'],'MAP_REPO_VERSION_TO_SPECS',fake.MAP_REPO_VERSION_TO_SPECS,raising=False)
    module=SimpleNamespace(RUN_EVALUATION_LOG_DIR=tmp_path/'old',CONTAINER_USER='author-user',
        create_container=lambda *a,**k:pytest.fail('original grader tried to pull'),
        build_container=lambda *a,**k:pytest.fail('original grader tried to build'),cleanup_container=lambda *a:None)
    field='build_container' if mode=='legacy' else 'create_container';old=getattr(module,field);old_cleanup=module.cleanup_container
    prediction=dict(instance_id=req['task']['id'],model_name_or_path=req['model'],model_patch='diff --git fixture')
    def run_instance(test_spec,pred,client,run_id,timeout,rm_image=False,force_rebuild=False,rewrite_reports=False,skip_patch=False):
        assert test_spec is spec and pred is prediction and timeout==23
        assert not any((rm_image,force_rebuild,rewrite_reports,skip_patch))
        container=getattr(module,field)(test_spec=test_spec,client=client,run_id=run_id,logger=None)
        container.start();client.events.append(('author-apply-and-score',pred['model_patch']))
        report=module.RUN_EVALUATION_LOG_DIR/run_id/req['model'].replace('/','__')/req['task']['id']/'report.json'
        report.parent.mkdir(parents=True);report.write_text(json.dumps({req['task']['id']:{'resolved':False}}), encoding='utf-8')
        module.cleanup_container(client=client,container=container,logger=None)
    module.run_instance=run_instance
    report=swe_trial.grade(req,module,client,spec,grader,agent,prediction)
    assert json.loads(report.read_text(encoding='utf-8'))[req['task']['id']]['resolved'] is False
    assert not client.live and grader.record['cleaned']
    assert getattr(module,field) is old and module.cleanup_container is old_cleanup and module.RUN_EVALUATION_LOG_DIR==tmp_path/'old'
    options=next(event[1] for event in client.events if event[0]=='create')
    assert options['image']==GRADER and options['network_mode']=='none' and 'volumes' not in options
    assert options['environment']['NVIDIA_VISIBLE_DEVICES']=='void'
    if mode=='legacy':assert options['user']=='nonroot' and options['nano_cpus']==2000000000 and options['platform']=='linux/amd64'
    else:assert options['user']=='author-user' and options['cap_add']==['SYS_ADMIN']
    operations=[event[0] for event in client.events];assert operations.index('exec')<operations.index('author-apply-and-score')<operations.index('remove')
    with pytest.raises(ValueError,match='fresh'):swe_trial.grade(req,module,client,spec,grader,agent,prediction)


def test_agent_base_commit_checked_before_auth_and_no_dataset_or_official_tree_is_mounted(tmp_path):
    req=request(tmp_path);client=Client();client.commit='e'*40
    owner=swe_containers.Owner(req,client,'agent','daemon',tmp_path/'channel')
    environment=swe_containers.AgentEnvironment(owner,tmp_path/'channel')
    with pytest.raises(ValueError,match='base_commit'):asyncio.run(environment.start())
    assert not owner.record['credentials_may_exist']
    volumes=owner.container.options['volumes']
    assert req['package'] not in volumes
    assert volumes[str(Path(req['package'])/'ctxpress')]['bind']=='/ctxpress-runtime/ctxpress'
    assert all('tests' not in key and 'data' not in key and 'official' not in key for key in volumes)
    owner.cleanup();assert not client.live


def test_uncertain_creation_is_recovered_by_exact_name_and_credentials_block_deletion(tmp_path):
    req=request(tmp_path);client=Client();owner=swe_containers.Owner(req,client,'agent','daemon')
    owner.create();owner.container=None;owner.credentials(True)
    with pytest.raises(RuntimeError,match='credentials'):owner.cleanup()
    assert client.live and not owner.record['cleaned']
    owner.credentials(False);owner.cleanup();assert not client.live and owner.record['cleaned']


def test_grading_cannot_begin_with_agent_credentials_or_live_agent(tmp_path):
    req=request(tmp_path);client=Client();agent=swe_containers.Owner(req,client,'agent','daemon')
    grader=swe_containers.Owner(req,client,'verifier','daemon')
    with pytest.raises(RuntimeError,match='Agent cleanup'):swe_trial.grade(req,SimpleNamespace(),client,None,grader,agent,{})
    assert not client.events


@pytest.mark.parametrize('change',['label','image','name','id','daemon','live-worker'])
def test_recovery_checks_all_ownership_before_deleting_anything(tmp_path,monkeypatch,change):
    req=request(tmp_path);client=Client();owner=swe_containers.Owner(req,client,'verifier','daemon');container=owner.create()
    record=dict(owner.record,pid=None);eval_plan.atomic_json(owner.path,record)
    value=dict(container.attrs,Id=container.id);value['Config']=copy.deepcopy(value['Config']);calls=[]
    if change=='label':value['Config']['Labels']['ctxpress.run']='another'
    if change=='image':value['Image']=AGENT
    if change=='name':value['Name']='/another'
    if change=='id':value['Id']='another'
    if change=='live-worker':record['pid']=123;eval_plan.atomic_json(owner.path,record)
    monkeypatch.setattr(swe_driver.processes,'alive',lambda *args:True)
    def docker(*args):
        calls.append(args)
        if args[0]=='info':return 'changed' if change=='daemon' else 'daemon'
        if args[0]=='ps':return container.id
        if args[:2]==('container','inspect'):return json.dumps([value])
        pytest.fail('mutated resources before identity verification')
    monkeypatch.setattr(swe_driver.harbor_driver,'docker',docker)
    with pytest.raises(ValueError):swe_driver.recover(owner.path,req['label'])
    assert not json.loads(owner.path.read_text(encoding='utf-8'))['cleaned']


def test_patch_capture_includes_committed_staged_untracked_and_binary_edits(tmp_path):
    repo=tmp_path/'repo';repo.mkdir()
    def git(*args):return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,text=True).stdout
    git('init');git('config','user.email','fixture@example.invalid');git('config','user.name','Fixture');git('config','core.autocrlf','false')
    (repo/'committed.txt').write_text('old\n', encoding='utf-8');(repo/'staged.txt').write_text('old\n', encoding='utf-8');git('add','.');git('commit','-m','base')
    commit=git('rev-parse','HEAD').strip();(repo/'committed.txt').write_text('fixed\n', encoding='utf-8');git('add','committed.txt');git('commit','-m','agent')
    (repo/'staged.txt').write_text('staged fix\n', encoding='utf-8');git('add','staged.txt');(repo/'new.txt').write_text('new fix\n', encoding='utf-8');(repo/'binary.bin').write_bytes(b'\x00\xffbinary')
    env=swe_containers.AgentEnvironment(SimpleNamespace(request={'task':{'initial_state':{'base_commit':commit}}}),None)
    async def execute(command,**kwargs):
        # Execute the exact patch command in a real local Git repository.
        result=subprocess.run(command,shell=True,cwd=repo,capture_output=True,text=True)
        return SimpleNamespace(return_code=result.returncode,stdout=result.stdout,stderr=result.stderr)
    env.exec=execute;patch=asyncio.run(env.patch())
    assert '+fixed' in patch and '+staged fix' in patch and '+new fix' in patch and 'GIT binary patch' in patch
    clean=tmp_path/'clean';git('clone',str(repo),str(clean))
    subprocess.run(['git','-C',str(clean),'reset','--hard',commit],check=True,capture_output=True)
    subprocess.run(['git','-C',str(clean),'apply','--binary','-'],input=patch.encode(),check=True,capture_output=True)
    assert (clean/'binary.bin').read_bytes()==b'\x00\xffbinary' and (clean/'committed.txt').read_text(encoding='utf-8')=='fixed\n'


@pytest.mark.parametrize('mode',['completed','timeout','budget','agent_error','no-grade','cleanup-failure'])
def test_trial_preserves_partial_predictions_and_grades_only_after_checked_cleanup(tmp_path,monkeypatch,mode):
    req=request(tmp_path);req['run']['timeout']=0.01
    if mode=='no-grade':req['run']['grade']=False
    client=Client();events=[]
    class Environment:
        def __init__(self,owner,channel):self.owner=owner
        async def start(self):self.owner.create();events.append('start')
        async def patch(self):events.append('capture');return '' if mode=='agent_error' else 'partial fixture patch\n'
    def agent_class(request,credentials):
        class Agent:
            ctxpress_stop=None
            async def setup(self,environment):credentials(True);events.append('setup')
            async def run(self,*args):
                events.append('agent')
                if mode=='timeout':await asyncio.sleep(1)
                if mode in ('budget','agent_error'):
                    if mode=='budget':self.ctxpress_stop='max_calls'
                    raise swe_trial.AgentExitError('fixture')
            async def cleanup_credentials(self,*args):
                if mode=='cleanup-failure':raise RuntimeError('fixture cleanup failed')
                credentials(False);events.append('credentials-removed')
        return Agent
    def grading(request,module,client,spec,owner,agent_owner,prediction):
        assert agent_owner.record['cleaned'] and not agent_owner.record['credentials_may_exist']
        assert not client.live
        events.append('official-grading');owner.cleanup()
        # Missing author report remains ungraded; do not invent a score.
        return tmp_path/'absent-report.json'
    monkeypatch.setattr(swe_trial,'AgentEnvironment',Environment);monkeypatch.setattr(swe_trial,'agent_class',agent_class)
    monkeypatch.setattr(swe_trial,'grade',grading)
    execution=swe_trial.run(req,None,client,None,tmp_path/'channel','daemon')
    if mode=='cleanup-failure':
        with pytest.raises(RuntimeError,match='cleanup failed'):asyncio.run(execution)
        assert 'official-grading' not in events and client.live
        assert not (tmp_path/'swe-worker-result.json').exists()
        return
    asyncio.run(execution)
    prediction=json.loads((tmp_path/'predictions.jsonl').read_text(encoding='utf-8'))
    assert prediction['model_patch']==('' if mode=='agent_error' else 'partial fixture patch\n')
    state=json.loads((tmp_path/'swe-worker-result.json').read_text(encoding='utf-8'))
    expected={'timeout':'timeout','budget':'max_calls','agent_error':'agent_error'}.get(mode,'completed')
    assert state['stop']==expected and state['calls']==0 and not client.live
    if mode=='no-grade':assert 'official-grading' not in events and not state['separate_verifier']['author_grading']
    else:assert events.index('credentials-removed')<events.index('official-grading')


@pytest.mark.parametrize('change',['instance','script','tests'])
def test_legacy_spec_is_restored_and_validated_without_calling_network_setup(tmp_path,monkeypatch,change):
    _,_,_,config,job,found=prepared(tmp_path,'legacy');root=Path(config['environment']['official_root'])
    row=swe_protocol.instance(found)
    captured=dict(instance_id=found['id'],repo=row['repo'],version=row['version'],FAIL_TO_PASS=row['FAIL_TO_PASS'],
        PASS_TO_PASS=[],eval_script_list=['author eval script'])
    if change=='instance':captured['instance_id']='other__repo-1'
    if change=='script':captured['eval_script_list']=['changed script']
    if change=='tests':captured['FAIL_TO_PASS']=['different test']
    (root/'test_specs'/(found['id']+'.json')).write_text(json.dumps(captured), encoding='utf-8')
    module=ModuleType('swebench.harness.test_spec');module.TestSpec=lambda **kwargs:SimpleNamespace(**kwargs)
    module.make_test_spec=lambda *args:pytest.fail('called environment setup builder')
    module.make_eval_script_list=lambda *args,**kwargs:['author eval script']
    module.MAP_REPO_VERSION_TO_SPECS={row['repo']:{row['version']:{}}}
    monkeypatch.setitem(sys.modules,'swebench.harness.test_spec',module)
    with pytest.raises(ValueError,match='TestSpec'):swe_protocol.prepared_spec(found,root,'legacy',None)


def test_cancelled_resource_mutation_finishes_before_cleanup_can_begin():
    began=threading.Event();release=threading.Event();events=[]
    def upload():
        began.set();assert release.wait(5);events.append('upload-finished')
    async def run():
        operation=asyncio.create_task(swe_containers.completed_thread(upload))
        assert await asyncio.to_thread(began.wait,5)
        operation.cancel();await asyncio.sleep(0)
        assert not operation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await operation
        events.append('cleanup')
    try:asyncio.run(run())
    finally:release.set()
    assert events==['upload-finished','cleanup']


def test_recovery_deletes_credentials_before_container_and_stops_if_deletion_fails(tmp_path,monkeypatch):
    req=request(tmp_path);client=Client();owner=swe_containers.Owner(req,client,'agent','daemon')
    container=owner.create();record=dict(owner.record,pid=None,credentials_may_exist=True)
    eval_plan.atomic_json(owner.path,record);calls=[]
    value=dict(container.attrs,Id=container.id)
    def docker(*args):
        calls.append(args)
        if args[0]=='info':return 'daemon'
        if args[0]=='ps':return container.id
        if args[:2]==('container','inspect'):return json.dumps([value])
        if args[0]=='exec':raise RuntimeError('checked credentials removal failed')
        return ''
    monkeypatch.setattr(swe_driver.harbor_driver,'docker',docker)
    with pytest.raises(RuntimeError,match='credentials'):swe_driver.recover(owner.path,req['label'])
    assert not any(args[0]=='rm' for args in calls)
    assert not json.loads(owner.path.read_text(encoding='utf-8'))['cleaned']


def test_common_json_and_html_reports_keep_swe_protocol_and_official_outcome(tmp_path):
    from ctxpress.harness import eval_report
    _,_,directory,_,job,_=prepared(tmp_path)
    result=dict(test_only=True,protocol='ctxpress_comparison',swe_api='prepared',grading_run_id=PROJECT,
        real_run_verified=False,separate_verifier={'agent_image':AGENT,'verifier_image':GRADER,'checked_cleanup':True},
        official_artifacts={'model.patch':{'sha256':'f'*64}},grade={'resolved':False,'infra_invalid':False})
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET status='completed',attempt=1,result=? WHERE id=?",(json.dumps(result),job['id']))
    eval_report.write_report(directory);report=eval_report.report(directory);actual=report['methods'][0]['jobs'][0]
    assert actual['swe_api']=='prepared' and actual['grading_run_id']==PROJECT and not actual['real_run_verified']
    assert report['methods'][0]['valid_grades']==1 and report['methods'][0]['resolved']==0
    assert GRADER in (directory/'report.html').read_text(encoding='utf-8')
