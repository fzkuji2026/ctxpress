"""Environment declarations remain usable while original files and tags move."""
import builtins, copy, json, shutil, subprocess, sys
from pathlib import Path
import pytest
from ctxpress.harness.jobs import environment as eval_environment, inputs as eval_inputs, plan as eval_plan, queue as evaluation
from ctxpress.harness import cli as eval_cli
from test_eval_inputs import inputs

BASE = 'sha256:' + '1'*64
BOUNDARY = 'sha256:' + '2'*64


def declared(tmp_path, monkeypatch):
    cfg = inputs(tmp_path)
    root = tmp_path/'requirements'
    (root/'srs').mkdir(parents=True)
    (root/'empty').mkdir()
    (root/'srs/task.md').write_text('original requirements',encoding='utf-8')
    seen = []
    def image(reference):
        seen.append(reference)
        digest = BOUNDARY if reference in ('hsnap-3:14', BOUNDARY) else BASE
        return dict(reference=reference,id=digest,os='linux',architecture='amd64',repo_digests=[])
    monkeypatch.setattr(eval_environment,'image',image)
    lock = eval_environment.capture(root,'fixture-base',[(3,14)])
    path = tmp_path/'environment.json'
    eval_plan.atomic_json(path,lock)
    cfg['environment']['snapshot'] = str(path)
    return cfg,lock,seen


def test_compile_does_not_inspect_images_and_frozen_environment_survives_source_removal(tmp_path,monkeypatch):
    cfg,lock,seen = declared(tmp_path,monkeypatch)
    seen.clear()
    plan = eval_plan.compile_plan(cfg)
    assert seen == [] and plan['environment_snapshot'] == lock
    directory = evaluation.prepare(plan,tmp_path/'run')
    shutil.rmtree(lock['workspace']['root'])
    for source in plan['artifacts']:
        path = Path(source)
        if path.exists():
            path.unlink()
    evaluation.prepare(plan,directory)
    config,paths = eval_inputs.execution(evaluation._verified_plan(directory),directory/'inputs')
    workspace = Path(config['environment']['workspace'])
    assert (workspace/'srs/task.md').read_text(encoding='utf-8') == 'original requirements'
    assert (workspace/'empty').is_dir()
    frozen_lock = eval_environment.load(config['environment']['snapshot'])
    assert eval_environment.runtime(frozen_lock,workspace,3,14) == (BASE,BOUNDARY)
    # Original tags can move; execution inspects only immutable content IDs.
    assert seen == [BASE,BOUNDARY]


@pytest.mark.parametrize('change',['extra-file','changed-file','missing-directory'])
def test_workspace_mutation_blocks_scheduler_before_job_launch(tmp_path,monkeypatch,change):
    cfg,lock,_ = declared(tmp_path,monkeypatch)
    plan = eval_plan.compile_plan(cfg)
    directory = evaluation.prepare(plan,tmp_path/'run')
    root = directory/'inputs/workspace'
    if change == 'extra-file':
        (root/'injected.md').write_text('new unreviewed instructions',encoding='utf-8')
    elif change == 'changed-file':
        (root/'srs/task.md').write_text('new requirements',encoding='utf-8')
    else:
        (root/'empty').rmdir()
    with pytest.raises(ValueError,match='workspace|snapshot changed'):
        evaluation.schedule(directory,launch=lambda *a: pytest.fail('launched modified environment'))
    assert evaluation.state(directory)['counts'] == {'pending':1}


def test_missing_image_content_blocks_runtime_without_pulling(tmp_path,monkeypatch):
    reader = eval_environment.image
    cfg,lock,_ = declared(tmp_path,monkeypatch)
    seen = []
    def docker(command,**kwargs):
        seen.append(command)
        raise subprocess.CalledProcessError(1,command)
    monkeypatch.setattr(eval_environment,'image',reader)
    monkeypatch.setattr(eval_environment.subprocess,'run',docker)
    with pytest.raises(subprocess.CalledProcessError):
        eval_environment.runtime(lock,lock['workspace']['root'],3,14)
    assert seen == [['docker','image','inspect',BASE]]


def test_selected_boundaries_must_have_manifest_image_bindings(tmp_path,monkeypatch):
    cfg,lock,_ = declared(tmp_path,monkeypatch)
    cfg['boundaries'][0]['j'] = 15
    with pytest.raises(ValueError,match='missing a selected boundary'):
        eval_plan.compile_plan(cfg)


def test_manifest_cannot_traverse_or_declare_unbound_workspace_paths(tmp_path,monkeypatch):
    cfg,lock,_ = declared(tmp_path,monkeypatch)
    content = {key:copy.deepcopy(value) for key,value in lock.items() if key!='sha256'}
    content['workspace']['directories'].append('../outside')
    with pytest.raises(ValueError,match='relative path'):
        eval_environment.verify(eval_environment._seal(content))


def test_credentials_are_rejected_without_reading_them(tmp_path,monkeypatch):
    root = tmp_path/'workspace'; root.mkdir()
    auth = root/'auth.json'; auth.write_text('synthetic fixture',encoding='utf-8')
    actual = builtins.open
    def guarded(path,*args,**kwargs):
        if Path(path).resolve() == auth:
            pytest.fail('credential contents were opened')
        return actual(path,*args,**kwargs)
    monkeypatch.setattr(builtins,'open',guarded)
    with pytest.raises(ValueError,match='credential files'):
        eval_environment.workspace(root)


def test_capture_cli_only_inspects_images_and_writes_declaration(tmp_path,monkeypatch,capsys):
    root = tmp_path/'workspace'; root.mkdir()
    (root/'task.md').write_text('fixture requirements',encoding='utf-8')
    commands = []
    def docker(command,**kwargs):
        commands.append(command)
        assert command[:3] == ['docker','image','inspect']
        digest = BOUNDARY if command[-1] == 'hsnap-3:14' else BASE
        return subprocess.CompletedProcess(command,0,json.dumps([dict(Id=digest,Os='linux',Architecture='amd64')]),'')
    monkeypatch.setattr(eval_environment.subprocess,'run',docker)
    output = tmp_path/'captured.json'
    eval_cli.main(['capture-environment','--workspace',str(root),'--base-image','fixture-base',
                     '--boundary','3:14','--output',str(output)])
    result = json.loads(capsys.readouterr().out)
    assert result['experiments_started'] == 0 and result['workspace_files'] == 1
    assert eval_environment.load(output)['images']['boundaries']['3:14']['id'] == BOUNDARY
    assert commands == [['docker','image','inspect','hsnap-3:14'],['docker','image','inspect','fixture-base']]


def test_frozen_job_passes_copied_workspace_and_manifest_to_backend(tmp_path,monkeypatch):
    cfg,lock,_ = declared(tmp_path,monkeypatch)
    plan = eval_plan.compile_plan(cfg)
    directory = evaluation.prepare(plan,tmp_path/'run')
    job_id = plan['jobs'][0]['id']
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET status='running',attempt=1 WHERE id=?",(job_id,))
    shutil.rmtree(lock['workspace']['root'])
    Path(cfg['environment']['snapshot']).unlink()
    script = '''import sys
from pathlib import Path
from ctxpress.benchmarks.milestone import checkpoint_run as codex_docker; from ctxpress.harness.jobs import environment as eval_environment, queue as evaluation

def run(n,j,entry,**kwargs):
    root=Path(kwargs['workspace'])
    assert (root/'srs/task.md').read_text()=='original requirements'
    assert (root/'empty').is_dir()
    lock=eval_environment.load(kwargs['snapshot'],[(n,j)])
    eval_environment.verify_workspace(lock,root)
    assert '/inputs/' in str(root).replace('\\\\','/')
    return dict(test_only=True,requests=1,rewrites=[dict(request=1,status=200)],stop='offline frozen environment path check')
codex_docker.run=run
evaluation.execute_job(sys.argv[1],sys.argv[2],1)
'''
    process = subprocess.run([sys.executable,'-c',script,str(directory),job_id],cwd=directory/'runtime',
                             env=evaluation._runtime_env(directory),capture_output=True,text=True,timeout=15)
    assert process.returncode == 0, process.stderr
    assert evaluation.state(directory)['counts'] == {'completed':1}


def test_environment_document_and_embedded_plan_must_agree(tmp_path,monkeypatch):
    cfg,lock,_ = declared(tmp_path,monkeypatch)
    plan = eval_plan.compile_plan(cfg)
    content = {key:value for key,value in plan['environment_snapshot'].items() if key!='sha256'}
    content['images']['base']['id'] = 'sha256:'+'3'*64
    plan['environment_snapshot'] = eval_environment._seal(content)
    import hashlib
    plan['sha256'] = hashlib.sha256(eval_plan.canonical({key:value for key,value in plan.items() if key!='sha256'}).encode()).hexdigest()
    with pytest.raises(ValueError,match='reviewed plan'):
        evaluation.prepare(plan,tmp_path/'run')
    assert not (tmp_path/'run/inputs').exists()


def test_report_keeps_actual_environment_evidence_and_fixed_scope(tmp_path,monkeypatch):
    cfg,lock,_ = declared(tmp_path,monkeypatch)
    plan = eval_plan.compile_plan(cfg)
    directory = evaluation.prepare(plan,tmp_path/'run')
    job_id = plan['jobs'][0]['id']
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET status='running',attempt=1 WHERE id=?",(job_id,))
    evidence = dict(manifest_sha256=lock['sha256'],workspace_frozen=True,base_image_id=BASE,boundary_image_id=BOUNDARY)
    evaluation.set_result(directory,job_id,1,dict(test_only=True,requests=0,environment=evidence))
    from ctxpress.harness.results.report import report, write_report
    result = report(directory)
    assert result['environment_manifest_sha256'] == lock['sha256']
    assert result['methods'][0]['jobs'][0]['environment'] == evidence
    assert 'not frozen' in result['environment_scope']
    assert 'synthetic' in result['evidence']
    write_report(directory)
    markup = (directory/'report.html').read_text(encoding='utf-8')
    assert lock['sha256'] in markup and '官方评分依赖与外部模型版本尚未固定' in markup


def test_auth_file_cannot_be_loaded_as_environment_manifest(tmp_path,monkeypatch):
    auth = tmp_path/'auth.json'
    auth.write_text('synthetic fixture',encoding='utf-8')
    actual = Path.read_bytes
    def guarded(path):
        if path == auth:
            pytest.fail('read credential contents as a manifest')
        return actual(path)
    monkeypatch.setattr(Path,'read_bytes',guarded)
    with pytest.raises(ValueError,match='credential files'):
        eval_environment.load(auth)
