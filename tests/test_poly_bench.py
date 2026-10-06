"""PolyBench schema, frozen dispatch and author API composition without models/Docker."""
import copy, json, shutil, subprocess, sys, threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
import pytest
from ctxpress import benchmarks
from ctxpress.benchmarks import poly_protocol, swe_driver
from ctxpress.harness import eval_inputs, eval_plan, eval_trees, evaluation, task, task_resources
from ctxpress.harness import poly_grading, swe_containers
from test_swe_runner import Client, AGENT, GRADER, COMMIT, request as swe_request


def data(tmp_path,dataset='AmazonScience/SWE-PolyBench_Verified'):
    root=tmp_path/'data';root.mkdir()
    rows=[dict(instance_id='owner__repo-'+str(index),repo='owner/repo',base_commit=COMMIT,
        language=language,task_category=category,problem_statement='Repair '+language+'.',
        patch='hidden gold',test_patch='hidden tests',Dockerfile='FROM prepared:fixture',
        F2P="['regression']",P2P='[]',test_command='author-test-command',modified_nodes='[]')
        for index,(language,category) in enumerate(zip(['Python','Java','Javascript','Typescript'],['Bug Fix','Feature','Refactoring','Bug Fix']),1)]
    (root/'instances.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
    (root/'dataset_manifest.json').write_text(json.dumps(dict(dataset=dataset,revision='fixture-v1')), encoding='utf-8')
    return root,rows


@pytest.mark.parametrize('dataset',['AmazonScience/SWE-PolyBench','AmazonScience/SWE-PolyBench_500','AmazonScience/SWE-PolyBench_Verified'])
def test_declared_variants_preserve_languages_and_categories_without_gold(tmp_path,dataset):
    root,_=data(tmp_path,dataset);adapter=benchmarks.get('swe-polybench');found=adapter.task_instances(root)
    assert [item['initial_state']['language'] for item in found]==['python','java','javascript','typescript']
    assert all(item['evaluation']['dataset']['name']==dataset for item in found)
    assert all('hidden' not in adapter.agent_instruction(item) and 'hidden' not in json.dumps(item) for item in found)
    assert [item['id'] for item in adapter.task_instances(root,languages=['typescript'],categories=['Bug Fix'])]==['owner__repo-4']
    assert all(item['inputs'][0]['role']=='grading' for item in found)


@pytest.mark.parametrize('change',['dataset','revision','language','category','duplicate','test-command','F2P'])
def test_invalid_provenance_tasks_and_grading_fields_are_rejected(tmp_path,change):
    root,rows=data(tmp_path);meta=json.loads((root/'dataset_manifest.json').read_text(encoding='utf-8'))
    if change=='dataset':meta['dataset']='princeton-nlp/SWE-bench_Verified'
    if change=='revision':meta['revision']=''
    if change=='language':rows[0]['language']='rust'
    if change=='category':rows[0]['task_category']='other'
    if change=='duplicate':rows.append(rows[0])
    if change=='test-command':rows[0]['test_command']=''
    if change=='F2P':rows[0]['F2P']="__import__('os').system('false')"
    (root/'dataset_manifest.json').write_text(json.dumps(meta), encoding='utf-8')
    (root/'instances.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
    with pytest.raises(ValueError):
        found=benchmarks.get('swe-polybench').task_instances(root)
        poly_protocol.prepared_instance(found[0],lambda **fields:fields)


def prepared(tmp_path,worker_inputs=False,worker_change=None):
    root,_=data(tmp_path);adapter=benchmarks.get('swe-polybench');found=adapter.task_instances(root)[0]
    binary=tmp_path/'bin';binary.mkdir();(binary/'codex').write_bytes(b'fixture CLI')
    names=['pyproject.toml',*[f'src/poly_bench_evaluation/{name}' for name in
        ('__init__.py','run_evaluation.py','polybench_data.py','docker_utils.py','scoring.py','constants.py','parsers/__init__.py')]]
    trees={}
    fixture={
        'pyproject.toml':'[project]\nname="poly_bench_evaluation"\nversion="0.1.0"\n',
        'src/poly_bench_evaluation/polybench_data.py':'class PolyBenchInstance:\n    def __init__(self, **fields): self.__dict__.update(fields)\n    def model_copy(self, update): return PolyBenchInstance(**(vars(self)|update))\n',
        'src/poly_bench_evaluation/run_evaluation.py':'''REPO_TO_PARSER_CLASS={'owner/repo':'fixture'}
class DockerManager:
    def __init__(self, image_id, delete_image, client): pass
    def check_image_local(self, local_image_name): pass
    def create_container(self): pass
    def apply_patch_to_container(self, patch_content, patch_type): pass
    def docker_run(self, test_command, timeout): pass
    def _cleanup(self): pass
    def _get_workdir_from_image(self): pass
JAVA_TIMEOUT=1800
DEFAULT_TIMEOUT=1200
def instance_level_metric_scoring(*a, **k): raise RuntimeError('unexpected metrics')
def store_instance_level_output(*a, **k): pass
def evaluate_instance(instance, result_path, evaluate_gold, repo_path, delete_image, client,
                      retrieval_metrics_only, node_retrieval_metrics, repair_native_packages):
    raise RuntimeError('preflight cannot execute a trial')
''',
        'docker.py':"def from_env(*a, **k): raise RuntimeError('preflight cannot contact Docker')\n",
        'poly_bench_evaluation-0.1.0.dist-info/METADATA':'Name: poly_bench_evaluation\nVersion: 0.1.0\n'}
    if worker_change=='version':fixture['poly_bench_evaluation-0.1.0.dist-info/METADATA']=fixture['poly_bench_evaluation-0.1.0.dist-info/METADATA'].replace('0.1.0','0.2.0')
    if worker_change=='parser':fixture['src/poly_bench_evaluation/run_evaluation.py']=fixture['src/poly_bench_evaluation/run_evaluation.py'].replace("'owner/repo'","'different/repo'")
    if worker_change=='api':fixture['src/poly_bench_evaluation/run_evaluation.py']=fixture['src/poly_bench_evaluation/run_evaluation.py'].replace('repair_native_packages','different_parameter')
    for key,files in {'polybench':names,'dependencies':['poly_bench_evaluation-0.1.0.dist-info/METADATA']+(['docker.py'] if worker_inputs else [])}.items():
        directory=tmp_path/key
        for name in files:
            path=directory/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(fixture.get(name,'') if worker_inputs else '# fixture input\n', encoding='utf-8')
        trees[key]=eval_trees.capture(directory,files,folder='unused')['tree']
    runtime=dict(python=str(Path(sys.executable).resolve()),sha256=eval_plan.file_sha256(sys.executable),version=sys.version,platform='linux')
    record=dict(task_sha256=task_resources.task_digest(found),images={'agent':{'id':AGENT},'grading':{'verifier':{'id':GRADER}}})
    lock=task_resources.seal(dict(schema=task_resources.SCHEMA,version=1,benchmark='swe-polybench',release='fixture-v1',
        tasks={found['id']:record},trees=trees,runtime=runtime))
    resource=tmp_path/'resources.json';resource.write_text(json.dumps(lock), encoding='utf-8')
    cfg=dict(schema='ctxpress.eval',version=1,benchmark='swe-polybench',start_mode='task_start',scope='benchmark',model='fixture-model',
        backend='codex_docker',tasks=[found['id']],environment=dict(data=str(root),bindir=str(binary),resources=str(resource)),
        methods=[{'class':'NoCompaction'}],run={'grading_timeout':23})
    plan=eval_plan.compile_plan(cfg);assert plan['missing_environment_files']==[]
    directory=evaluation.prepare(plan,tmp_path/'run');effective,paths=eval_inputs.execution(plan,directory/'inputs');job=plan['jobs'][0]
    return plan,directory,effective,job,task.remap(job['task'],paths)


@pytest.mark.linux_only
@pytest.mark.parametrize('change',[None,'version','parser','api'])
def test_isolated_preflight_checks_frozen_imports_without_models_or_docker(tmp_path,monkeypatch,change):
    _,directory,config,job,found=prepared(tmp_path,worker_inputs=True,worker_change=change)
    for name in ('data','polybench','dependencies','bin'):shutil.rmtree(tmp_path/name)
    credentials=tmp_path/'auth.json';credentials.write_text('runtime fixture', encoding='utf-8')
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE',str(credentials))
    original_run=subprocess.run
    monkeypatch.setattr(swe_driver.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='codex-cli fixture-version\n'))
    req,_=swe_driver.prepare(found,job['method'],config,job,directory/'attempt','plan-job')
    request_file=directory/'preflight-request.json';eval_plan.atomic_json(request_file,req)
    result=original_run([sys.executable,'-I','-S','-B',str(swe_driver.WORKER),str(request_file),'--check'],
        capture_output=True,text=True,timeout=15,env={'PATH':'/usr/bin:/bin'})
    if change is None:
        assert result.returncode==0,result.stderr
        assert json.loads(result.stdout)==dict(imports='verified',framework='poly_bench_evaluation',version='0.1.0',model_calls=0)
    else:assert result.returncode!=0 and 'ValueError' in result.stderr
    assert not list((directory/'attempt').glob('resources-*'))


def test_dispatch_uses_only_frozen_inputs_and_shared_patch_resource_recovery(tmp_path,monkeypatch):
    plan,directory,config,job,found=prepared(tmp_path)
    for name in ('data','polybench','dependencies','bin'):shutil.rmtree(tmp_path/name)
    credentials=tmp_path/'auth.json';credentials.write_text('runtime fixture', encoding='utf-8')
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE',str(credentials))
    monkeypatch.setattr(swe_driver.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='codex-cli fixture-version\n'))
    req,auth=swe_driver.prepare(found,job['method'],config,job,directory/'attempt','plan-job')
    assert req['api']=='polybench' and req['task']['initial_state']['language']=='python'
    assert req['agent_image']==AGENT and req['grading_image']==GRADER and req['run']['grading_timeout']==23
    assert 'hidden gold' not in json.dumps(req) and 'hidden tests' not in json.dumps(req)
    assert auth==str(credentials.resolve()) and not plan['benchmark']['real_run_verified']
    changed=copy.deepcopy(found);changed['initial_state']['language']='java'
    with pytest.raises(ValueError,match='binding'):poly_protocol.instance(changed)


@pytest.mark.parametrize('change',['source','metadata','release','runtime','grader','services','F2P'])
def test_plan_reports_missing_official_resources(tmp_path,change):
    plan,_,config,job,found=prepared(tmp_path);lock=copy.deepcopy(plan['task_resources'])
    if change=='source':lock['trees']['polybench']['files'].pop('src/poly_bench_evaluation/scoring.py')
    if change=='metadata':lock['trees']['dependencies']['files']={}
    if change=='release':lock['release']='other'
    if change=='runtime':lock['runtime']['platform']='win32'
    if change=='grader':lock['tasks'][found['id']]['images']['grading']={}
    if change=='services':lock['tasks'][found['id']]['images']['services']={'other':{'id':GRADER}}
    if change=='F2P':
        source=Path(found['inputs'][0]['path']);source.write_text(source.read_text(encoding='utf-8').replace("['regression']",'[]'), encoding='utf-8')
    assert poly_protocol.requirements(config,[found],lock)


def official_fixture(monkeypatch,events,mode):
    module=ModuleType('synthetic_poly_docker');module.threading=threading
    class Manager:
        def __init__(self,image_id,delete_image,client):self.image_id=image_id;self.client=client;self.container=None
        def check_image_local(self,*args):pytest.fail('author attempted image lookup without prepared hook')
        def create_container(self):pytest.fail('author attempted unmanaged container')
        def _cleanup(self):pytest.fail('author attempted unmanaged cleanup')
        def apply_patch_to_container(self,patch_content,patch_type):
            events.append(('author-patch',patch_type,patch_content))
            if patch_type=='test' and mode=='test-patch-failure':raise ValueError('fixture test patch failed')
            if patch_type=='code' and mode=='model-patch-failure':return 1
            return 0
        def docker_run(self,test_command,timeout):
            events.append(('author-test',test_command,timeout))
            self._cleanup();return 0
    Manager.__module__=module.__name__;module.DockerManager=Manager
    monkeypatch.setitem(sys.modules,module.__name__,module)
    api=SimpleNamespace(DockerManager=Manager,JAVA_TIMEOUT=1800,DEFAULT_TIMEOUT=1200)
    def store(instance_output,result_path,suffix='_result'):
        Path(result_path,instance_output['instance_id']+suffix+'.json').write_text(json.dumps(instance_output), encoding='utf-8')
    api.store_instance_level_output=store
    api.instance_level_metric_scoring=lambda *a,**k:pytest.fail('author attempted reference-source retrieval')
    def evaluate_instance(instance,result_path,evaluate_gold,repo_path,delete_image,client,retrieval_metrics_only,node_retrieval_metrics,repair_native_packages):
        assert not any((evaluate_gold,delete_image,retrieval_metrics_only,node_retrieval_metrics,repair_native_packages))
        assert Path.cwd()==Path(result_path)
        Path('relative-author.log').write_text('test IO fixture', encoding='utf-8')
        row=dict(instance_id=instance.instance_id,patch_applied=False,generation=bool(instance.model_patch),
            with_logs=False,all_f2p_passed=False,no_p2p_failed=False,resolved=False,passed_tests=[],failed_tests=[])
        if instance.model_patch:
            manager=api.DockerManager('polybench_'+instance.language.lower()+'_'+instance.instance_id.lower(),delete_image,client)
            assert manager.check_image_local(manager.image_id);manager.create_container()
            try:
                manager.apply_patch_to_container(instance.test_patch,'test')
                result=manager.apply_patch_to_container(instance.model_patch,'code')
                if result==0:
                    manager.docker_run(instance.test_command,api.JAVA_TIMEOUT if instance.language.lower()=='java' else api.DEFAULT_TIMEOUT)
                    row.update(patch_applied=True,with_logs=True,all_f2p_passed=True,no_p2p_failed=True,resolved=True,passed_tests=['regression'])
            except ValueError:pass
            manager._cleanup()
        api.store_instance_level_output(row,result_path)
        metrics=api.instance_level_metric_scoring(instance=instance,repo_path=repo_path)
        api.store_instance_level_output(metrics,result_path,'_metrics')
    api.evaluate_instance=evaluate_instance
    return api,module


class Instance(SimpleNamespace):
    def model_copy(self,update):return Instance(**(vars(self)|update))


@pytest.mark.parametrize('mode',['complete','empty','model-patch-failure','test-patch-failure'])
@pytest.mark.parametrize('language',['Python','Java','Javascript','Typescript'])
def test_author_evaluation_uses_clean_owned_verifier_and_omits_retrieval(tmp_path,monkeypatch,mode,language):
    root,_=data(tmp_path);found=benchmarks.get('swe-polybench').task_instances(root)[0]
    req=swe_request(tmp_path);req.update(api='polybench',task=found,repository_directory='/repo')
    client=Client();events=[];api,docker_module=official_fixture(monkeypatch,events,mode)
    original=api.DockerManager;original_threading=docker_module.threading
    agent=swe_containers.Owner(req,client,'agent','daemon');agent.cleanup()
    owner=swe_containers.Owner(req,client,'verifier','daemon')
    instance=poly_protocol.prepared_instance(found,Instance);instance.language=language
    prediction=dict(instance_id=found['id'],model_patch='' if mode=='empty' else 'agent diff')
    if mode=='test-patch-failure':
        with pytest.raises(RuntimeError,match='infrastructure'):poly_grading.grade(req,api,client,instance,owner,agent,prediction)
    else:
        report=poly_grading.grade(req,api,client,instance,owner,agent,prediction)
        result=benchmarks.get('swe-polybench').read_grade(found,report)
        assert result['resolved']==(mode=='complete') and not result['retrieval_metrics_supported']
        assert not list(report.parent.glob('*_metrics.json'))
        assert (report.parent/'relative-author.log').is_file()
    assert not client.live and owner.record['cleaned']
    assert api.DockerManager is original and docker_module.threading is original_threading
    assert api.JAVA_TIMEOUT==1800 and api.DEFAULT_TIMEOUT==1200
    if mode!='empty':
        options=next(event[1] for event in client.events if event[0]=='create')
        assert options['image']==GRADER and options['network_mode']=='none' and 'volumes' not in options
        assert options['working_dir']=='/repo'
        assert events[0]==('author-patch','test','hidden tests')
    if mode=='complete':assert events[-1]==('author-test','author-test-command',23)


@pytest.mark.parametrize('directory',['','/','../repo','/repo/../other','/repo/'])
def test_prepared_image_repository_directory_is_validated_before_credentials(directory,tmp_path):
    req=swe_request(tmp_path);client=Client()
    client.images=SimpleNamespace(get=lambda identity:SimpleNamespace(id=identity,attrs={'Os':'linux','Config':{'WorkingDir':directory}}))
    with pytest.raises(ValueError,match='WorkingDir'):poly_grading.repository_directory(req,client)
    assert not client.live


@pytest.mark.parametrize('change',['wrong-instance','numeric','missing-tests','contradictory','invalid-json'])
def test_grade_reader_never_turns_ambiguous_evidence_into_success(tmp_path,change):
    root,_=data(tmp_path);adapter=benchmarks.get('swe-polybench');found=adapter.task_instances(root)[0]
    row=dict(instance_id=found['id'],patch_applied=True,generation=True,with_logs=True,all_f2p_passed=True,no_p2p_failed=True,
        resolved=True,passed_tests=['regression'],failed_tests=[])
    if change=='wrong-instance':row['instance_id']='other__repo-1'
    if change=='numeric':row['resolved']=1
    if change=='missing-tests':row.pop('passed_tests')
    if change=='contradictory':row['with_logs']=False
    report=tmp_path/'report.json';report.write_text('{' if change=='invalid-json' else json.dumps(row), encoding='utf-8')
    assert adapter.read_grade(found,report)['resolved'] is None
