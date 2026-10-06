"""Code artifacts, frozen checker imports and independent lifecycle without models/Docker."""
import asyncio, copy, hashlib, io, json, math, shutil, subprocess, sys, tarfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from ctxpress import benchmarks
from ctxpress.benchmarks import code_protocol, swe_driver
from ctxpress.harness import code_grading, code_report, code_trial, eval_inputs, eval_plan, eval_trees, evaluation, task, task_resources
from ctxpress.harness import eval_outcomes, eval_report
from test_swe_runner import Client, AGENT, GRADER, request as swe_request


def data(tmp_path,split='complete',subset='full'):
    root=tmp_path/'data';root.mkdir()
    rows=[dict(task_id='BigCodeBench/'+str(i),entry_point='task_func',complete_prompt='def task_func(x):\n    """Repair the parser."""\n',
        instruct_prompt='Return the parsed value using task_func(x).',code_prompt='def task_func(x):',
        canonical_solution='    return "hidden gold"',test='hidden official TestCases') for i in range(2)]
    (root/'instances.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
    (root/'dataset_manifest.json').write_text(json.dumps(dict(dataset='bigcode/bigcodebench'+('-hard' if subset=='hard' else ''),
        revision='fixture-v1',split=split,subset=subset)), encoding='utf-8')
    return root,rows


@pytest.mark.parametrize('split',['complete','instruct'])
@pytest.mark.parametrize('subset',['full','hard'])
def test_catalog_keeps_prompt_variants_and_private_reference_inputs(tmp_path,split,subset):
    root,rows=data(tmp_path,split,subset);adapter=benchmarks.get('bigcodebench');found=adapter.task_instances(root)
    assert len(found)==2 and found[0]['evaluation']['dataset']['subset']==subset
    assert found[0]['initial_state']['problem_statement']==rows[0][split+'_prompt']
    assert found[0]['initial_state']['seed_code']==(rows[0]['complete_prompt'] if split=='complete' else '')
    assert all('hidden' not in json.dumps(item) and 'hidden' not in adapter.agent_instruction(item) for item in found)
    assert code_protocol.instance(found[0])['canonical_solution']==rows[0]['canonical_solution']
    assert adapter.describe()['artifact_kind']=='code_samples' and not adapter.describe()['real_run_verified']


@pytest.mark.parametrize('change',['dataset','split','subset','revision','id','duplicate','prompt','entry','test'])
def test_invalid_data_and_grading_schema_fail_before_execution(tmp_path,change):
    root,rows=data(tmp_path);meta=json.loads((root/'dataset_manifest.json').read_text(encoding='utf-8'))
    if change=='dataset':meta['dataset']='bigcode/bigcodebench-hard'
    if change=='split':meta['split']='other'
    if change=='subset':meta['subset']='other'
    if change=='revision':meta['revision']=''
    if change=='id':rows[0]['task_id']='../escape'
    if change=='duplicate':rows.append(rows[0])
    if change=='prompt':rows[0]['complete_prompt']=''
    if change=='entry':rows[0]['entry_point']='not-valid()'
    if change=='test':rows[0]['test']=None
    (root/'dataset_manifest.json').write_text(json.dumps(meta), encoding='utf-8')
    (root/'instances.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
    with pytest.raises(ValueError):
        found=benchmarks.get('bigcodebench').task_instances(root);code_protocol.instance(found[0])


def test_official_sample_export_accepts_repeated_tasks_and_empty_solutions(tmp_path):
    samples=[dict(task_id='BigCodeBench/0',solution=''),dict(task_id='BigCodeBench/0',solution='def f():\n    return 1\n')]
    path=tmp_path/'samples.jsonl';benchmarks.get('bigcodebench').write_samples(path,samples)
    assert [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]==samples
    with pytest.raises(ValueError):benchmarks.get('bigcodebench').write_samples(path,[dict(task_id='BigCodeBench/0',solution=42)])
    assert [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]==samples


@pytest.mark.parametrize('value',[{'pass_k':[1,1]},{'pass_k':[]},{'pass_k':[True]},{'temperature':.2},
                                {'calibrated':1},{'min_time_limit':0},{'max_data_limit':1.5}])
def test_invalid_or_unsupported_generation_and_evaluation_options_are_rejected(value):
    with pytest.raises(ValueError):code_protocol.options(value)


def prepared(tmp_path,change=None,repeats=3):
    root,_=data(tmp_path);found=benchmarks.get('bigcodebench').task_instances(root)
    binary=tmp_path/'bin';binary.mkdir();(binary/'codex').write_bytes(b'fixture CLI')
    fixtures={
        'bigcodebench/__init__.py':'__version__="0.1.0"\n',
        'bigcodebench/eval/__init__.py':'''PASS='pass'
FAIL='fail'
TIMEOUT='timeout'
def untrusted_check(code, test_code, entry_point, max_as_limit, max_data_limit, max_stack_limit, min_time_limit, gt_time_limit):
    raise RuntimeError('preflight cannot evaluate a solution')
def estimate_pass_at_k(num_samples, num_correct, k):
    raise RuntimeError('preflight cannot compute a metric')
''',
        'bigcodebench/gen/util/__init__.py':'''def trusted_check(code, test_code, task_id, max_as_limit, max_data_limit, max_stack_limit, min_time_limit):
    raise RuntimeError('preflight cannot evaluate groundtruth')
''',
        'docker.py':"def from_env(*a, **k): raise RuntimeError('preflight cannot contact Docker')\n"}
    if change=='version':fixtures['bigcodebench/__init__.py']='__version__="0.2.0"\n'
    if change=='api':fixtures['bigcodebench/eval/__init__.py']=fixtures['bigcodebench/eval/__init__.py'].replace('gt_time_limit','wrong_parameter')
    names=['pyproject.toml','bigcodebench/__init__.py','bigcodebench/_version.py','bigcodebench/eval/__init__.py',
           'bigcodebench/eval/utils.py','bigcodebench/eval/_special_oracle.py','bigcodebench/gen/__init__.py','bigcodebench/gen/util/__init__.py']
    dependencies=['docker.py']+[name+'-0.1.0.dist-info/METADATA' for name in ('bigcodebench','docker','numpy')]
    trees={}
    for key,files in {'bigcodebench':names,'dependencies':dependencies}.items():
        directory=tmp_path/key
        for name in files:
            path=directory/name;path.parent.mkdir(parents=True,exist_ok=True)
            library=name.split('-')[0]
            path.write_text(fixtures.get(name,'Name: '+library+'\nVersion: 0.1.0\n' if name.endswith('/METADATA') else ''), encoding='utf-8')
        trees[key]=eval_trees.capture(directory,files,folder='unused')['tree']
    runtime=dict(python=str(Path(sys.executable).resolve()),sha256=eval_plan.file_sha256(sys.executable),version=sys.version,platform='linux')
    resources={item['id']:dict(task_sha256=task_resources.task_digest(item),images=dict(agent={'id':AGENT},grading={'verifier':{'id':GRADER}})) for item in found}
    lock=task_resources.seal(dict(schema=task_resources.SCHEMA,version=1,benchmark='bigcodebench',release='fixture-v1',tasks=resources,trees=trees,runtime=runtime))
    source=tmp_path/'resources.json';source.write_text(json.dumps(lock), encoding='utf-8')
    cfg=dict(schema='ctxpress.eval',version=1,benchmark='bigcodebench',start_mode='task_start',scope='benchmark',model='fixture-model',
        backend='codex_docker',tasks=[item['id'] for item in found],environment=dict(data=str(root),bindir=str(binary),resources=str(source)),
        methods=[{'class':'NoCompaction'}],repeats=repeats,run=dict(grading_timeout=23,code={'pass_k':[1,2,5]}))
    plan=eval_plan.compile_plan(cfg);assert not plan['missing_environment_files']
    directory=evaluation.prepare(plan,tmp_path/'run');effective,paths=eval_inputs.execution(plan,directory/'inputs');job=plan['jobs'][0]
    return plan,directory,effective,job,task.remap(job['task'],paths)


@pytest.mark.linux_only
@pytest.mark.parametrize('change',[None,'version','api'])
def test_frozen_host_and_grader_preflight_checks_author_api_after_sources_removed(tmp_path,monkeypatch,change):
    plan,directory,config,job,found=prepared(tmp_path,change)
    for name in ('data','bigcodebench','dependencies','bin'):shutil.rmtree(tmp_path/name)
    credentials=tmp_path/'auth.json';credentials.write_text('runtime fixture', encoding='utf-8');monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE',str(credentials))
    original_run=subprocess.run
    monkeypatch.setattr(swe_driver.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='codex-cli fixture-version\n'))
    req,_=swe_driver.prepare(found,job['method'],config,job,directory/'attempt','plan-job')
    assert req['api']=='codebench' and req['sample_index']==0 and req['n_samples']==3
    assert 'hidden' not in json.dumps(req) and not plan['benchmark']['real_run_verified']
    path=directory/'preflight.json';eval_plan.atomic_json(path,req)
    result=original_run([sys.executable,'-I','-S','-B',str(swe_driver.WORKER),str(path),'--check'],
        capture_output=True,text=True,timeout=15,env={'PATH':'/usr/bin:/bin'})
    if change is None:
        assert result.returncode==0,result.stderr
        assert json.loads(result.stdout)['framework']=='bigcodebench-host-docker'
    else:assert result.returncode!=0 and 'ValueError' in result.stderr
    assert not list((directory/'attempt').glob('resources-*'))


@pytest.mark.parametrize('change',['checker','metadata','runtime','grading','services','release','test'])
def test_resource_requirements_keep_missing_official_inputs_visible(tmp_path,change):
    plan,_,config,job,found=prepared(tmp_path);lock=copy.deepcopy(plan['task_resources'])
    if change=='checker':lock['trees']['bigcodebench']['files'].pop('bigcodebench/eval/__init__.py')
    if change=='metadata':lock['trees']['dependencies']['files']={}
    if change=='runtime':lock.pop('runtime')
    if change=='grading':lock['tasks'][found['id']]['images']['grading']={}
    if change=='services':lock['tasks'][found['id']]['images']['services']={'other':{'id':GRADER}}
    if change=='release':lock['release']='other'
    if change=='test':found['initial_state']['entry_point']='different'
    assert code_protocol.requirements(config,[found],lock)


def author(status='pass',reference_time=.1):
    events=[]
    def trusted_check(**kwargs):events.append(('reference',kwargs));return dict(task_id=kwargs['task_id'],time=reference_time)
    def untrusted_check(**kwargs):events.append(('sample',kwargs));return status,{} if status=='pass' else {'test_parse':'author failure'}
    def estimate_pass_at_k(num_samples,num_correct,k):
        events.append(('estimator',num_samples,k))
        # Synthetic official interface; tests verify the production code calls it.
        values=[1-math.comb(num_samples-c,k)/math.comb(num_samples,k) if num_samples-c>=k else 1.0 for c in num_correct]
        return SimpleNamespace(tolist=lambda:values)
    checker=SimpleNamespace(PASS='pass',FAIL='fail',TIMEOUT='timeout',untrusted_check=untrusted_check,estimate_pass_at_k=estimate_pass_at_k)
    return (checker,SimpleNamespace(trusted_check=trusted_check),'fixture-author'),events


@pytest.mark.parametrize('calibrated',[True,False])
@pytest.mark.parametrize('reference_time',[.1,0,None])
def test_official_checks_keep_reference_timing_calibration_limits_and_estimator(tmp_path,calibrated,reference_time):
    root,_=data(tmp_path);found=benchmarks.get('bigcodebench').task_instances(root)[0];problem=code_protocol.instance(found)
    policy=code_protocol.options(dict(calibrated=calibrated,pass_k=[1,2,5]));official,events=author(reference_time=reference_time)
    request=dict(task_id=found['id'],sample_id=1,n_samples=3,dataset=found['evaluation']['dataset'],options=policy)
    result=code_grading.evaluate(request,official,problem,'def task_func(x): return x')
    assert events[0][1]['code']==problem['complete_prompt']+'\n'+problem['canonical_solution']
    assert result['groundtruth_valid']==(reference_time is not None)
    if reference_time is None:assert len(events)==1 and result['status'] is None
    else:
        sample=next(event[1] for event in events if event[0]=='sample')
        assert sample['gt_time_limit']==(reference_time if reference_time else 20)
        assert sample['code'].startswith(problem['code_prompt']+'\n    pass\n')==calibrated
        assert sample['max_stack_limit']==10 and result['estimator_table']['1'][1]==pytest.approx(1/3)
        assert set(result['estimator_table'])=={'1','2'}


def runtime_request(tmp_path,found,n=3,index=0):
    req=swe_request(tmp_path,'codebench');req.update(task=found,repository_directory='/ctxpress-task',
        runtime={'version':sys.version},sample_index=index,n_samples=n,official=str(tmp_path/'official'))
    req['run']['code']=code_protocol.options(dict(pass_k=[1,2,5]));return req


class CodeClient(Client):
    def __init__(self,official):super().__init__();self.official=official;self.solution='def task_func(x): return x';self.fault=None
    def create(self,**options):
        container=super().create(**options);original=container.exec_run
        def put_archive(directory,value):
            with tarfile.open(fileobj=io.BytesIO(value)) as archive:
                assert directory=='/ctxpress-task';self.events.append(('seed',archive.extractfile('solution.py').read().decode()))
            return True
        container.put_archive=put_archive
        def execute(args,**kwargs):
            result=original(args,**kwargs)
            if args[:2]==['/bin/bash','-lc'] and args[-1].startswith('python3 -c '):result.output=(json.dumps(self.solution).encode(),b'')
            if args[0]=='timeout':
                if args[-1]=='--check':
                    if self.fault=='imports':result.exit_code=1
                    return result
                self.events.append(('official-sample-execution',True))
                if self.fault=='timeout':result.exit_code=124;return result
                if self.fault=='missing':return result
                volumes=options['volumes'];inputs=Path(next(path for path,mount in volumes.items() if mount['bind']=='/ctxpress-grade-input'))
                output=Path(next(path for path,mount in volumes.items() if mount['bind']=='/ctxpress-grade-output'))
                request=json.loads((inputs/'request.json').read_text(encoding='utf-8'));problem=json.loads((inputs/'problem.json').read_text(encoding='utf-8'))
                result_data=code_grading.evaluate(request,self.official,problem,(inputs/'solution.py').read_text(encoding='utf-8'))
                eval_plan.atomic_json(output/'sample-result.json',result_data)
            return result
        container.exec_run=execute;return container


@pytest.mark.parametrize('mode',['pass','fail','sample-timeout','agent-timeout','empty','no-grade','cleanup-failure','imports','outer-timeout','missing','reference-failure'])
def test_complete_production_lifecycle_preserves_samples_and_clean_independent_grading(tmp_path,monkeypatch,mode):
    root,_=data(tmp_path);found=benchmarks.get('bigcodebench').task_instances(root)[0];req=runtime_request(tmp_path,found)
    official,_=author(status='timeout' if mode=='sample-timeout' else 'fail' if mode in ('fail','empty') else 'pass',
                      reference_time=None if mode=='reference-failure' else .1)
    client=CodeClient(official)
    client.fault={'outer-timeout':'timeout','imports':'imports','missing':'missing'}.get(mode)
    if mode=='empty':client.solution=''
    if mode=='no-grade':req['run']['grade']=False
    req['run']['timeout']=.01
    def agent_class(request,credentials):
        class Agent:
            ctxpress_stop=None
            async def setup(self,environment):credentials(True);client.events.append(('credentials',True))
            async def run(self,*args):
                if mode=='agent-timeout':await asyncio.sleep(1)
            async def cleanup_credentials(self,environment):
                if mode=='cleanup-failure':raise RuntimeError('credential deletion failed')
                credentials(False);client.events.append(('credentials',False))
        return Agent
    monkeypatch.setattr(code_trial,'agent_class',agent_class)
    if mode in ('cleanup-failure','imports','outer-timeout','missing'):
        with pytest.raises(RuntimeError):asyncio.run(code_trial.run(req,None,client,code_protocol.instance(found),tmp_path/'channel','daemon'))
        if mode=='cleanup-failure':assert client.live and not any(event[0]=='official-sample-execution' for event in client.events)
        else:assert not client.live
        return
    asyncio.run(code_trial.run(req,None,client,code_protocol.instance(found),tmp_path/'channel','daemon'))
    state=json.loads((tmp_path/'swe-worker-result.json').read_text(encoding='utf-8'));assert not client.live and state['artifact_kind']=='code_samples'
    sample=json.loads((tmp_path/'samples.jsonl').read_text(encoding='utf-8'));assert sample==dict(task_id=found['id'],solution=client.solution)
    assert state['stop']==('timeout' if mode=='agent-timeout' else 'completed')
    if mode=='no-grade':assert state['official_report'] is None;return
    grade=benchmarks.get('bigcodebench').read_grade(found,state['official_report'])
    if mode=='reference-failure':assert grade['infra_invalid'] and grade['resolved'] is None;return
    assert grade['sample_passed']==(mode not in ('fail','empty','sample-timeout')) and grade['resolved'] is None
    outcome=eval_outcomes.observe(grade);assert outcome['code_sample_valid'] and not outcome['boolean_valid'] and not outcome['ungraded']
    creates=[event[1] for event in client.events if event[0]=='create'];assert len(creates)==2
    assert creates[1]['image']==GRADER and creates[1]['network_mode']=='none'
    assert not any(mount['bind'] in ('/cxbin','/ctxpress-method','/ctxpress-channel') for mount in creates[1]['volumes'].values())
    assert all(mount['mode']=='ro' for mount in creates[1]['volumes'].values() if mount['bind']!='/ctxpress-grade-output')
    events=[event[0] for event in client.events];assert events.index('remove')<events.index('official-sample-execution')
    report=Path(state['official_report']);report.write_bytes(report.read_bytes()+b' ')
    assert 'error' in benchmarks.get('bigcodebench').read_grade(found,report)


def test_report_aggregates_author_estimator_only_for_a_complete_bound_cohort(tmp_path):
    plan,directory,_,_,_=prepared(tmp_path);jobs=[];adapter=benchmarks.get('bigcodebench')
    for spec in plan['jobs']:
        attempt=tmp_path/spec['id'];attempt.mkdir();found=spec['task'];req=runtime_request(attempt,found,index=spec['repeat'])
        official,_=author(status='pass' if spec['repeat']==0 else 'fail');client=CodeClient(official)
        agent=code_trial.Owner(req,client,'agent','daemon');agent.create();agent.record['task_id']=found['id'];agent.cleanup()
        owner=code_trial.Owner(req,client,'verifier','daemon')
        adapter.write_samples(attempt/'samples.jsonl',[dict(task_id=found['id'],solution=client.solution)])
        path=code_trial.grade(req,client,code_protocol.instance(found),client.solution,owner,agent)
        grade=adapter.read_grade(found,path);assert 'error' not in grade,grade
        jobs.append(dict(spec=json.dumps(spec),status='completed',result=json.dumps(dict(grade=grade))))
    metrics=code_report.summarize(plan,jobs)['NoCompaction']
    assert metrics['complete'] and metrics['valid_samples']==6 and metrics['task_count']==2
    assert metrics['pass_at_k']['1']==pytest.approx(1/3) and metrics['pass_at_k']['2']==pytest.approx(2/3) and metrics['pass_at_k']['5'] is None
    assert metrics['unavailable']['5']=='insufficient samples'
    with evaluation.database(directory) as connection:
        for job in jobs:
            spec=json.loads(job['spec']);result=json.loads(job['result']);result['test_only']=True
            connection.execute("UPDATE jobs SET status='completed',attempt=1,result=? WHERE id=?",(json.dumps(result),spec['id']))
    files=eval_report.write_report(directory);group=eval_report.report(directory)['methods'][0]
    assert group['valid_grades']==0 and group['valid_rewards']==0 and group['valid_code_samples']==6 and group['ungraded']==0
    assert group['code_metrics']['pass_at_k']['1']==pytest.approx(1/3) and group['resolve_rate_valid_grades'] is None
    assert 'pass@1: 33.3%' in Path(files['html']).read_text(encoding='utf-8') and '有效代码样本' in Path(files['html']).read_text(encoding='utf-8')
    incomplete=code_report.summarize(plan,jobs[:3])['NoCompaction']
    assert not incomplete['complete'] and incomplete['task_count']==2 and incomplete['planned_samples']==6
    assert all(value is None for value in incomplete['pass_at_k'].values())
    jobs[0]['status']='failed';assert not code_report.summarize(plan,jobs)['NoCompaction']['complete']
    assert all(value is None for value in code_report.summarize(plan,jobs)['NoCompaction']['pass_at_k'].values())
    jobs[0]['status']='completed';raw=json.loads(jobs[0]['result']);Path(raw['grade']['report']).unlink()
    assert not code_report.summarize(plan,jobs)['NoCompaction']['complete']
    current=eval_report.report(directory)['methods'][0]
    assert current['valid_code_samples']==5 and not current['code_metrics']['complete']
