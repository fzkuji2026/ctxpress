"""Isolated native preparation, continuous dispatch and official result reading."""
from __future__ import annotations
import argparse, copy, json, sys
from pathlib import Path


def load(request):
    package=Path(__file__).resolve().parents[3]
    if request.get('schema')!='ctxpress.eval.milestone_preflight' or request.get('version')!=1 or request.get('package')!=str(package):
        raise ValueError('invalid frozen native preparation request')
    sys.path.insert(0,str(package))
    from ctxpress.harness.jobs import plan as eval_plan, resources as task_resources, environment as eval_environment, task as task_api
    from ctxpress.benchmarks.milestone import protocol as milestone_protocol
    lock,_=task_resources.read(request['resources'],'swe-milestone',[request['original_task']])
    task_api.verify_remap(request['original_task'],request['task'])
    runtime=lock.get('runtime') or {}
    if (Path(sys.executable).resolve()!=Path(runtime.get('python','')).resolve() or sys.version!=runtime.get('version') or
            sys.platform!=runtime.get('platform') or sys.platform!='linux' or eval_plan.file_sha256(sys.executable)!=runtime.get('sha256')):
        raise ValueError('native preflight runtime differs from the frozen Linux Python')
    official=Path(request['official']).resolve();selected=copy.deepcopy(lock)
    for key,tree in selected['trees'].items():
        root=official/key;actual=eval_environment.workspace(root)
        if not eval_environment.same_tree(actual, tree):
            raise ValueError('native preflight official tree changed: '+key)
        tree['root']=str(root)
    missing=milestone_protocol.requirements({},[request['task']],selected)
    if missing:raise ValueError('; '.join(missing))
    source=official/'code'
    if (source/'manifests/BENCHMARK_VERSION').read_text(encoding='utf-8').strip()!=lock['release']:
        raise ValueError('native source benchmark version differs from the reviewed data release')
    sys.path[:0]=[str(source),str(official/'dependencies')]
    # Optional harness dependencies come only from the frozen input tree.
    from harness.e2e import run_e2e
    if Path(run_e2e.__file__).resolve()!=source/'harness/e2e/run_e2e.py':
        raise ValueError('native preflight imported another harness')
    return source


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('request')
    modes=parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--check',action='store_true');modes.add_argument('--read-grade',action='store_true')
    modes.add_argument('--execute',action='store_true')
    modes.add_argument('--validate-execution',action='store_true')
    args=parser.parse_args(argv)
    request=json.loads(Path(args.request).read_text(encoding='utf-8'));source=load(request)
    if args.validate_execution:
        from ctxpress.benchmarks.milestone.runner import validate
        validate(request);print(json.dumps(dict(execution_inputs='verified',model_calls=0,containers_started=0)));return
    if args.execute:
        from ctxpress.benchmarks.milestone.runner import run
        result=run(request,source);print(json.dumps(result));return
    if args.read_grade:
        from ctxpress.core import artifacts as artifact_io
        from ctxpress.benchmarks.milestone.grade import read
        result=read(request['task'],source,request['trial'])
        artifact_io.atomic_json(Path(request['folder'])/'native-grade.json',result)
        print(json.dumps(dict(task_id=request['task']['id'],collector='verified',model_calls=0,containers_started=0)))
        return
    from ctxpress.benchmarks.milestone.native import prepare
    prepare(request['task'],source,Path(request['folder'])/'native-preparation')
    print(json.dumps(dict(task_id=request['task']['id'],author_bindings='verified',model_calls=0,containers_started=0,
        preparation=str(Path(request['folder'])/'native-preparation'/'preparation.json'),real_run_verified=False)))


if __name__=='__main__':main()
