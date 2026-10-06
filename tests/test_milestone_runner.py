"""Dispatch assembly and failure recovery with local substitutes; no model/Docker."""
import contextlib,json,os,signal,sys
from pathlib import Path
from types import ModuleType,SimpleNamespace
import pytest
from ctxpress.benchmarks import milestone_driver
from ctxpress.harness import milestone_runner as runner


def fixture(tmp_path,monkeypatch,outcome=True,cleanup_error=False):
    source=tmp_path/'source';source.mkdir();events=[]
    package=ModuleType('harness');package.__path__=[]
    e2e=ModuleType('harness.e2e');e2e.__path__=[]
    author=ModuleType('harness.e2e.run_e2e');version=ModuleType('harness.e2e.data_version')
    version.__file__=str(source/'harness/e2e/data_version.py')
    e2e.run_e2e=author;e2e.data_version=version
    for name,value in [('harness',package),('harness.e2e',e2e),('harness.e2e.run_e2e',author),('harness.e2e.data_version',version)]:
        monkeypatch.setitem(sys.modules,name,value)
    image={'id':'sha256:'+'a'*64,'reference':'fixture:v1.0.2'};proof={'head':'a'*40,'tag':'b'*40,'source_dirty':True,'source_status_sha256':'c'*64}
    lock={'release':'v1.0.2','native_data_version':proof,'tasks':{'repo':{'images':{'agent':image,'grading':{'M1':image}}}}}
    monkeypatch.setattr(runner,'validate',lambda request:lock)
    workspace=tmp_path/'native-inputs/repo';workspace.mkdir(parents=True)
    trial=tmp_path/'trial';trial.mkdir()
    binding=SimpleNamespace(mode='absent',to_metadata=lambda trial:{'mode':'absent'})
    metadata={'repo_src_dirs':['src'],'test_dirs':['tests'],'exclude_patterns':[]}
    monkeypatch.setattr(runner.milestone_native,'prepare',lambda *args:{'workspace':workspace,'trial':trial,
        'metadata':metadata,'config_path':trial/'config.yaml','repo_config_binding':binding,'runtime_policy_binding':binding})
    monkeypatch.setattr(runner.milestone_version,'materialize',lambda *args:events.append('version'))
    version.check_data_version=lambda *args,**kw:{'benchmark_version':'v1.0.2','data_version':{'checked':True,'state':'match','commit':proof['head']}}
    def check_image(reference,**kw):
        assert reference==image['reference']
        return {'state':'match','image':reference}
    version.check_image_tag_consistency=check_image
    auth=tmp_path/'auth.json';auth.write_text('synthetic', encoding='utf-8')
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE',str(auth))
    class Registry:
        def __init__(self,*args):events.append('registry')
        def cleanup(self):
            events.append('cleanup')
            if cleanup_error:raise RuntimeError('retained grader')
    class Channel:
        def __init__(self,*args):pass
        def open(self):events.append('channel');return tmp_path/'socket'
        def cleanup(self):events.append('channel-stop')
    monkeypatch.setattr(runner.milestone_resources,'Registry',Registry)
    monkeypatch.setattr(runner.milestone_transport,'Channel',Channel)
    @contextlib.contextmanager
    def hook(name):
        events.append(name)
        try:yield
        finally:events.append(name+'-restore')
    monkeypatch.setattr(runner.milestone_codex,'installed',lambda settings:hook('codex'))
    monkeypatch.setattr(runner.milestone_containers,'installed',lambda *args:hook('containers'))
    monkeypatch.setattr(runner.milestone_agent,'installed',lambda *args:hook('agent'))
    monkeypatch.setattr(runner.milestone_trial,'installed',lambda *args:hook('trial'))
    class Orchestrator:
        def __init__(self,**kw):
            events.append('orchestrator');assert kw['image_name']==image['id']
            self.container_setup=SimpleNamespace(ctxpress_owner='owned-agent')
            self.config=SimpleNamespace(evaluation_timeout=60);self.build_failure_fail_closed=True
    class Trial:
        def __init__(self,**kw):
            assert kw['remove_container'] is False and kw['force'] is False
            assert (trial/'trial_metadata.json').is_file()
        def run(self):
            events.append('run')
            signal.signal(signal.SIGTERM,lambda *args:None)
            if isinstance(outcome,BaseException):raise outcome
            return outcome
    author.E2EOrchestrator=Orchestrator;author.E2ETrialRunner=Trial
    author.RUNTIME_POLICY_ENV_KEYS=['SWE_MILESTONE_QUARANTINE']
    def activate(binding):
        os.environ.pop('SWE_MILESTONE_UNPROTECTED',None)
        os.environ['SWE_MILESTONE_QUARANTINE']='frozen-policy'
    author._activate_runtime_policy=activate
    author.TRIAL_METADATA_SCHEMA_VERSION_WITH_RUNTIME_POLICY_BINDING=5
    monkeypatch.setattr(runner.milestone_grade,'read',lambda *args:events.append('grade') or {'resolved':None})
    folder=tmp_path/'attempt';folder.mkdir()
    request={'folder':str(folder),'package':str(tmp_path),'task':{'id':'repo'},'execution':{
        'run':{'timeout':10,'max_calls':3,'grade':True},'project':'ctxp-ms-'+'1'*24,'label':'label',
        'upstream':'https://example.invalid/codex','bindir':str(tmp_path),'method':{'class':'NoCompaction'},
        'binary_version':'1.0','compact_limit':10000,'profiles':str(tmp_path),'model':'fixture','reasoning':'low'}}
    return source,request,events


def test_dispatch_keeps_author_order_and_grades_only_after_cleanup(tmp_path,monkeypatch):
    source,request,events=fixture(tmp_path,monkeypatch)
    before=signal.getsignal(signal.SIGTERM)
    monkeypatch.setenv('SWE_MILESTONE_UNPROTECTED','original')
    monkeypatch.setenv('SWE_MILESTONE_QUARANTINE','original-policy')
    result=runner.run(request,source)
    assert events==['version','registry','channel','codex','containers','orchestrator','agent','trial','run',
        'trial-restore','agent-restore','containers-restore','codex-restore','cleanup','channel-stop','grade']
    assert signal.getsignal(signal.SIGTERM)==before
    assert os.environ['SWE_MILESTONE_UNPROTECTED']=='original' and os.environ['SWE_MILESTONE_QUARANTINE']=='original-policy'
    assert result['stop']=='completed' and result['cleanup_complete'] and not result['real_run_verified']
    metadata=json.loads((tmp_path/'trial/trial_metadata.json').read_text(encoding='utf-8'))
    assert metadata['unprotected'] is False
    assert metadata['ctxpress_data_version_evidence']['source_dirty'] is True
    audit=json.loads((Path(request['folder'])/'native-budget.json').read_text(encoding='utf-8'))
    assert not audit['budget_started']


@pytest.mark.parametrize('outcome',[RuntimeError('agent failed'),KeyboardInterrupt(),SystemExit(2)])
def test_aborted_dispatch_restores_hooks_and_retains_failure_evidence(tmp_path,monkeypatch,outcome):
    source,request,events=fixture(tmp_path,monkeypatch,outcome)
    before=signal.getsignal(signal.SIGTERM)
    with pytest.raises(type(outcome)):runner.run(request,source)
    assert events[-2:]==['cleanup','channel-stop'] and 'grade' not in events
    assert signal.getsignal(signal.SIGTERM)==before
    state=json.loads((Path(request['folder'])/'native-execution.json').read_text(encoding='utf-8'))
    assert state['stop']=='trial_error' and state['error_type']==type(outcome).__name__


def test_cleanup_failure_cannot_produce_a_grade(tmp_path,monkeypatch):
    source,request,events=fixture(tmp_path,monkeypatch,cleanup_error=True)
    with pytest.raises(RuntimeError,match='journals retained'):runner.run(request,source)
    assert 'grade' not in events
    state=json.loads((Path(request['folder'])/'native-execution.json').read_text(encoding='utf-8'))
    assert not state['cleanup_complete']


def test_collector_failure_is_not_reported_as_completed(tmp_path,monkeypatch):
    source,request,events=fixture(tmp_path,monkeypatch)
    def fail(*args):raise ValueError('invalid scoring evidence')
    monkeypatch.setattr(runner.milestone_grade,'read',fail)
    with pytest.raises(ValueError,match='scoring evidence'):runner.run(request,source)
    state=json.loads((Path(request['folder'])/'native-execution.json').read_text(encoding='utf-8'))
    assert state['cleanup_complete'] and state['stop']=='grading_error'
    assert not (Path(request['folder'])/'native-grade.json').exists()


def test_recovery_orders_credentials_before_network_and_channel(tmp_path,monkeypatch):
    names=['resources-milestone-channel.json','resources-milestone-network.json',
        'resources-milestone-ctxp-ms-x-eval-abc.json','resources-milestone-ctxp-ms-x-agent.json',
        'resources-milestone-services-'+ 'a'*24+'.json']
    for name in names:(tmp_path/name).write_text('{}', encoding='utf-8')
    observed=[];monkeypatch.setattr(milestone_driver,'recover',lambda path,label:observed.append(path.name))
    milestone_driver.recover_attempt(tmp_path,'owner')
    assert observed==[names[3],names[2],names[4],names[1],names[0]]


def test_version_proof_is_required_before_binary_or_authentication(tmp_path,monkeypatch):
    monkeypatch.setattr(milestone_driver,'request',lambda *args:{'resources':'fixture'})
    monkeypatch.setattr(milestone_driver.task_resources,'read',lambda *args:({'tasks':{}},None))
    monkeypatch.setattr(milestone_driver.subprocess,'run',lambda *args,**kw:pytest.fail('binary invoked before version evidence'))
    with pytest.raises(ValueError,match='native_data_version'):
        milestone_driver.prepare_execution({}, {},{}, {'task':{}},tmp_path,'owner')


@pytest.mark.linux_only
@pytest.mark.parametrize('outcome',['success','failure','cancel'])
def test_parent_dispatch_isolated_environment_and_terminal_recovery(tmp_path,monkeypatch,outcome):
    folder=tmp_path/'attempt';events=[];commands=[]
    payload={'execution':{'binary_version':'1.0'}}
    monkeypatch.setattr(milestone_driver,'prepare_execution',lambda *args:(payload,{'python':'/frozen/python'},'/explicit/auth.json'))
    monkeypatch.setenv('OPENAI_API_KEY','must-not-inherit');monkeypatch.setenv('SWE_MILESTONE_DATA_VERSION_CHECK','off')
    def validate(command,**kwargs):
        commands.append(command)
        assert 'OPENAI_API_KEY' not in kwargs['env'] and 'SWE_MILESTONE_DATA_VERSION_CHECK' not in kwargs['env']
        assert command[-1]=='--validate-execution' and not (folder/'native-preparation').exists()
        events.append('validated')
    monkeypatch.setattr(milestone_driver.subprocess,'run',validate)
    class Process:
        pid=12345
        def __init__(self,command,**kwargs):
            commands.append(command);assert command[-1]=='--execute' and kwargs['start_new_session']
            assert kwargs['env']['CTXPRESS_CODEX_AUTH_FILE']=='/explicit/auth.json'
            self.terminal=False;self.waits=0;events.append('spawned')
        def poll(self):return 0 if self.terminal else None
        def wait(self,timeout=None):
            self.waits+=1
            if outcome=='cancel' and self.waits==1:raise KeyboardInterrupt()
            self.terminal=True;events.append('terminal')
            if outcome=='success':
                (folder/'native-execution.json').write_text(json.dumps({'stop':'completed','calls':2,'cleanup_complete':True}), encoding='utf-8')
                (folder/'native-grade.json').write_text(json.dumps({'resolved':False}), encoding='utf-8')
                logs=folder/'agent-logs';logs.mkdir()
                (logs/'a.jsonl').write_text(json.dumps({'request':1,'tokens_before':100,'tokens_after':80,'changed':1,
                    'usage':{'input_tokens':80,'cached_tokens':60,'output_tokens':10}})+'\n{partial', encoding='utf-8')
            return 1 if outcome=='failure' else 0
    monkeypatch.setattr(milestone_driver.subprocess,'Popen',Process)
    monkeypatch.setattr(milestone_driver.os,'killpg',lambda pid,number:events.append('terminate'))
    monkeypatch.setattr(milestone_driver,'recover_attempt',lambda *args:events.append('recover'))
    adapter=SimpleNamespace(task_start_description=lambda:{'name':'swe-milestone'})
    args=(adapter,{'id':'repo'},{'class':'NoCompaction'},{'model':'fixture','reasoning':'low'}, {})
    kwargs={'paths':None,'folder':folder,'label':'owner'}
    if outcome=='cancel':
        with pytest.raises(KeyboardInterrupt):milestone_driver.execute(*args,**kwargs)
        assert events==['validated','spawned','terminate','terminal','recover']
    elif outcome=='failure':
        with pytest.raises(RuntimeError,match='worker failed'):milestone_driver.execute(*args,**kwargs)
        assert events[-2:]==['terminal','recover']
    else:
        result=milestone_driver.execute(*args,**kwargs)
        assert events[-2:]==['terminal','recover']
        assert result['usage']['api_cached_tokens']==60 and result['requests']==1
        assert result['grade']=={'resolved':False} and result['start_mode']=='task_start'
    assert all(command[1:4]==['-I','-S','-B'] for command in commands)
