"""Native itinerary layout/config contracts; no models, containers or installs."""
import copy, json, shutil, sys
from pathlib import Path
from types import ModuleType,SimpleNamespace
import pytest
from ctxpress import benchmarks
from ctxpress.benchmarks.milestone import data as milestone_data, protocol as milestone_protocol
from ctxpress.harness.jobs import inputs as eval_inputs, plan as eval_plan, queue as evaluation, task
from ctxpress.benchmarks.milestone import native as milestone_native
from test_task_instances import dataset
from test_task_start_plan import config


def enriched(tmp_path):
    cfg=config(tmp_path);root=Path(cfg['environment']['data'])
    for name,text in [('dockerfiles/M2/apply_patches.sh','author patch application'),
                      ('dockerfiles/M2/fixtures/input.txt','author fixture'),
                      ('scripts/post_snapshot.py','private grading script'),
                      ('tests/helper.py','private test helper'),('e2e_config.yaml','dag_unlock:\n  early_unblock: true\n')]:
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text, encoding='utf-8')
    (root/'dockerfiles/M2/empty-fixture').mkdir()
    sibling=root.parent/'config';sibling.mkdir();(sibling/(root.name+'.yaml')).write_text('evaluation_post_snapshot_script: scripts/post_snapshot.py\n', encoding='utf-8')
    (root/'e2e_trial/old/evaluation').mkdir(parents=True);(root/'e2e_trial/old/evaluation/gold.json').write_text('unselected old results', encoding='utf-8')
    # Refresh binding after extending the actual selected input set.
    resource=Path(cfg['environment']['resources']);lock=json.loads(resource.read_text(encoding='utf-8'));found=milestone_data.itinerary(root)
    from ctxpress.harness.jobs import resources as task_resources
    lock.pop('sha256');lock['tasks'][found['id']]['task_sha256']=task_resources.task_digest(found)
    resource.write_text(json.dumps(task_resources.seal(lock)), encoding='utf-8')
    return cfg,root


def test_native_grading_assets_and_config_are_selected_without_old_trials_or_agent_visible_tests(tmp_path):
    _,root=enriched(tmp_path);found=milestone_data.itinerary(root)
    roles={Path(item['path']).name:item['role'] for item in found['inputs']}
    assert roles['apply_patches.sh']==roles['input.txt']==roles['post_snapshot.py']==roles[root.name+'.yaml']=='grading'
    assert roles['e2e_config.yaml']=='runtime' and not any('/e2e_trial/' in item['path'].replace('\\','/') for item in found['inputs'])
    assert 'dockerfiles/M2/empty-fixture' in found['initial_state']['input_directories']
    assert found['evaluation']['graded_milestones']==['M2'] and found['evaluation']['active_milestones']==['M1','M2']


def test_testcontainers_plan_requires_captured_services_and_keeps_legacy_flag_semantics(tmp_path):
    cfg,root=enriched(tmp_path);path=root/'dockerfiles/M2/test_config.json'
    path.write_text(json.dumps([{'name':'e2e','requires_docker_socket':True}]), encoding='utf-8')
    found=milestone_data.itinerary(root);lock=json.loads(Path(cfg['environment']['resources']).read_text(encoding='utf-8'))
    assert milestone_protocol.service_milestones(found)==['M2']
    assert any('captured service_images' in message for message in milestone_protocol.requirements(cfg,[found],lock))
    lock['tasks'][found['id']]['images']['services']={'database':{'id':'sha256:'+'a'*64,'reference':'redis:7'}}
    assert not any('service' in message.lower() for message in milestone_protocol.requirements(cfg,[found],lock))
    path.write_text(json.dumps({'requires_docker_socket':True}), encoding='utf-8')
    assert milestone_protocol.service_milestones(milestone_data.itinerary(root))==[]


def test_service_flags_cannot_silently_change_after_capture_or_use_string_booleans(tmp_path):
    _,root=enriched(tmp_path);path=root/'dockerfiles/M2/test_config.json';found=milestone_data.itinerary(root)
    path.write_text('[{"requires_docker_socket":true}]', encoding='utf-8')
    with pytest.raises(ValueError,match='changed'):milestone_protocol.service_milestones(found)
    path.write_text('[{"requires_docker_socket":"false"}]', encoding='utf-8')
    with pytest.raises(ValueError,match='invalid'):milestone_protocol.service_milestones(milestone_data.itinerary(root))


def test_sibling_config_freezes_under_its_authoritative_native_location_without_expanding_tasks(tmp_path):
    cfg,root=enriched(tmp_path);plan=eval_plan.compile_plan(cfg)
    assert plan['config']['environment']['data']==str(root)
    assert plan['input_trees']['dataset-config']['tree']['files'].keys()=={root.name+'.yaml'}
    assert {job['task']['id'] for job in plan['jobs']}=={root.name}
    directory=evaluation.prepare(plan,tmp_path/'run');effective,paths=eval_inputs.execution(plan,directory/'inputs')
    moved=task.remap(plan['jobs'][0]['task'],paths)
    shutil.rmtree(root);shutil.rmtree(tmp_path/'config')
    staging=tmp_path/'native-staging';workspace=milestone_native.materialize(moved,staging)
    assert workspace.name==moved['id']
    assert (workspace.parent/'config'/(moved['id']+'.yaml')).read_text(encoding='utf-8').startswith('evaluation_post_snapshot_script:')
    assert (workspace/'dockerfiles/M2/empty-fixture').is_dir()
    assert (workspace/'scripts/post_snapshot.py').read_text(encoding='utf-8')=='private grading script'
    assert not (workspace/'e2e_trial').exists()
    assert moved['initial_state']['workspace']==str(root) and not root.exists()
    assert plan['benchmark']['execution_supported'] and not plan['benchmark']['real_run_verified']


def test_single_workspace_copy_restores_identity_even_without_sibling_config(tmp_path):
    root=dataset(tmp_path);found=milestone_data.itinerary(root)
    copied=tmp_path/'task-data';shutil.copytree(root,copied)
    moved=task.remap(found,{item['path']:str(copied/Path(item['path']).relative_to(root)) for item in found['inputs']})
    shutil.rmtree(root)
    workspace=milestone_native.materialize(moved,tmp_path/'staging')
    assert workspace.name==found['id'] and (workspace/'metadata.json').is_file()
    assert workspace.name!=copied.name
    with pytest.raises(ValueError,match='fresh'):milestone_native.materialize(moved,tmp_path/'staging')


@pytest.mark.parametrize('change',['changed-file','duplicate-target','outside','unsafe-directory'])
def test_materialization_rejects_changes_before_they_can_enter_a_native_trial(tmp_path,change):
    found=milestone_data.itinerary(dataset(tmp_path));target=tmp_path/'staging'
    if change=='changed-file':Path(found['inputs'][1]['path']).write_text('drift', encoding='utf-8')
    if change=='duplicate-target':found['inputs'].append(dict(found['inputs'][0]))
    if change=='outside':
        outside=tmp_path/'outside.txt';outside.write_text('outside', encoding='utf-8')
        found['inputs'].append(dict(role='grading',path=str(outside),sha256=eval_plan.file_sha256(outside)))
    if change=='unsafe-directory':found['initial_state']['input_directories']=['../outside']
    with pytest.raises(ValueError):milestone_native.materialize(found,target)
    assert not target.exists()


def contract_tree(root):
    for name,classes in milestone_protocol.CONTRACTS.items():
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
        text=''
        for klass,methods in classes.items():
            text+='class '+klass+':\n'
            for method,args in methods.items():text+='    def '+method+'(self'+''.join(', '+arg for arg in sorted(args))+'):\n        pass\n'
        path.write_text(text, encoding='utf-8')
    for name,functions in milestone_protocol.FUNCTION_CONTRACTS.items():
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
        prior=path.read_text(encoding='utf-8') if path.exists() else ''
        path.write_text(prior+''.join('def '+name+'('+', '.join(sorted(args))+'):\n    pass\n' for name,args in functions.items()), encoding='utf-8')
    return root


def test_contract_check_rejects_transitional_source_without_importing_or_running_it(tmp_path):
    root=contract_tree(tmp_path/'code')
    assert milestone_protocol.contract_errors(root)==[]
    path=root/'harness/e2e/orchestrator.py';text=path.read_text(encoding='utf-8')
    path.write_text(text.replace(', runtime_policy_binding',''), encoding='utf-8')
    assert any('E2EOrchestrator.__init__' in item for item in milestone_protocol.contract_errors(root))
    path.write_text('raise RuntimeError("must not import source")\n'+text, encoding='utf-8')
    assert milestone_protocol.contract_errors(root)==[]


def test_actual_native_api_preparation_uses_author_binders_and_preserves_all_milestones(tmp_path,monkeypatch):
    cfg,root=enriched(tmp_path);found=milestone_data.itinerary(root);code=tmp_path/'native-code'
    (code/'harness/e2e').mkdir(parents=True);events=[]
    metadata=json.loads((root/'metadata.json').read_text(encoding='utf-8'));metadata.update(repo_src_dirs=['src'],test_dirs=['tests'],exclude_patterns=[])
    (root/'metadata.json').write_text(json.dumps(metadata), encoding='utf-8');found=milestone_data.itinerary(root)
    def module(name):
        value=ModuleType('harness.e2e.'+name);value.__file__=str(code/'harness/e2e'/(name+'.py'))
        monkeypatch.setitem(sys.modules,value.__name__,value);return value
    package=ModuleType('harness');package.__path__=[];monkeypatch.setitem(sys.modules,'harness',package)
    package=ModuleType('harness.e2e');package.__path__=[];monkeypatch.setitem(sys.modules,'harness.e2e',package)
    runner=module('run_e2e');runner.load_workspace_metadata=lambda workspace,repo_config:json.loads((workspace/'metadata.json').read_text(encoding='utf-8'))
    configs=module('config');configs.E2EConfig=lambda path:SimpleNamespace(ignore_weak_dependencies=False)
    dag=module('dag')
    def construct(dependencies_csv,selected_ids_file,ignore_weak_dependencies,additional_dependencies_csv):
        events.append('author-dag');return SimpleNamespace(all_milestones=set(selected_ids_file.read_text(encoding='utf-8').split()))
    dag.DAGManager=construct
    repo=module('repo_config_binding');policy=module('runtime_policy_binding');evaluator=module('evaluator')
    def resolve_repo(identity,workspace,project_root):
        selected=workspace.parent/'config'/(identity+'.yaml');events.append(('repo',selected.read_text(encoding='utf-8')))
        return SimpleNamespace(config={'evaluation_post_snapshot_script':'scripts/post_snapshot.py'},sha256='a'*64)
    repo.resolve_repo_config=resolve_repo;repo.freeze_repo_config=lambda trial,resolved:resolved
    policy.resolve_runtime_policy=lambda identity,code:SimpleNamespace(mode='protected',sha256='b'*64)
    policy.freeze_runtime_policy=lambda trial,resolved:resolved
    policy.runtime_policy_coverage_errors=lambda resolved:[]
    evaluator.validate_workspace_filter_lists=lambda workspace:[]
    actual=milestone_native.prepare(found,code,tmp_path/'prepared')
    assert actual['dag'].all_milestones=={'M1','M2'}
    assert actual['audit']['graded_milestones']==['M2'] and actual['audit']['model_calls']==actual['audit']['containers_started']==0
    assert actual['audit']['repo_config_sha256']=='a'*64 and actual['audit']['runtime_policy_mode']=='protected'
    assert events[0][0]=='repo' and events[1]=='author-dag'
    assert not actual['audit']['execution_supported']


def test_empty_dependency_graph_keeps_selected_isolated_milestones_in_native_selection(tmp_path):
    root=dataset(tmp_path);(root/'dependencies.csv').write_text('source_id,target_id\n', encoding='utf-8')
    found=milestone_data.itinerary(root)
    assert found['evaluation']['active_milestones']==['M1','M2'] and found['evaluation']['dependencies']==[]
    assert {item['role'] for item in found['inputs']}=={'task','runtime','grading'}


@pytest.mark.parametrize('change',['graded-set','active-order','directory','role','file-hash','count'])
def test_frozen_task_cannot_change_itinerary_semantics_even_with_unchanged_instance_id(tmp_path,change):
    original=milestone_data.itinerary(dataset(tmp_path))
    moved=task.remap(original,{item['path']:str(tmp_path/'copy'/str(index)) for index,item in enumerate(original['inputs'])})
    task.verify_remap(original,moved)
    if change=='graded-set':moved['evaluation']['graded_milestones']=['M1']
    if change=='active-order':moved['evaluation']['active_milestones'].reverse()
    if change=='directory':moved['initial_state']['input_directories']=['other']
    if change=='role':moved['inputs'][0]['role']='task'
    if change=='file-hash':moved['inputs'][0]['sha256']='a'*64
    if change=='count':moved['inputs'].pop()
    with pytest.raises(ValueError,match='remapped task'):task.verify_remap(original,moved)
