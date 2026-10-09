"""Portable task contract and real dataset schema; all execution fixtures are synthetic."""
import copy, csv, hashlib, json
from graphlib import CycleError
from pathlib import Path
import pytest
from ctxpress import benchmarks
from ctxpress.harness.jobs import plan as eval_plan, task
from ctxpress.benchmarks.milestone.data import itinerary
from test_eval_inputs import inputs


def dataset(tmp_path):
    root = tmp_path/'sample_repo_v1_v2'; root.mkdir()
    (root/'metadata.json').write_text(json.dumps(dict(repo_name='sample/repo',base_tag='v1')), encoding='utf-8')
    (root/'milestones.csv').write_text('id,title\nM1,first\nM2,second\nM3,unselected\n', encoding='utf-8')
    (root/'selected_milestone_ids.txt').write_text('M1\nM2\n', encoding='utf-8')
    (root/'non-graded_milestone_ids.txt').write_text('M1\n', encoding='utf-8')
    (root/'dependencies.csv').write_text('source_id,target_id\nM1,M2\nM2,M3\n', encoding='utf-8')
    (root/'additional_dependencies.csv').write_text('source_id,target_id\n# direction note,\n', encoding='utf-8')
    for mid in ('M1','M2'):
        p=root/'srs'/mid; p.mkdir(parents=True);(p/'SRS.md').write_text('task instructions', encoding='utf-8')
    p=root/'dockerfiles/M2';p.mkdir(parents=True);(p/'test_config.json').write_text('{}', encoding='utf-8')
    p=root/'test_results/M2';p.mkdir(parents=True);(p/'M2_classification.json').write_text('{}', encoding='utf-8')
    return root


def seal(plan):
    plan['sha256'] = hashlib.sha256(eval_plan.canonical({k:v for k,v in plan.items() if k!='sha256'}).encode()).hexdigest()
    return plan


def test_itinerary_is_a_task_start_instance_without_recorded_coordinates(tmp_path):
    root=dataset(tmp_path); found=itinerary(root)
    assert found['start_mode']=='task_start' and found['id']==root.name
    assert found['initial_state']['repo']=='sample/repo' and 'recorded_boundary' not in found['initial_state']
    assert found['evaluation']['active_milestones']==['M1','M2']
    assert found['evaluation']['graded_milestones']==['M2']
    assert found['evaluation']['dependencies']==[['M1','M2']]
    assert [Path(item['path']).name for item in found['inputs'] if item['role']=='task']==['SRS.md','SRS.md']
    assert all('/test_results/' not in item['path'].replace('\\','/') for item in found['inputs'] if item['role']=='task')
    assert all(eval_plan.file_sha256(item['path'])==item['sha256'] for item in found['inputs'])


@pytest.mark.parametrize('change',['duplicate-selection','unknown-selection','unknown-edge','cycle','missing-srs','missing-classification'])
def test_incomplete_or_ambiguous_itinerary_cannot_become_a_task(tmp_path,change):
    root=dataset(tmp_path)
    if change=='duplicate-selection':(root/'selected_milestone_ids.txt').write_text('M1\nM1\n', encoding='utf-8')
    if change=='unknown-selection':(root/'selected_milestone_ids.txt').write_text('absent\n', encoding='utf-8')
    if change=='unknown-edge':(root/'dependencies.csv').write_text('source_id,target_id\nM0,M1\n', encoding='utf-8')
    if change=='cycle':(root/'dependencies.csv').write_text('source_id,target_id\nM1,M2\nM2,M1\n', encoding='utf-8')
    if change=='missing-srs':(root/'srs/M1/SRS.md').unlink()
    if change=='missing-classification':(root/'test_results/M2/M2_classification.json').unlink()
    with pytest.raises((ValueError,FileNotFoundError,CycleError)):
        itinerary(root)


def test_from_job_rejects_conflicting_start_states(tmp_path):
    plan=eval_plan.compile_plan(inputs(tmp_path)); job=copy.deepcopy(plan['jobs'][0])
    assert task.from_job(job,'swe-milestone')['initial_state']['recorded_boundary']==job['boundary']
    job['task']['initial_state']['recorded_boundary']['j']+=1
    with pytest.raises(ValueError,match='disagree'):
        task.from_job(job,'swe-milestone')


def test_start_state_cannot_claim_a_recorded_history_is_a_fresh_task(tmp_path):
    found=itinerary(dataset(tmp_path));found['initial_state']['recorded_boundary']={'n':3,'j':14}
    with pytest.raises(ValueError,match='cannot contain'):
        task.validate(found)


def test_task_input_hashes_remain_bound_even_if_plan_is_resealed(tmp_path):
    plan=eval_plan.compile_plan(inputs(tmp_path))
    plan['jobs'][0]['task']['inputs'][0]['sha256']='0'*64
    with pytest.raises(ValueError,match='not bound'):
        eval_plan.verify(seal(plan),check_inputs=False)


def test_start_mode_mismatch_is_rejected_without_execution(tmp_path):
    plan=eval_plan.compile_plan(inputs(tmp_path));plan['config']['start_mode']='task_start'
    with pytest.raises(ValueError,match='start mode'):
        eval_plan.verify(seal(plan),check_inputs=False)


def test_snapshot_path_remap_leaves_identity_and_initial_state_unchanged(tmp_path):
    found=itinerary(dataset(tmp_path));original=copy.deepcopy(found)
    paths={item['path']:str(tmp_path/'frozen'/str(i)) for i,item in enumerate(found['inputs'])}
    moved=task.remap(found,paths)
    assert found==original and moved['initial_state']==found['initial_state']
    assert moved['id']==found['id'] and [item['path'] for item in moved['inputs']]==list(paths.values())


def test_legacy_job_keeps_recorded_identity_without_inventing_file_inputs():
    point=dict(id='legacy',n=0,j=1,context_tokens=150000)
    found=task.from_job(dict(boundary=point),'swe-milestone')
    assert found['initial_state']['recorded_boundary']==point and found['inputs']==[]


def test_task_start_discovery_distinguishes_code_support_from_real_run(tmp_path):
    dataset(tmp_path)
    adapter=benchmarks.get()
    found=adapter.task_instances(tmp_path)
    assert len(found)==1 and found[0]['start_mode']=='task_start'
    description = adapter.task_start_description()
    assert description['execution_supported'] and description['official_grading_supported']
    assert description['from_task_start'] and not description['real_run_verified']


def test_configured_post_snapshot_script_is_a_grading_input(tmp_path):
    root = dataset(tmp_path)
    (root/'dockerfiles/evaluation_post_snapshot.sh').write_text('#!/bin/bash\n', encoding='utf-8')
    (tmp_path/'config').mkdir()
    (tmp_path/'config'/(root.name+'.yaml')).write_text(
        'repo_src_dirs: [src]\n# Evaluator-owned hook\nevaluation_post_snapshot_script: "dockerfiles/evaluation_post_snapshot.sh"\n',
        encoding='utf-8')
    grading = [Path(item['path']).as_posix() for item in itinerary(root)['inputs'] if item['role'] == 'grading']
    assert any(path.endswith('/dockerfiles/evaluation_post_snapshot.sh') for path in grading)
    (tmp_path/'config'/(root.name+'.yaml')).write_text('repo_src_dirs: [src]\n', encoding='utf-8')
    assert not any(Path(item['path']).name == 'evaluation_post_snapshot.sh' for item in itinerary(root)['inputs'])
