"""Frozen grader imports and configuration survive continued development."""
import copy, io, json, shutil, subprocess, sys, tarfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from ctxpress.harness.jobs import environment as eval_environment, inputs as eval_inputs, plan as eval_plan, queue as evaluation
from ctxpress.core import artifacts as artifact_io
from ctxpress.benchmarks.milestone import checkpoint_grading as eval_grading
from ctxpress.harness.jobs import grading_inputs
from test_eval_inputs import inputs

IMAGE = 'sha256:'+'1'*64


def declared(tmp_path,monkeypatch):
    cfg = inputs(tmp_path)
    code,data,trials,deps = tmp_path/'official',tmp_path/'fixture_repo',tmp_path/'trials',tmp_path/'deps'
    for path in (code/'harness/e2e',code/'harness/utils',code/'manifests',code/'quarantine_configs',data/'dockerfiles/milestone_002',data/'test_results/milestone_002',deps/'yaml',deps/'pathspec'):
        path.mkdir(parents=True,exist_ok=True)
    for path in (code/'harness/__init__.py',code/'harness/e2e/__init__.py',code/'harness/utils/__init__.py',deps/'pathspec/__init__.py'):
        path.write_text('',encoding='utf-8')
    (deps/'yaml/__init__.py').write_text('import json\ndef safe_load(value): return json.loads(value)\n',encoding='utf-8')
    (code/'harness/utils/src_filter.py').write_text('''class SrcFileFilter:
    def __init__(self,**kwargs): self.modifiable_test_patterns=[]
    def _compile(self): pass
    def should_include_in_snapshot(self,name): return name.startswith('core/') and not name.endswith('_test.go')
''',encoding='utf-8')
    (code/'harness/e2e/image_version.py').write_text('def resolve_image(ref): raise RuntimeError("live image resolver used")\n',encoding='utf-8')
    (code/'harness/e2e/evaluator.py').write_text('''import json,sys
from pathlib import Path
from harness.e2e.image_version import resolve_image

def main():
    def value(key): return sys.argv[sys.argv.index(key)+1]
    root=Path(value('--workspace-root'))
    assert root.name=='fixture_repo' and (root/'metadata.json').is_file()
    assert '/grading/' in value('--repo-config').replace('\\\\','/')
    assert value('--runtime-policy-mode')=='protected'
    assert 'sha256:' in resolve_image('swe-milestone/fixture_repo__milestone_002')
    config=json.loads(Path(value('--repo-config')).read_text())
    assert config['repo_src_dirs']==['core']
    Path(value('--output')).write_text(json.dumps(dict(resolved=True,infra_invalid=False,test_only=True)))
''',encoding='utf-8')
    (code/'LICENSE').write_text('fixture license',encoding='utf-8')
    (code/'pyproject.toml').write_text('# fixture',encoding='utf-8')
    (code/'manifests/BENCHMARK_VERSION').write_text('v1.0.2',encoding='utf-8')
    (data/'metadata.json').write_text('{}',encoding='utf-8')
    (data/'dockerfiles/milestone_002/test_config.json').write_text('{}',encoding='utf-8')
    (data/'test_results/milestone_002/milestone_002_classification.json').write_text('{}',encoding='utf-8')
    trial='ours_sol_low_navidrome_002'; folder=trials/trial/'evaluation/milestone_002'; folder.mkdir(parents=True)
    (trials/trial/'repo_config.yaml').write_text(json.dumps(dict(repo_src_dirs=['core'])),encoding='utf-8')
    (trials/trial/'runtime_policy.yaml').write_text('{}',encoding='utf-8')
    (folder/'source_snapshot.integrity.json').write_text(json.dumps(dict(capture_filter={},manifest_overlay={},agent_base_image_id=IMAGE)),encoding='utf-8')
    monkeypatch.setitem(sys.modules,'yaml',SimpleNamespace(safe_load=json.loads))
    monkeypatch.setattr(grading_inputs,'dependencies',lambda: {name:eval_environment.workspace(deps/name) for name in ('yaml','pathspec')})
    monkeypatch.setattr(eval_environment,'image',lambda ref:dict(reference=ref,id=IMAGE,os='fixture',architecture='fixture',repo_digests=[]))
    lock=grading_inputs.capture(code,data,trials,[(3,14)])
    path=tmp_path/'grading.json'; artifact_io.atomic_json(path,lock)
    cfg['environment']['grading']=str(path)
    return cfg,lock


def fixture_children(monkeypatch):
    """Only image metadata is synthetic; worker imports/packaging/arguments are real."""
    actual=subprocess.run
    def run(command,**kwargs):
        if len(command)>4 and Path(command[4]).name=='checkpoint_worker.py':
            assert command[1:4]==['-I','-S','-B']
            worker=Path(command[4])
            bootstrap=("import runpy,sys\n"
                +"sys.path.insert(0,"+repr(str(worker.parents[3]))+")\n"
                +"from ctxpress.harness.jobs import environment as eval_environment\n"
                +"eval_environment.image=lambda ref: dict(id="+repr(IMAGE)+")\n"
                +"sys.argv=["+repr(str(worker))+",*sys.argv[1:]]\n"
                +"runpy.run_path("+repr(str(worker))+",run_name='__main__')\n")
            return actual([command[0],*command[1:4],'-c',bootstrap,*command[5:]],**kwargs)
        pytest.fail('unexpected subprocess in synthetic grading: '+str(command))
    monkeypatch.setattr(eval_grading.subprocess,'run',run)


def test_official_grading_children_use_frozen_code_inputs_and_dependencies(tmp_path,monkeypatch):
    cfg,lock=declared(tmp_path,monkeypatch)
    plan=eval_plan.compile_plan(cfg); directory=evaluation.prepare(plan,tmp_path/'run')
    for descriptor in lock['trees'].values():
        shutil.rmtree(descriptor['root'])
    Path(cfg['environment']['grading']).unlink()
    evaluation.prepare(plan,directory)
    effective,_=eval_inputs.execution(evaluation._verified_plan(directory),directory/'inputs')
    env=effective['environment']
    fixture_children(monkeypatch)
    preflight=eval_grading.preflight(env['grading'],env['grading_root'],3,14)
    assert preflight['entrypoint_imported'] and preflight['experiments_started']==0
    raw=tmp_path/'source.tar'
    with tarfile.open(raw,'w') as archive:
        for name in ('core/source.go','core/source_test.go','go.mod'):
            content=name.encode(); member=tarfile.TarInfo(name); member.size=len(content); archive.addfile(member,io.BytesIO(content))
    output=tmp_path/'grade.json'
    result=eval_grading.execute(env['grading'],env['grading_root'],3,14,raw,'agent-impl-milestone_002','fixture-commit',tmp_path/'package',output)
    assert result.get('resolved') is True and result['infra_invalid'] is False,result
    assert result['grading_manifest_sha256']==lock['sha256']
    assert result['test_only']
    assert json.loads(output.read_text(encoding='utf-8'))['test_only']
    with tarfile.open(tmp_path/'package/source_snapshot.tar') as archive:
        assert archive.getnames()==['core/source.go','go.mod']
    metadata=json.loads((tmp_path/'package/source_snapshot.integrity.json').read_text(encoding='utf-8'))
    assert metadata['expected_count']==2 and metadata['build_manifests']==['go.mod']
    assert metadata['agent_base_image_id']==IMAGE


@pytest.mark.parametrize('key',['code','data','trials','yaml','pathspec'])
def test_mutated_grader_inputs_block_job_launch(tmp_path,monkeypatch,key):
    cfg,lock=declared(tmp_path,monkeypatch)
    directory=evaluation.prepare(eval_plan.compile_plan(cfg),tmp_path/'run')
    root=directory/'inputs/grading'/grading_inputs.folder(lock,key)
    (root/'injected.py').write_text('unreviewed code',encoding='utf-8')
    with pytest.raises(ValueError,match='grading input copy changed'):
        evaluation.schedule(directory,launch=lambda *a:pytest.fail('launched changed grading code'))
    assert evaluation.state(directory)['counts']=={'pending':1}


def test_grading_runtime_change_is_rejected_before_any_grading_process(tmp_path,monkeypatch):
    cfg,lock=declared(tmp_path,monkeypatch)
    directory=evaluation.prepare(eval_plan.compile_plan(cfg),tmp_path/'run')
    original=eval_plan.file_sha256
    monkeypatch.setattr(eval_plan,'file_sha256',lambda path:'changed' if str(path)==lock['runtime']['python'] else original(path))
    with pytest.raises(ValueError,match='grading Python changed'):
        eval_grading.runtime(lock,directory/'inputs/grading',3,14)


def test_declaration_cannot_reference_an_unfrozen_trial_config(tmp_path,monkeypatch):
    cfg,lock=declared(tmp_path,monkeypatch)
    content={key:copy.deepcopy(value) for key,value in lock.items() if key!='sha256'}
    content['boundaries']['3:14']['repo_config']='outside.yaml'
    with pytest.raises(ValueError,match='undeclared input'):
        grading_inputs.verify(grading_inputs._seal(content))


def test_detached_task_resolves_grader_copies_and_keeps_synthetic_evidence(tmp_path,monkeypatch):
    cfg,lock=declared(tmp_path,monkeypatch)
    plan=eval_plan.compile_plan(cfg); directory=evaluation.prepare(plan,tmp_path/'run')
    job_id=plan['jobs'][0]['id']
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET status='running',attempt=1 WHERE id=?",(job_id,))
    for descriptor in lock['trees'].values():
        shutil.rmtree(descriptor['root'])
    Path(cfg['environment']['grading']).unlink()
    script='''import sys
from pathlib import Path
from ctxpress.benchmarks.milestone import checkpoint_run as codex_docker, checkpoint_grading as eval_grading; from ctxpress.harness.jobs import grading_inputs, queue as evaluation

def run(n,j,entry,**kwargs):
    lock=grading_inputs.load(kwargs['grading'],[(n,j)])
    roots,record=eval_grading.runtime(lock,kwargs['grading_root'],n,j,check_images=False)
    assert '/inputs/grading/' in roots['code'].replace('\\\\','/')
    assert Path(roots['trials'],record['repo_config']).is_file()
    return dict(test_only=True,requests=1,rewrites=[dict(request=1,status=200)],grade=dict(resolved=True,infra_invalid=False,test_only=True,grading_manifest_sha256=lock['sha256']))
codex_docker.run=run
evaluation.execute_job(sys.argv[1],sys.argv[2],1)
'''
    result=subprocess.run([sys.executable,'-c',script,str(directory),job_id],cwd=directory/'runtime',
                          env=evaluation._runtime_env(directory),capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    from ctxpress.harness.results.report import report, write_report
    evidence=report(directory)
    assert evidence['grading_manifest_sha256']==lock['sha256']
    assert evidence['grading_scope']==lock['scope']
    assert 'synthetic' in evidence['evidence']
    write_report(directory)
    markup=(directory/'report.html').read_text(encoding='utf-8')
    assert lock['sha256'] in markup and '系统库及外部模型版本尚未整体冻结' in markup


def test_backend_captured_snapshot_uses_declared_grader_not_live_scripts(tmp_path,monkeypatch):
    from ctxpress.benchmarks.milestone import checkpoint_run as codex_docker
    env=SimpleNamespace(src_dirs=['core/'],root_files=['go.mod'],grading='frozen-manifest',grading_root='frozen-grading-root',
                        scripts=tmp_path/'absent-scripts',e2e=tmp_path/'absent-trials')
    def docker(command,**kwargs):
        if 'rev-parse' in command:
            return SimpleNamespace(returncode=0,stdout='fixture-commit\n',stderr='')
        if 'stdout' in kwargs and hasattr(kwargs['stdout'],'write'):
            kwargs['stdout'].write(b'fixture raw archive')
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    called=[]
    def execute(*args):
        called.append(args)
        assert args[:4]==('frozen-manifest','frozen-grading-root',3,14)
        assert Path(args[4]).read_bytes()==b'fixture raw archive'
        assert args[5:7]==('agent-impl-milestone_002','fixture-commit')
        return dict(resolved=True,infra_invalid=False,test_only=True)
    monkeypatch.setattr(codex_docker.subprocess,'run',docker)
    monkeypatch.setattr(eval_grading,'execute',execute)
    result=codex_docker.capture_and_grade('ctxp-0123456789',3,14,'milestone_002','unused-original-trial',
                                       'submitted','agent-impl-milestone_002',tmp_path/'result',env)
    assert len(called)==1 and result['test_only']


def test_wrong_official_boundary_mapping_is_rejected(tmp_path,monkeypatch):
    cfg,lock=declared(tmp_path,monkeypatch)
    content={key:copy.deepcopy(value) for key,value in lock.items() if key!='sha256'}
    content['boundaries']['3:14']['milestone']='milestone_003_sub-01'
    with pytest.raises(ValueError,match='official boundary mapping'):
        grading_inputs.verify(grading_inputs._seal(content))
