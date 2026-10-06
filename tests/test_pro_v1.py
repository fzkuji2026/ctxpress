"""V1 freezing and independent author-main composition; synthetic runtime IO only."""
import copy, json, shutil, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from ctxpress import benchmarks
from ctxpress.benchmarks import pro_v1, swe_driver
from ctxpress.harness import eval_inputs, eval_plan, eval_trees, evaluation, task, task_resources
from ctxpress.harness import pro_v1_grading, swe_containers
from test_swe_runner import Client, COMMIT, AGENT, GRADER, request as swe_request

ID='instance_owner__repo-'+'f'*40


def data(tmp_path):
    root=tmp_path/'data';root.mkdir()
    row=dict(instance_id=ID,repo='owner/repo',base_commit=COMMIT,problem_statement='Repair the parser.',
        requirements='Preserve streams.',interface='parse(source)',patch='hidden gold',test_patch='hidden tests',
        fail_to_pass="['regression']",pass_to_pass=['existing'],selected_test_files_to_run="['tests/test_parse.py']",
        before_repo_set_cmd='export AUTHOR_MODE=1')
    (root/'instances.jsonl').write_text(json.dumps(row)+'\n', encoding='utf-8')
    (root/'dataset_manifest.json').write_text(json.dumps(dict(dataset='swe-bench-pro',revision='fixture-v1',benchmark_version='v1',subset='public')), encoding='utf-8')
    return root,row


def prepared(tmp_path,worker_change=None):
    root,_=data(tmp_path);found=benchmarks.get('swe-bench-pro').task_instances(root)[0]
    binary=tmp_path/'bin';binary.mkdir();(binary/'codex').write_bytes(b'fixture CLI')
    source={
        'swe_bench_pro_eval.py':'''import docker
from helper_code.image_uri import get_dockerhub_image_uri
def eval_with_docker(patch, sample, output_dir, dockerhub_username, scripts_dir, prefix='', redo=False, block_network=False, docker_platform=None):
    raise RuntimeError('preflight must not execute grading')
def main(): raise RuntimeError('preflight must not run a task')
def parse_args(): pass
def assemble_workspace_files(): pass
def create_entryscript(): pass
def strip_binary_hunks(): pass
def collect_outputs_local(): pass
''',
        'helper_code/image_uri.py':'def get_dockerhub_image_uri(*a): pass\n',
        'docker.py':"def from_env(*a, **k): raise RuntimeError('preflight must not contact Docker')\n"}
    if worker_change=='api':source['swe_bench_pro_eval.py']=source['swe_bench_pro_eval.py'].replace('block_network=False','wrong_network=False')
    files=['swe_bench_pro_eval.py','helper_code/image_uri.py',f'run_scripts/{ID}/run_script.sh',f'run_scripts/{ID}/parser.py',
           f'dockerfiles/base_dockerfile/{ID}/Dockerfile',f'dockerfiles/instance_dockerfile/{ID}/Dockerfile']
    dependencies=['docker.py']+[name+'-0.1.0.dist-info/METADATA' for name in ('pandas','docker','tqdm')]
    trees={}
    for key,names in {'pro_v1':files,'dependencies':dependencies}.items():
        directory=tmp_path/key
        for name in names:
            path=directory/name;path.parent.mkdir(parents=True,exist_ok=True)
            library=name.split('-')[0]
            content=source.get(name,'Name: '+library+'\nVersion: 0.1.0\n' if name.endswith('/METADATA') else '# fixture author input\n')
            path.write_text(content, encoding='utf-8')
        trees[key]=eval_trees.capture(directory,names,folder='unused')['tree']
    runtime=dict(python=str(Path(sys.executable).resolve()),sha256=eval_plan.file_sha256(sys.executable),version=sys.version,platform='linux')
    resource=dict(task_sha256=task_resources.task_digest(found),images=dict(agent={'id':AGENT},grading={'verifier':{'id':GRADER}}))
    lock=task_resources.seal(dict(schema=task_resources.SCHEMA,version=1,benchmark='swe-bench-pro',release='fixture-v1',
        tasks={ID:resource},trees=trees,runtime=runtime))
    resources=tmp_path/'resources.json';resources.write_text(json.dumps(lock), encoding='utf-8')
    cfg=dict(schema='ctxpress.eval',version=1,benchmark='swe-bench-pro',start_mode='task_start',scope='benchmark',model='fixture-model',
        backend='codex_docker',tasks=[ID],environment=dict(data=str(root),bindir=str(binary),resources=str(resources)),
        methods=[{'class':'NoCompaction'}],run={'grading_timeout':23})
    plan=eval_plan.compile_plan(cfg);assert not plan['missing_environment_files']
    directory=evaluation.prepare(plan,tmp_path/'run');effective,paths=eval_inputs.execution(plan,directory/'inputs');job=plan['jobs'][0]
    return plan,directory,effective,job,task.remap(job['task'],paths)


def test_v1_catalog_uses_dataset_base_not_fixing_sha_and_only_public_prompt(tmp_path):
    root,row=data(tmp_path);adapter=benchmarks.get('swe-bench-pro');found=adapter.task_instances(root)[0]
    prompt=adapter.agent_instruction(found)
    assert found['initial_state']['base_commit']==COMMIT and COMMIT not in ID
    assert all(value in prompt for value in (row['problem_statement'],row['requirements'],row['interface']))
    assert 'hidden' not in prompt and 'regression' not in json.dumps(found)
    assert pro_v1.instance(found)['fail_to_pass']=="['regression']"


@pytest.mark.parametrize('change',['base','id','f2p','eval','interface','subset','duplicate'])
def test_v1_rejects_wrong_revision_schema_and_executable_test_lists(tmp_path,change):
    root,row=data(tmp_path);meta=json.loads((root/'dataset_manifest.json').read_text(encoding='utf-8'))
    if change=='base':row['base_commit']='main'
    if change=='id':row['instance_id']='../foreign'
    if change=='f2p':row['fail_to_pass']=[]
    if change=='eval':row['selected_test_files_to_run']="__import__('os').system('false')"
    if change=='interface':row['interface']=['wrong']
    if change=='subset':meta['subset']='hard51'
    rows=[row,row] if change=='duplicate' else [row]
    (root/'instances.jsonl').write_text(''.join(json.dumps(item)+'\n' for item in rows), encoding='utf-8')
    (root/'dataset_manifest.json').write_text(json.dumps(meta), encoding='utf-8')
    with pytest.raises(ValueError):benchmarks.get('swe-bench-pro').task_instances(root)


@pytest.mark.linux_only
@pytest.mark.parametrize('change',[None,'api'])
def test_v1_isolated_preflight_uses_frozen_inputs_after_originals_removed(tmp_path,monkeypatch,change):
    plan,directory,config,job,found=prepared(tmp_path,change)
    for name in ('data','pro_v1','dependencies','bin'):shutil.rmtree(tmp_path/name)
    auth=tmp_path/'auth.json';auth.write_text('runtime fixture', encoding='utf-8');monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE',str(auth))
    original_run=subprocess.run
    monkeypatch.setattr(swe_driver.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='codex-cli fixture-version\n'))
    req,_=swe_driver.prepare(found,job['method'],config,job,directory/'attempt','plan-job')
    assert req['api']=='pro-v1' and req['agent_image']==AGENT and req['grading_image']==GRADER and req['run']['grading_timeout']==23
    assert 'hidden' not in json.dumps(req) and not plan['benchmark']['real_run_verified']
    request_file=directory/'preflight.json';eval_plan.atomic_json(request_file,req)
    result=original_run([sys.executable,'-I','-S','-B',str(swe_driver.WORKER),str(request_file),'--check'],
        capture_output=True,text=True,timeout=15,env={'PATH':'/usr/bin:/bin'})
    if change is None:
        assert result.returncode==0,result.stderr
        assert json.loads(result.stdout)==dict(imports='verified',framework='pro-v1-author',model_calls=0)
    else:assert result.returncode!=0 and 'ValueError' in result.stderr
    assert not list((directory/'attempt').glob('resources-*'))


@pytest.mark.parametrize('change',['script','dockerfile','metadata','runtime','grader','services','release'])
def test_v1_plan_requires_selected_author_inputs_and_prepared_grader(tmp_path,change):
    plan,_,config,job,found=prepared(tmp_path);lock=copy.deepcopy(plan['task_resources'])
    if change=='script':lock['trees']['pro_v1']['files'].pop(f'run_scripts/{ID}/parser.py')
    if change=='dockerfile':lock['trees']['pro_v1']['files'].pop(f'dockerfiles/base_dockerfile/{ID}/Dockerfile')
    if change=='metadata':lock['trees']['dependencies']['files']={}
    if change=='runtime':lock.pop('runtime')
    if change=='grader':lock['tasks'][ID]['images']['grading']={}
    if change=='services':lock['tasks'][ID]['images']['services']={'other':{'id':GRADER}}
    if change=='release':lock['release']='other'
    assert pro_v1.requirements(config,[found],lock)


@pytest.mark.parametrize('mode',['pass','fail','empty-patch','missing-output','timeout','wrong-base','drift','malformed-output'])
def test_author_main_keeps_verdict_and_isolated_owned_io_and_invalid_infra(tmp_path,mode):
    root,row=data(tmp_path);adapter=benchmarks.get('swe-bench-pro');found=adapter.task_instances(root)[0]
    req=swe_request(tmp_path,'pro-v1');req['task']=found;req['official']=str(tmp_path/'official')
    source=Path(req['official'])/'pro_v1';source.mkdir(parents=True)
    client=Client();agent=swe_containers.Owner(req,client,'agent','daemon');agent.create();agent.record['base_commit']=COMMIT;agent.cleanup()
    owner=swe_containers.Owner(req,client,'verifier','daemon')
    prediction=dict(instance_id=ID,model_name_or_path=req['model'],model_patch='' if mode=='empty-patch' else 'agent patch')
    adapter_prediction=tmp_path/'predictions.jsonl';adapter_prediction.write_text(json.dumps(prediction)+'\n', encoding='utf-8')
    sample=pro_v1.instance(found);module=SimpleNamespace();events=[]
    module.docker=SimpleNamespace(from_env=lambda:pytest.fail('author contacted unbound Docker'))
    module.get_dockerhub_image_uri=lambda *a:pytest.fail('author requested a registry image')
    module.py_platform=SimpleNamespace(machine=lambda:'host-arch')
    module.parse_args=lambda:pytest.fail('author parsed host argv')
    output=dict(tests=[{'name':'regression','status':'PASSED'},{'name':'existing','status':'PASSED'}])
    if mode=='malformed-output':output=dict(tests=[{'name':'regression','status':None}])
    original_create=client.create
    def create(**options):
        container=original_create(**options);original_exec=container.exec_run
        def execute(args,**kwargs):
            result=original_exec(args,**kwargs)
            if args[0]=='timeout':
                events.append('author-entryscript')
                workspace=Path(next(iter(options['volumes'])))
                if mode!='missing-output':(workspace/'output.json').write_text(json.dumps(output), encoding='utf-8')
                if mode=='timeout':result.exit_code=124
            return result
        container.exec_run=execute;return container
    client.containers.create=create
    if mode=='wrong-base':client.commit='e'*40
    def author_eval(patch_text,sample,output_dir,dockerhub_username,scripts_dir,**kwargs):
        events.append(('author-patch',patch_text));folder=Path(output_dir)/ID;workspace=folder/'workspace';workspace.mkdir(parents=True)
        (workspace/'entryscript.sh').write_text('author entryscript', encoding='utf-8');image=module.get_dockerhub_image_uri(ID,dockerhub_username,sample['repo'])
        bound=module.docker.from_env();bound.images.pull(image)
        options=dict(volumes={str(workspace):{'bind':'/workspace','mode':'rw'}},detach=True,remove=True,entrypoint='/bin/bash',
            command=['-c','bash /workspace/entryscript.sh'],network_mode='none')
        if mode=='drift':options['volumes']['/foreign']={'bind':'/secret','mode':'ro'}
        try:
            container=bound.containers.run(image,**options);container.wait()
            if not (workspace/'output.json').exists():return None
            result=json.loads((workspace/'output.json').read_text(encoding='utf-8'));(folder/'_output.json').write_text(json.dumps(result), encoding='utf-8');return result
        except Exception:return None  # The real author swallows Docker failures.
    module.eval_with_docker=author_eval
    def main():
        args=module.parse_args();selected=json.loads(Path(args.raw_sample_path).read_text(encoding='utf-8'));patch=json.loads(Path(args.patch_path).read_text(encoding='utf-8'))[0]
        try:
            result=module.eval_with_docker(patch['model_patch'],selected,args.output_dir,args.dockerhub_username,args.scripts_dir,
                prefix='',redo=args.redo,block_network=args.block_network,docker_platform=args.docker_platform)
            verdict=mode!='fail' and result is not None
        except Exception:verdict=False
        (Path(args.output_dir)/'eval_results.json').write_text(json.dumps({ID:verdict}), encoding='utf-8')
    module.main=main;cwd=Path.cwd()
    if mode in ('missing-output','timeout','wrong-base','drift','malformed-output'):
        with pytest.raises(RuntimeError,match='infrastructure'):pro_v1_grading.grade(req,module,client,sample,owner,agent,prediction)
        assert not (tmp_path/'official-logs/pro-v1-grade.json').exists()
    else:
        report=pro_v1_grading.grade(req,module,client,sample,owner,agent,prediction)
        grade=adapter.read_grade(found,report);assert grade['resolved']==(mode!='fail')
        assert grade['authoritative_phase']=='fresh_regrade' and not grade['published_protocol_reproduced']
        assert ('author-patch',prediction['model_patch']) in events
        options=[event[1] for event in client.events if event[0]=='create'][-1]
        assert options['image']==GRADER and options['network_mode']=='none' and list(options['volumes'])==[str(tmp_path/'official-logs'/ID/'workspace')]
        assert options['entrypoint']==[] and options['working_dir']=='/app'
        report.write_bytes(report.read_bytes()+b' ');assert adapter.read_grade(found,report)['resolved'] is None
    assert owner.record['cleaned'] and not client.live and Path.cwd()==cwd


@pytest.mark.parametrize('evidence',[None,[]])
def test_raw_v1_score_without_independent_evidence_stays_unknown(tmp_path,evidence):
    root,_=data(tmp_path);found=benchmarks.get('swe-bench-pro').task_instances(root)[0]
    report=tmp_path/'eval_results.json';report.write_text(json.dumps({ID:True}), encoding='utf-8')
    if evidence is not None:(tmp_path/'pro-v1-grade.json').write_text(json.dumps(evidence), encoding='utf-8')
    assert benchmarks.get('swe-bench-pro').read_grade(found,report)['resolved'] is None
