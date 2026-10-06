"""Queued jobs use reviewed input versions while the workspace keeps changing."""
import copy, json, subprocess, sys
from pathlib import Path
import pytest
from ctxpress.harness.jobs import inputs as eval_inputs, plan as eval_plan, queue as evaluation
from ctxpress.core import artifacts as artifact_io
from ctxpress.replay.calibrate import fit


def inputs(tmp_path):
    scripts, binary = tmp_path/'scripts', tmp_path/'bin'
    scripts.mkdir(); binary.mkdir()
    for name in ('miss_probe_docker.py','c03_run.py','multi_probe.py','make_sidecar.py','grade.sh'):
        (scripts/name).write_text('# fixture script\n', encoding='utf-8')
    (binary/'codex').write_bytes(b'fixture executable')
    (binary/'codex-code-mode-host').write_bytes(b'fixture code mode helper')
    trajectory = scripts/'recorded.jsonl'; trajectory.write_text('{"fixture":"original history"}\n',encoding='utf-8')
    (scripts/'valid_points.json').write_text(json.dumps([dict(n=3,j=14,i=0,src='recorded.jsonl',prefix_tokens=130000)]),encoding='utf-8')
    profile = tmp_path/'profile.json'
    before = [dict(seg='call',kind='read',res=['a.py'],size=5,text='cat a.py'),
              dict(seg='out',kind='read',res=['a.py'],sub='code',size=100,text='source'*80)]
    fit([dict(name='training',prefix=0,alpha=1,reqs=[dict(before=before,t=i+1) for i in range(4)])],profile)
    method = {'class':'WithMemory','args':{'inner':{'class':'CostModel','args':{'profile':str(profile),'allow_summary':False}}}}
    config = dict(schema='ctxpress.eval',version=1,scope='mechanism',backend='codex_docker',model='fixture-model',
                  environment=dict(scripts=str(scripts),bindir=str(binary)),
                  boundaries=[dict(id='point',n=3,j=14,context_tokens=130000)], methods=[method], repeats=1)
    return config


def test_queued_jobs_keep_original_inputs_after_source_changes_or_removal(tmp_path):
    plan = eval_plan.compile_plan(inputs(tmp_path))
    assert not plan['missing_environment_files']
    expected = {source:Path(source).read_bytes() for source in plan['artifacts']}
    directory = evaluation.prepare(plan,tmp_path/'run')
    approved = (directory/'plan.json').read_bytes()
    # Include runtime companion, source trajectory, policy and benchmark scripts.
    assert str(tmp_path/'bin/codex-code-mode-host') in plan['artifacts']
    assert str(tmp_path/'scripts/recorded.jsonl') in plan['artifacts']
    for source in expected:
        Path(source).unlink()
    evaluation.prepare(plan,directory)
    verified = evaluation._verified_plan(directory)
    config, paths = eval_inputs.execution(verified,directory/'inputs')
    assert (directory/'plan.json').read_bytes() == approved
    assert config['environment']['bindir'] == str(directory/'inputs/bin')
    assert config['environment']['scripts'] == str(directory/'inputs/scripts')
    assert all(Path(paths[source]).read_bytes()==content for source,content in expected.items())
    method = eval_inputs.method(verified['jobs'][0]['method'],paths)
    profile = method['args']['inner']['args']['profile']
    assert profile != plan['jobs'][0]['method']['args']['inner']['args']['profile']
    from ctxpress.methods import build
    assert build(method).inner.provenance['sessions'] == ['training']


def test_snapshot_modification_blocks_launch_and_never_falls_back(tmp_path,monkeypatch):
    plan = eval_plan.compile_plan(inputs(tmp_path))
    directory = evaluation.prepare(plan,tmp_path/'run')
    (directory/'inputs/bin/codex').write_bytes(b'replaced snapshot')
    with pytest.raises(ValueError,match='snapshot changed'):
        evaluation.schedule(directory,launch=lambda *a: pytest.fail('launched changed inputs'))
    assert evaluation.state(directory)['counts'] == {'pending':1}


def test_snapshot_race_does_not_publish_partial_inputs(tmp_path,monkeypatch):
    plan = eval_plan.compile_plan(inputs(tmp_path))
    original = eval_inputs.shutil.copy2
    def changed(source,target):
        result = original(source,target)
        Path(target).write_bytes(b'changed while copying')
        return result
    monkeypatch.setattr(eval_inputs.shutil,'copy2',changed)
    with pytest.raises(ValueError,match='during snapshot creation'):
        evaluation.prepare(plan,tmp_path/'run')
    assert not (tmp_path/'run/inputs').exists()
    assert not list((tmp_path/'run').glob('.inputs-*'))
    assert not (tmp_path/'run/jobs.sqlite').exists()


def test_runtime_freezing_does_not_rehash_mutable_inputs_before_verified_copy(tmp_path,monkeypatch):
    plan = eval_plan.compile_plan(inputs(tmp_path))
    original = eval_plan.file_sha256
    sources = {Path(name).resolve() for name in plan['artifacts']}
    def hashed(path):
        assert Path(path).resolve() not in sources, 'mutable input redundantly hashed before snapshot copy'
        return original(path)
    monkeypatch.setattr(eval_plan, 'file_sha256', hashed)
    directory = evaluation.prepare(plan, tmp_path/'run')
    assert evaluation.state(directory)['counts'] == {'pending':1}


def test_source_drift_before_freezing_still_cannot_create_runnable_jobs(tmp_path):
    plan = eval_plan.compile_plan(inputs(tmp_path))
    (tmp_path/'bin/codex').write_bytes(b'changed since planning')
    with pytest.raises(ValueError, match='input changed during snapshot'):
        evaluation.prepare(plan, tmp_path/'run')
    assert not (tmp_path/'run/inputs').exists()
    assert not (tmp_path/'run/jobs.sqlite').exists()


def test_source_redirected_to_credentials_is_rejected_before_copy(tmp_path,monkeypatch):
    plan = eval_plan.compile_plan(inputs(tmp_path))
    source = tmp_path/'bin/codex'
    auth = tmp_path/'auth.json'; auth.write_text('test fixture, never credentials', encoding='utf-8')
    source.unlink()
    try: source.symlink_to(auth)
    except OSError: pytest.skip('platform cannot create symlink')
    copy2 = eval_inputs.shutil.copy2
    def checked_copy(source_path, target, *args, **kwargs):
        assert Path(source_path) != source, 'credential alias was copied'
        return copy2(source_path, target, *args, **kwargs)
    monkeypatch.setattr(eval_inputs.shutil, 'copy2', checked_copy)
    with pytest.raises(ValueError, match='credential files'):
        evaluation.prepare(plan, tmp_path/'run')
    assert not (tmp_path/'run/jobs.sqlite').exists()


def test_input_manifest_cannot_be_rebound_to_a_different_plan(tmp_path):
    plan = eval_plan.compile_plan(inputs(tmp_path))
    directory = evaluation.prepare(plan,tmp_path/'run')
    changed = copy.deepcopy(plan)
    changed['config']['model'] = 'another model'
    content = {key:value for key,value in changed.items() if key!='sha256'}
    import hashlib
    changed['sha256'] = hashlib.sha256(eval_plan.canonical(content).encode()).hexdigest()
    with pytest.raises(ValueError,match='another plan'):
        evaluation.prepare(changed,directory)
    assert evaluation._verified_plan(directory)['sha256'] == plan['sha256']


def test_input_manifest_cannot_escape_the_snapshot_directory(tmp_path):
    plan = eval_plan.compile_plan(inputs(tmp_path))
    directory = evaluation.prepare(plan,tmp_path/'run')
    path = directory/'inputs/manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    source = next(iter(manifest['files']))
    # Even an otherwise resealed map cannot reference a mutable source outside inputs.
    manifest['files'][source]['path'] = source
    content = {key:value for key,value in manifest.items() if key!='sha256'}
    artifact_io.atomic_json(path,eval_inputs._sealed(content))
    with pytest.raises(ValueError,match='snapshot changed'):
        evaluation._verified_plan(directory)


def test_detached_scheduler_uses_inputs_after_sources_are_deleted(tmp_path):
    plan = eval_plan.compile_plan(inputs(tmp_path))
    directory = evaluation.prepare(plan,tmp_path/'run')
    for source in plan['artifacts']:
        Path(source).unlink()
    # This process imports the frozen scheduler from runtime, never the workspace.
    script = '''import subprocess, sys
from ctxpress.harness.jobs.queue import schedule

def launch(directory, job_id, attempt):
    return subprocess.Popen([sys.executable, sys.argv[2], str(directory), job_id, str(attempt), '0.1'])
schedule(sys.argv[1],launch=launch,interval=0.02)
'''
    result = subprocess.run([sys.executable,'-c',script,str(directory),str(Path(__file__).with_name('fake_eval_job.py'))],
                            cwd=directory/'runtime',env=evaluation._runtime_env(directory),capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stderr
    assert evaluation.state(directory)['counts'] == {'completed':1}


def test_incomplete_boundary_catalog_is_explicit_and_does_not_start(tmp_path,monkeypatch):
    cfg = inputs(tmp_path)
    (tmp_path/'scripts/valid_points.json').write_text('[]',encoding='utf-8')
    plan = eval_plan.compile_plan(cfg)
    assert any('#boundary-3-14' in path for path in plan['missing_environment_files'])
    path = tmp_path/'plan.json'; artifact_io.atomic_json(path,plan)
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k: pytest.fail('started incomplete evaluation'))
    with pytest.raises(ValueError,match='incomplete'):
        evaluation.start(path,tmp_path/'run',background=True)


@pytest.mark.parametrize("requests,status,expected", [(1,200,"completed"), (0,None,"failed"), (1,401,"failed")])
def test_frozen_worker_resolves_real_job_paths_after_source_removal(tmp_path, requests, status, expected):
    plan = eval_plan.compile_plan(inputs(tmp_path))
    directory = evaluation.prepare(plan,tmp_path/'run')
    job_id = plan['jobs'][0]['id']
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET status='running',attempt=1 WHERE id=?",(job_id,))
    (directory/'jobs'/job_id/'attempt-1').mkdir(parents=True)
    for source in plan['artifacts']:
        Path(source).unlink()
    script = '''import sys
from pathlib import Path
from ctxpress.benchmarks.milestone import checkpoint_run as codex_docker; from ctxpress.harness.jobs import queue as evaluation
from ctxpress.methods import build

def run(n,j,entry,**kwargs):
    assert (n,j)==(3,14)
    assert '/inputs/' in kwargs['bindir'].replace('\\\\','/')
    assert '/inputs/' in kwargs['scripts'].replace('\\\\','/')
    profile=entry['args']['inner']['args']['profile']
    assert Path(profile).is_file() and build(entry).inner.provenance['sessions']==['training']
    assert any(Path(path).read_text().startswith('{"fixture"') for path in kwargs['input_paths'].values() if path.endswith('.jsonl'))
    return dict(test_only=True,requests=int(sys.argv[3]),rewrites=[dict(request=1,status=int(sys.argv[4]))],stop='offline path check')
codex_docker.run=run
evaluation.execute_job(sys.argv[1],sys.argv[2],1)
'''
    result = subprocess.run([sys.executable,'-c',script,str(directory),job_id,str(requests),str(status or 0)],cwd=directory/'runtime',
                            env=evaluation._runtime_env(directory),capture_output=True,text=True,timeout=15)
    assert (result.returncode == 0) == (expected == 'completed'), result.stderr
    assert evaluation.state(directory)['counts'] == {expected:1}
    with evaluation.database(directory) as connection:
        saved = json.loads(connection.execute('SELECT result FROM jobs WHERE id=?', (job_id,)).fetchone()['result'])
    assert saved['benchmark']['name'] == 'swe-milestone'
    assert saved['benchmark']['task_id'] == 'point'
    if expected == 'failed':
        assert 'ModelRequestFailure' in result.stderr
        with evaluation.database(directory) as connection:
            row = connection.execute('SELECT result,error FROM jobs WHERE id=?',(job_id,)).fetchone()
        assert json.loads(row['result'])['requests'] == requests and row['error'] == 'ModelRequestFailure'
    result = json.loads((directory/'jobs'/job_id/'attempt-1/result.json').read_text(encoding='utf-8'))
    assert result['test_only'] and result['boundary_context_evidence']['catalog_prefix_tokens'] == 130000


def test_credential_filename_is_rejected_before_hashing_or_copying(tmp_path,monkeypatch):
    auth = tmp_path/'auth.json'
    auth.write_text('offline fixture only',encoding='utf-8')
    real_open = Path.open
    def open_file(path,*args,**kwargs):
        if path.resolve() == auth.resolve():
            pytest.fail('credential file opened for an input snapshot')
        return real_open(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',open_file)
    with pytest.raises(ValueError,match='credential files'):
        eval_plan.file_sha256(auth)


def test_legacy_plan_keeps_its_original_input_validation_contract(tmp_path):
    plan = eval_plan.compile_plan(inputs(tmp_path))
    plan.pop('input_snapshot_version')
    import hashlib
    plan['sha256'] = hashlib.sha256(eval_plan.canonical({key:value for key,value in plan.items() if key!='sha256'}).encode()).hexdigest()
    directory = evaluation.prepare(plan,tmp_path/'run')
    assert not (directory/'inputs').exists()
    (tmp_path/'bin/codex').write_bytes(b'changed original binary')
    with pytest.raises(ValueError,match='input changed'):
        evaluation._verified_plan(directory)
