"""Capture local resource identities without downloading or executing a benchmark."""
import copy, json, subprocess
from pathlib import Path
import pytest
from ctxpress.__main__ import main
from ctxpress.harness.jobs import environment as eval_environment, plan as eval_plan, queue as evaluation, resources as task_resources
from ctxpress import benchmarks
from test_original_benchmarks import terminal_data
from test_task_start_plan import config
from test_task_instances import seal


def capture_spec(tmp_path):
    root=terminal_data(tmp_path);selected=benchmarks.get('terminal-bench').task_instances(root)
    code=tmp_path/'official';code.mkdir();(code/'runner.py').write_text('# fixture', encoding='utf-8')
    spec=dict(schema='ctxpress.eval.task_resource_spec',version=1,benchmark='terminal-bench',release='fixture-release',
              tasks={selected[0]['id']:dict(agent_image='local:agent',grading_images={'task':'local:agent'})},
              trees={'code':dict(root=str(code),files=['runner.py'])})
    return root,selected,spec


def test_resource_capture_inspects_only_declared_images_and_never_runs_tasks(tmp_path,monkeypatch):
    _,selected,spec=capture_spec(tmp_path);original=copy.deepcopy(spec);inspections=[]
    def image(reference):
        inspections.append(reference);return dict(reference=reference,id='sha256:'+'f'*64)
    monkeypatch.setattr(eval_environment,'image',image)
    lock=task_resources.capture(spec,selected)
    assert spec==original and inspections==['local:agent']
    assert lock['tasks'][selected[0]['id']]['task_sha256']==task_resources.task_digest(selected[0])
    assert lock['runtime']['sha256']==eval_plan.file_sha256(lock['runtime']['python'])
    # Agent and grading phase records cannot alias a mutable image dictionary.
    images=lock['tasks'][selected[0]['id']]['images'];images['agent']['reference']='changed'
    assert images['grading']['task']['reference']=='local:agent'


def test_auxiliary_services_are_pinned_separately_and_do_not_expand_task_selection(tmp_path,monkeypatch):
    _,selected,spec=capture_spec(tmp_path)
    spec['tasks'][selected[0]['id']]['service_images']={'database':'local:db','cache':'local:db'}
    inspections=[]
    def image(reference):
        inspections.append(reference)
        return dict(reference=reference,id='sha256:'+('f' if reference=='local:db' else 'e')*64)
    monkeypatch.setattr(eval_environment,'image',image)
    lock=task_resources.capture(spec,selected)
    assert inspections==['local:agent','local:db']
    services=lock['tasks'][selected[0]['id']]['images']['services']
    assert services['database']['id']=='sha256:'+'f'*64
    services['database']['reference']='changed'
    assert services['cache']['reference']=='local:db'
    assert set(lock['tasks'])=={selected[0]['id']}


@pytest.mark.parametrize('services',[{'main':'local:other'},{'../escape':'local:other'}, {'db':True}, []])
def test_invalid_service_capture_fails_before_docker_inspection(tmp_path,monkeypatch,services):
    _,selected,spec=capture_spec(tmp_path)
    spec['tasks'][selected[0]['id']]['service_images']=services
    monkeypatch.setattr(eval_environment,'image',lambda reference:pytest.fail('inspected invalid service declaration'))
    with pytest.raises(ValueError,match='service'):task_resources.capture(spec,selected)


@pytest.mark.parametrize('change',['wrong-task','missing-tree-file','auth-input','bad-image','wrong-benchmark'])
def test_bad_capture_specs_fail_before_docker_inspection(tmp_path,monkeypatch,change):
    _,selected,spec=capture_spec(tmp_path)
    if change=='wrong-task':spec['tasks']={'unknown':next(iter(spec['tasks'].values()))}
    if change=='missing-tree-file':spec['trees']['code']['files']=['missing.py']
    if change=='auth-input':spec['trees']['code']['files']=['auth.json']
    if change=='bad-image':next(iter(spec['tasks'].values()))['agent_image']=True
    if change=='wrong-benchmark':spec['benchmark']='another'
    monkeypatch.setattr(eval_environment,'image',lambda reference:pytest.fail('inspected invalid capture input'))
    with pytest.raises((ValueError,FileNotFoundError)):task_resources.capture(spec,selected)


def test_cli_captures_explicit_local_tasks_with_zero_experiments(tmp_path,monkeypatch,capsys):
    root,selected,spec=capture_spec(tmp_path);source=tmp_path/'spec.json';source.write_text(json.dumps(spec), encoding='utf-8')
    destination=tmp_path/'captured.json'
    monkeypatch.setattr(eval_environment,'image',lambda reference:dict(reference=reference,id='sha256:'+'e'*64))
    main(['eval','capture-task-resources','--benchmark','terminal-bench','--data',str(root),'--spec',str(source),
          '--task',selected[0]['id'],'--output',str(destination)])
    output=json.loads(capsys.readouterr().out)
    assert output['experiments_started']==output['downloads_started']==0 and destination.is_file()
    task_resources.read(destination,'terminal-bench',selected)


def test_cli_output_cannot_overwrite_its_declared_official_inputs(tmp_path,monkeypatch):
    root,selected,spec=capture_spec(tmp_path);source=tmp_path/'spec.json';source.write_text(json.dumps(spec), encoding='utf-8')
    target=Path(spec['trees']['code']['root'])/'runner.py';before=target.read_bytes()
    monkeypatch.setattr(eval_environment,'image',lambda reference:pytest.fail('inspected a rejected output location'))
    with pytest.raises(SystemExit):main(['eval','capture-task-resources','--benchmark','terminal-bench','--data',str(root),
        '--spec',str(source),'--task',selected[0]['id'],'--output',str(target)])
    assert target.read_bytes()==before


def test_resealed_plan_cannot_replace_the_frozen_official_resource_tree(tmp_path):
    plan=eval_plan.compile_plan(config(tmp_path))
    plan['input_trees']['official:code']['tree']['files']['harness/runner.py']='0'*64
    with pytest.raises(ValueError,match='resource tree differs'):eval_plan.verify(seal(plan),check_inputs=False)


def test_empty_harbor_environment_directory_survives_input_freezing(tmp_path):
    root=terminal_data(tmp_path);directory=root/'task-one'
    for path in sorted((directory/'environment').rglob('*'),reverse=True):
        if path.is_file():path.unlink()
        else:path.rmdir()
    (directory/'tests/empty').mkdir()
    found=benchmarks.get('terminal-bench').task_instances(root)[0]
    binary=tmp_path/'bin';binary.mkdir();(binary/'codex').write_bytes(b'fixture')
    cfg=dict(schema='ctxpress.eval',version=1,benchmark='terminal-bench',start_mode='task_start',scope='benchmark',
             backend='codex_docker',model='fixture',environment=dict(data=str(root),bindir=str(binary)),
             tasks=[found['id']],methods=[{'class':'NoCompaction'}])
    plan=eval_plan.compile_plan(cfg);run=evaluation.prepare(plan,tmp_path/'run')
    assert (run/'inputs/task-data/task-one/environment').is_dir()
    assert (run/'inputs/task-data/task-one/tests/empty').is_dir()
    evaluation._verified_plan(run)


@pytest.mark.linux_only
def test_native_capture_binds_authentic_git_objects_before_inspecting_images(tmp_path,monkeypatch):
    cfg=config(tmp_path);root=Path(cfg['environment']['data'])
    selected=benchmarks.get('swe-milestone').task_instances(root)
    subprocess.run(['git','init','-q',str(root)],check=True)
    subprocess.run(['git','-C',str(root),'add','.'],check=True)
    git=['git','-C',str(root),'-c','user.name=fixture','-c','user.email=fixture@example.invalid']
    subprocess.run(git+['commit','-qm','synthetic data'],check=True)
    subprocess.run(git+['tag','v1.0.2'],check=True)
    code=tmp_path/'capture-code';code.mkdir();(code/'runner.py').write_text('# synthetic', encoding='utf-8')
    spec=dict(schema='ctxpress.eval.task_resource_spec',version=1,benchmark='swe-milestone',release='v1.0.2',
        native_data_version=True,trees={'code':{'root':str(code),'files':['runner.py']}},
        tasks={selected[0]['id']:{'agent_image':'local:agent','grading_images':{'M1':'local:grade'}}})
    observed=[]
    monkeypatch.setattr(eval_environment,'image',lambda ref:observed.append(ref) or {'id':'sha256:'+'a'*64})
    lock=task_resources.capture(spec,selected)
    proof=lock['native_data_version'];assert proof['head']==proof['tag'] and len(proof['objects'])==1
    assert observed==['local:agent','local:grade']
    task_resources.verify(lock,'swe-milestone',selected)
    changed=copy.deepcopy(lock);changed['native_data_version']['objects'][proof['head']]['data']='!!'
    with pytest.raises(ValueError):task_resources.verify(task_resources.seal(changed),'swe-milestone',selected)
    observed.clear();spec['release']='v9.9.9'
    with pytest.raises(subprocess.CalledProcessError):task_resources.capture(spec,selected)
    assert observed==[]
