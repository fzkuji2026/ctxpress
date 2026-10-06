"""Official SWE-Milestone grading in an isolated child, from declared inputs (harness.jobs.grading_inputs)."""
from __future__ import annotations
import hashlib, importlib.util, json, os, re, subprocess, sys
from pathlib import Path
from ctxpress.harness.jobs import environment as eval_environment, plan as eval_plan
from ctxpress.harness.jobs.grading_inputs import folder, load, verify, verify_copies


def runtime(lock,root,n,j,check_images=True):
    verify(lock,[(n,j)])
    verify_copies(lock,root)
    if eval_plan.file_sha256(lock['runtime']['python']) != lock['runtime']['sha256']:
        raise ValueError('declared grading Python changed')
    if check_images:
        for record in lock['images'].values():
            if eval_environment.image(record['id'])['id'] != record['id']:
                raise ValueError('declared grading image is unavailable')
    roots = {key:str(Path(root).resolve()/folder(lock,key)) for key in lock['trees']}
    return roots,lock['boundaries'][f'{n}:{j}']


def command(lock,manifest,root,n,j):
    worker = Path(__file__).with_name('checkpoint_worker.py').resolve()
    return [lock['runtime']['python'],'-I','-S','-B',str(worker),'--manifest',str(manifest),'--root',str(root),'--n',str(n),'--j',str(j)]


def preflight(manifest,root,n,j):
    """Import copied official code before model use; no evaluator execution."""
    lock = load(manifest,[(n,j)])
    runtime(lock,root,n,j)
    process = subprocess.run([*command(lock,manifest,root,n,j),'check'],capture_output=True,text=True)
    if process.returncode:
        raise ValueError('frozen official grading preflight failed: '+process.stderr[-4000:])
    return json.loads(process.stdout)


def execute(manifest,root,n,j,snapshot,tag,commit,package,output):
    lock = load(manifest,[(n,j)])
    runtime(lock,root,n,j)
    common = command(lock,manifest,root,n,j)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    for operation,arguments in (
        ('package',['--snapshot',str(snapshot),'--tag',tag,'--commit',commit,'--package',str(package)]),
        ('grade',['--snapshot',str(Path(package)/'source_snapshot.tar'),'--output',str(output)])):
        # Version checks and copied input validation also run inside each child.
        process = subprocess.run([*common,operation,*arguments],env=env,capture_output=True,text=True)
        Path(output).parent.mkdir(parents=True,exist_ok=True)
        (Path(output).parent/(Path(output).stem+'-'+operation+'.log')).write_text(process.stdout+process.stderr,encoding='utf-8')
        if process.returncode:
            return dict(error='frozen official '+operation+' failed',infra_invalid=True,grading_manifest_sha256=lock['sha256'])
    if not Path(output).is_file():
        return dict(error='official evaluator did not produce a report',infra_invalid=True,grading_manifest_sha256=lock['sha256'])
    grade = json.loads(Path(output).read_text(encoding='utf-8'))
    result = dict(resolved=grade.get('resolved'),test_summary=grade.get('test_summary'),infra_invalid=grade.get('infra_invalid'),
                report=str(output),report_sha256=eval_plan.file_sha256(output),grading_manifest_sha256=lock['sha256'],grading_scope=lock['scope'],images=lock['images'],
                milestone_id=lock['boundaries'][f'{n}:{j}']['milestone'],graded_commit=commit)
    if grade.get('test_only'):
        result['test_only'] = True
    return result
