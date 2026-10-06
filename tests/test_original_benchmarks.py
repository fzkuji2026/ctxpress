"""Official task schemas and artifacts; these are synthetic, not benchmark scores."""
import json, math, shutil
from pathlib import Path
import pytest
from ctxpress import benchmarks
from ctxpress.__main__ import main
from ctxpress.harness import eval_inputs, eval_plan, evaluation, task


def swe_data(tmp_path, jsonl=True):
    root=tmp_path/'swe';root.mkdir()
    row=dict(instance_id='owner__repo-1',repo='owner/repo',base_commit='a'*40,problem_statement='Fix the parser.',
             patch='secret gold patch',test_patch='secret verifier patch',FAIL_TO_PASS=['hidden_test'])
    path=root/('instances.jsonl' if jsonl else 'instances.json')
    path.write_text(json.dumps(row)+'\n' if jsonl else json.dumps([row]),encoding='utf-8')
    return root,row


def terminal_data(tmp_path):
    root=tmp_path/'tb';root.mkdir()
    directory=root/'task-one'; directory.mkdir()
    (directory/'instruction.md').write_text('Repair the terminal program.', encoding='utf-8')
    (directory/'task.toml').write_text('version = "1.0"\n[environment]\ndocker_image = "prepared:task-one"\n[verifier]\ntimeout_sec = 120\n', encoding='utf-8')
    for name,text in [('environment/Dockerfile','FROM prepared:task-one'),('environment/config/setup.sh','setup'),
                      ('tests/test.sh','verify'),('tests/parser.py','hidden verifier'),('solution/solve.sh','reference solution')]:
        path=directory/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text, encoding='utf-8')
    return root


@pytest.mark.parametrize('name',['swe-bench','swe-bench-lite','swe-bench-verified'])
@pytest.mark.parametrize('jsonl',[True,False])
def test_swe_data_is_not_an_agent_visible_gold_patch(tmp_path,name,jsonl):
    root,_=swe_data(tmp_path,jsonl); adapter=benchmarks.get(name)
    found=adapter.task_instances(root);assert len(found)==1
    assert adapter.agent_instruction(found[0])=='Fix the parser.'
    assert found[0]['initial_state']==dict(repo='owner/repo',base_commit='a'*40,problem_statement='Fix the parser.')
    assert found[0]['inputs'][0]['role']=='grading'
    assert adapter.describe()['execution_supported'] and adapter.describe()['official_grading_supported']
    assert not adapter.describe()['real_run_verified']


@pytest.mark.parametrize('field,value',[('base_commit','main'),('base_commit',True),('problem_statement',''),
                                        ('repo','unknown'),('instance_id','../escape')])
def test_swe_instance_requires_actual_original_start_state(tmp_path,field,value):
    root,row=swe_data(tmp_path);row[field]=value;(root/'instances.jsonl').write_text(json.dumps(row), encoding='utf-8')
    with pytest.raises(ValueError):benchmarks.get('swe-bench').task_instances(root)


def test_duplicate_ids_and_competing_dataset_files_do_not_silently_select(tmp_path):
    root,row=swe_data(tmp_path);path=root/'instances.jsonl';path.write_text((json.dumps(row)+'\n')*2, encoding='utf-8')
    with pytest.raises(ValueError,match='unique'):benchmarks.get('swe-bench').task_instances(root)
    (root/'instances.json').write_text(json.dumps([row]), encoding='utf-8')
    with pytest.raises(ValueError,match='exactly one'):benchmarks.get('swe-bench').task_instances(root)


def test_lite_cannot_relabel_a_declared_verified_dataset(tmp_path):
    root,_=swe_data(tmp_path)
    (root/'dataset_manifest.json').write_text(json.dumps(dict(dataset='SWE-bench/SWE-bench_Verified',revision='frozen-source-revision')), encoding='utf-8')
    assert benchmarks.get('swe-bench-verified').task_instances(root)[0]['evaluation']['dataset']['revision']=='frozen-source-revision'
    with pytest.raises(ValueError,match='provenance'):benchmarks.get('swe-bench-lite').task_instances(root)


def test_official_prediction_jsonl_preserves_patch_newlines_and_empty_outputs(tmp_path):
    root,_=swe_data(tmp_path);adapter=benchmarks.get('swe-bench-verified');found=adapter.task_instances(root)[0]
    patch='diff --git a/x b/x\n+fixed\n'
    path=tmp_path/'predictions.jsonl';item=adapter.prediction(found,'fixture-model',patch)
    adapter.write_predictions(path,[item])
    assert json.loads(path.read_text(encoding='utf-8'))==dict(instance_id=found['id'],model_name_or_path='fixture-model',model_patch=patch)
    item['model_patch']='';adapter.write_predictions(path,[item]);assert json.loads(path.read_text(encoding='utf-8'))['model_patch']==''
    before=path.read_bytes()
    with pytest.raises(ValueError,match='duplicate'):adapter.write_predictions(path,[item,item])
    assert path.read_bytes()==before


@pytest.mark.parametrize('outcome',[True,False])
def test_official_swe_grade_binds_only_the_selected_instance(tmp_path,outcome):
    root,_=swe_data(tmp_path);adapter=benchmarks.get('swe-bench');found=adapter.task_instances(root)[0]
    path=tmp_path/'report.json';path.write_text(json.dumps({found['id']:dict(resolved=outcome,patch_successfully_applied=True)}), encoding='utf-8')
    grade=adapter.read_grade(found,path)
    assert grade['resolved'] is outcome and grade['infra_invalid'] is False
    assert grade['report_sha256']==eval_plan.file_sha256(path)
    path.write_text(json.dumps({'another-instance':dict(resolved=True)}), encoding='utf-8')
    assert adapter.read_grade(found,path)['resolved'] is None


def test_official_infrastructure_classification_is_not_counted_as_task_quality(tmp_path):
    root,_=swe_data(tmp_path);adapter=benchmarks.get('swe-bench');found=adapter.task_instances(root)[0]
    path=tmp_path/'report.json';path.write_text(json.dumps({found['id']:dict(resolved=False,infra_failure=True,infra_failure_reason='browser missing')}), encoding='utf-8')
    grade=adapter.read_grade(found,path);assert grade['infra_invalid'] is True
    assert grade['raw_instance_report']['infra_failure_reason']=='browser missing'


def test_terminal_layout_retains_setup_and_verifier_files_but_excludes_oracle_solution(tmp_path):
    root=terminal_data(tmp_path);adapter=benchmarks.get('terminal-bench');found=adapter.task_instances(root)[0]
    assert adapter.agent_instruction(found)=='Repair the terminal program.'
    roles={Path(item['path']).relative_to(root).as_posix():item['role'] for item in found['inputs']}
    assert roles['task-one/environment/config/setup.sh']=='runtime'
    assert roles['task-one/tests/parser.py']=='grading'
    assert roles['task-one/instruction.md']=='task'
    assert not any('/solution/' in name for name in roles)


def test_nested_science_release_keeps_official_task_identity_and_paths(tmp_path):
    root=terminal_data(tmp_path)
    nested=root/'life-sciences'/'biology'/'task-one'
    nested.parent.mkdir(parents=True)
    (root/'task-one').rename(nested)
    with (nested/'task.toml').open('a', encoding='utf-8') as output:
        output.write('[task]\nname = "terminal-bench-science/task-one"\n')
    (root/'dataset_manifest.json').write_text(json.dumps(dict(dataset='terminal-bench-science',revision='v0.1.0@frozen-commit')), encoding='utf-8')
    adapter=benchmarks.get('terminal-bench-science');found=adapter.task_instances(root)
    assert [item['id'] for item in found]==['task-one']
    assert adapter.official_task_name(found[0])=='terminal-bench-science/task-one'
    assert found[0]['initial_state']['task_directory']==str(nested)
    assert 'life-sciences/biology/task-one/environment/config' in adapter.data_directories(found,root)
    assert adapter.agent_instruction(found[0])=='Repair the terminal program.'
    assert not any('/solution/' in item['path'] for item in found[0]['inputs'])


def test_nested_release_rejects_colliding_task_ids(tmp_path):
    root=terminal_data(tmp_path)
    shutil.copytree(root/'task-one',root/'another-domain'/'task-one')
    with pytest.raises(ValueError,match='duplicate Harbor task IDs'):
        benchmarks.get('terminal-bench').task_instances(root)


@pytest.mark.parametrize('change',['missing-verifier','old-format','empty-instruction','bad-config'])
def test_terminal_catalog_does_not_accept_ungradable_or_legacy_tasks(tmp_path,change):
    root=terminal_data(tmp_path);directory=root/'task-one'
    if change=='missing-verifier':(directory/'tests/test.sh').unlink()
    if change=='old-format':(directory/'task.toml').unlink();(directory/'task.yaml').write_text('instruction: old format', encoding='utf-8')
    if change=='empty-instruction':(directory/'instruction.md').write_text('', encoding='utf-8')
    if change=='bad-config':(directory/'task.toml').write_text('environment = "bad"', encoding='utf-8')
    with pytest.raises(ValueError):benchmarks.get('terminal-bench').task_instances(root)


@pytest.mark.parametrize('rewards',[{'reward':1.0},{'reward':0.0},{'accuracy':0.75,'other':2}])
def test_harbor_numeric_reward_is_not_fabricated_as_a_solved_boolean(tmp_path,rewards):
    root=terminal_data(tmp_path);adapter=benchmarks.get('terminal-bench');found=adapter.task_instances(root)[0]
    path=tmp_path/'result.json';path.write_text(json.dumps(dict(task_name=found['id'],trial_name='synthetic',verifier_result={'rewards':rewards},exception_info=None)), encoding='utf-8')
    grade=adapter.read_grade(found,path)
    assert grade['infra_invalid'] is False and grade['valid_rewards'] is True
    assert grade['rewards']==rewards and grade['resolved'] is None


@pytest.mark.parametrize('change',['missing','wrong-task','exception','nan','boolean','empty'])
def test_invalid_harbor_outcomes_never_produce_quality_scores(tmp_path,change):
    root=terminal_data(tmp_path);adapter=benchmarks.get('terminal-bench');found=adapter.task_instances(root)[0]
    raw=dict(task_name=found['id'],verifier_result={'rewards':{'reward':1}},exception_info=None)
    path=tmp_path/'result.json'
    if change=='wrong-task':raw['task_name']='another'
    if change=='exception':raw['exception_info']={'exception_type':'fixture failure'}
    if change=='nan':raw['verifier_result']['rewards']={'reward':float('nan')}
    if change=='boolean':raw['verifier_result']['rewards']={'reward':True}
    if change=='empty':raw['verifier_result']['rewards']={}
    if change!='missing':path.write_text(json.dumps(raw), encoding='utf-8')
    grade=adapter.read_grade(found,path)
    assert grade['resolved'] is None and grade['infra_invalid'] is None and grade['failure_kind']=='grading_error' and not grade.get('valid_rewards')


@pytest.mark.parametrize('benchmark',['swe-bench-verified','terminal-bench'])
def test_common_task_start_plan_freezes_each_benchmark_and_remaps_visible_input(tmp_path,benchmark):
    root=swe_data(tmp_path)[0] if benchmark.startswith('swe-') else terminal_data(tmp_path)
    adapter=benchmarks.get(benchmark);found=adapter.task_instances(root)[0]
    binary=tmp_path/'bin';binary.mkdir();(binary/'codex').write_bytes(b'fixture binary')
    cfg=dict(schema='ctxpress.eval',version=1,benchmark=benchmark,start_mode='task_start',scope='benchmark',
        model='fixture',backend='codex_docker',tasks=[found['id']],environment=dict(data=str(root),bindir=str(binary)),methods=[{'class':'NoCompaction'}])
    plan=eval_plan.compile_plan(cfg);directory=evaluation.prepare(plan,tmp_path/'run')
    shutil.rmtree(root);(binary/'codex').unlink()
    restored=evaluation._verified_plan(directory);_,paths=eval_inputs.execution(restored,directory/'inputs')
    frozen=task.remap(restored['jobs'][0]['task'],paths)
    instruction=adapter.agent_instruction(frozen)
    assert 'secret' not in instruction and 'reference' not in instruction and 'hidden verifier' not in instruction
    assert plan['benchmark']['name']==benchmark and 'boundary' not in plan['jobs'][0]


def test_cli_discovery_of_harbor_tasks_requires_explicit_fresh_mode(tmp_path,capsys):
    root=terminal_data(tmp_path)
    main(['eval','tasks','--benchmark','terminal-bench','--start-mode','task_start','--data',str(root)])
    output=json.loads(capsys.readouterr().out)
    assert output['count']==1 and output['experiments_started']==0
    assert output['benchmark']['catalog_supported'] and output['benchmark']['execution_supported']
    assert not output['benchmark']['real_run_verified']
