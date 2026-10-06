"""Fresh plans use no recorded coordinates and survive removal of mutable sources."""
import copy, hashlib, json, shutil, subprocess
from pathlib import Path
import pytest
from ctxpress.benchmarks.milestone.data import itinerary
from ctxpress.harness.jobs import inputs as eval_inputs, plan as eval_plan, trees as eval_trees, queue as evaluation, resources as task_resources
from test_task_instances import dataset, seal


def config(tmp_path, resources=True):
    root = dataset(tmp_path)
    binary = tmp_path / 'bin'; binary.mkdir(); (binary/'codex').write_bytes(b'offline binary fixture')
    cfg = dict(schema='ctxpress.eval', version=1, benchmark='swe-milestone', start_mode='task_start',
               scope='benchmark', model='fixture', backend='codex_docker', tasks=[root.name],
               environment=dict(data=str(root), bindir=str(binary)), methods=[{'class':'NoCompaction'}])
    if resources:
        official = tmp_path/'official'; (official/'harness').mkdir(parents=True)
        (official/'harness/runner.py').write_text('# offline official source fixture\n', encoding='utf-8')
        descriptor = eval_trees.capture(official, ['harness/runner.py'], folder='unused')['tree']
        task = itinerary(root)
        lock = task_resources.seal(dict(schema=task_resources.SCHEMA, version=1, benchmark='swe-milestone',
            release='fixture-v1', trees={'code':descriptor}, tasks={task['id']:dict(task_sha256=task_resources.task_digest(task),
            images=dict(agent={'id':'sha256:'+'1'*64}, grading={'M2':{'id':'sha256:'+'2'*64}}))}))
        path=tmp_path/'resources.json'; path.write_text(json.dumps(lock), encoding='utf-8')
        cfg['environment']['resources']=str(path)
    return cfg


def test_task_start_plan_never_invents_boundary_coordinates(tmp_path):
    cfg = config(tmp_path); original=copy.deepcopy(cfg)
    plan=eval_plan.compile_plan(cfg)
    assert cfg==original and 'boundaries' not in plan['config']
    assert len(plan['jobs'])==1 and 'boundary' not in plan['jobs'][0]
    assert plan['jobs'][0]['task']['start_mode']=='task_start'
    assert plan['jobs'][0]['resources']['images']['agent']['id']=='sha256:'+'1'*64
    assert plan['benchmark']['execution_supported'] and not plan['benchmark']['real_run_verified']
    assert any('native_data_version=true' in item for item in plan['missing_environment_files'])
    eval_plan.verify(plan)


def test_runtime_and_data_trees_are_frozen_before_background_execution(tmp_path):
    cfg=config(tmp_path); plan=eval_plan.compile_plan(cfg)
    directory=evaluation.prepare(plan,tmp_path/'run')
    original_inputs={name:Path(name).read_bytes() for name in plan['artifacts']}
    shutil.rmtree(Path(cfg['environment']['data']))
    shutil.rmtree(tmp_path/'official'); shutil.rmtree(tmp_path/'bin')
    Path(cfg['environment']['resources']).unlink()
    restored=evaluation._verified_plan(directory)
    effective,paths=eval_inputs.execution(restored,directory/'inputs')
    assert all(Path(paths[name]).read_bytes()==value for name,value in original_inputs.items())
    assert (Path(effective['environment']['data'])/'srs/M2/SRS.md').read_text(encoding='utf-8')=='task instructions'
    assert (Path(effective['environment']['official_root'])/'code/harness/runner.py').is_file()
    assert '/inputs/' in effective['environment']['resources'].replace('\\','/')
    assert restored['sha256']==plan['sha256']


@pytest.mark.linux_only
def test_package_metadata_sibling_survives_freezing_and_still_checks_permissions(tmp_path):
    cfg = config(tmp_path)
    official = tmp_path/'official'
    names = ['swebench/harness/run.py', 'swebench.egg-info/PKG-INFO']
    for name in names:
        path = official/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('original ' + name, encoding='utf-8')
        path.chmod(0o755)
    resource_path = Path(cfg['environment']['resources'])
    lock = json.loads(resource_path.read_text(encoding='utf-8'))
    lock['trees']['package'] = eval_trees.capture(official, names, folder='unused')['tree']
    # Trees need distinct source roots, so the initial one is replaced.
    del lock['trees']['code']
    resource_path.write_text(json.dumps(task_resources.seal({k:v for k,v in lock.items() if k!='sha256'})), encoding='utf-8')
    plan = eval_plan.compile_plan(cfg)
    directory = evaluation.prepare(plan, tmp_path/'run')
    shutil.rmtree(official)
    evaluation._verified_plan(directory)
    copied = directory/'inputs/official-inputs/package/swebench/harness/run.py'
    copied.chmod(0o644)
    if copied.stat().st_mode & 0o111:
        pytest.skip('filesystem cannot change executable bits')
    with pytest.raises(ValueError, match='frozen benchmark input tree changed'):
        evaluation._verified_plan(directory)


@pytest.mark.parametrize('change',['file','extra-file','extra-directory','symlink'])
def test_frozen_tree_changes_are_refused_without_source_fallback(tmp_path,change):
    plan=eval_plan.compile_plan(config(tmp_path)); directory=evaluation.prepare(plan,tmp_path/'run')
    copied=directory/'inputs/task-data'
    if change=='file': (copied/'srs/M2/SRS.md').write_text('changed', encoding='utf-8')
    if change=='extra-file': (copied/'undeclared.txt').write_text('new', encoding='utf-8')
    if change=='extra-directory': (copied/'undeclared').mkdir()
    if change=='symlink':
        try: (copied/'link').symlink_to(copied/'srs/M2/SRS.md')
        except OSError: pytest.skip('platform cannot create symlink')
    with pytest.raises(ValueError): evaluation._verified_plan(directory)


def test_partial_plan_cannot_launch_an_unimplemented_executor(tmp_path,monkeypatch):
    cfg=config(tmp_path,resources=False);plan=eval_plan.compile_plan(cfg)
    assert len(plan['missing_environment_files'])>=2
    path=tmp_path/'plan.json';eval_plan.atomic_json(path,plan)
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**kw: pytest.fail('launched incomplete native executor'))
    with pytest.raises(ValueError,match='incomplete'): evaluation.start(path,tmp_path/'run',background=True)
    assert not (tmp_path/'run').exists()


@pytest.mark.parametrize('change',['formal','grade-off','unknown-task','duplicate-task','conflicting-boundaries'])
def test_fresh_task_validation_cannot_bypass_selection_or_quality_protocol(tmp_path,change):
    cfg=config(tmp_path)
    if change=='formal':cfg['scope']='formal'
    if change=='grade-off':cfg['run']={'grade':False}
    if change=='unknown-task':cfg['tasks']=['absent']
    if change=='duplicate-task':cfg['tasks']*=2
    if change=='conflicting-boundaries':cfg['boundaries']=[dict(n=0,j=1)]
    with pytest.raises(ValueError): eval_plan.compile_plan(cfg)


def test_method_task_repeat_product_is_explicit_and_never_expands_catalog(tmp_path):
    cfg=config(tmp_path);cfg.update(methods=[{'class':'NoCompaction'},{'class':'NoCompaction','label':'second'}],repeats=3,workers=4)
    plan=eval_plan.compile_plan(cfg)
    assert len(plan['jobs'])==6 and len({job['id'] for job in plan['jobs']})==6
    assert {job['task']['id'] for job in plan['jobs']}==set(cfg['tasks'])
    assert plan['max_parallel']==4


@pytest.mark.parametrize('change',['changed-data','mutable-image-tag','wrong-benchmark','wrong-job-image'])
def test_resource_binding_is_not_just_an_image_name(tmp_path,change):
    cfg=config(tmp_path)
    if change=='wrong-job-image':
        plan=eval_plan.compile_plan(cfg)
        plan['jobs'][0]['resources']['images']['agent']['id']='sha256:'+'3'*64
        # Jobs and manifest must not share mutable dictionaries.
        with pytest.raises(ValueError,match='resources differ'): eval_plan.verify(seal(plan),check_inputs=False)
        return
    path=Path(cfg['environment']['resources']);lock=json.loads(path.read_text(encoding='utf-8'))
    record=next(iter(lock['tasks'].values()))
    if change=='changed-data': record['task_sha256']='0'*64
    if change=='mutable-image-tag': record['images']['agent']['id']='latest'
    if change=='wrong-benchmark':lock['benchmark']='another'
    path.write_text(json.dumps(task_resources.seal({k:v for k,v in lock.items() if k!='sha256'})), encoding='utf-8')
    with pytest.raises(ValueError):eval_plan.compile_plan(cfg)


@pytest.mark.parametrize('name',['../outside','C:/outside','a\\outside','/outside','auth.json'])
def test_tree_paths_cannot_escape_or_capture_credentials(tmp_path,name):
    with pytest.raises((ValueError,FileNotFoundError)):
        eval_trees.capture(tmp_path,[name],folder='task-data')


def test_symbolic_parent_cannot_smuggle_an_external_tree(tmp_path):
    external=tmp_path/'external';external.mkdir();(external/'source.py').write_text('external', encoding='utf-8')
    root=tmp_path/'data';root.mkdir()
    try:(root/'alias').symlink_to(external,target_is_directory=True)
    except OSError:pytest.skip('platform cannot create symlink')
    with pytest.raises(ValueError,match='symbolic'):
        eval_trees.capture(root,['alias/source.py'],folder='task-data')


def test_tree_environment_rebinding_is_rejected_even_for_a_resealed_plan(tmp_path):
    plan=eval_plan.compile_plan(config(tmp_path))
    plan['config']['environment']['data']=str(tmp_path/'another')
    with pytest.raises(ValueError,match='not bound'):eval_plan.verify(seal(plan),check_inputs=False)
