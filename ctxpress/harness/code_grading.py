"""Independent BigCodeBench container entry; official checks execute only here."""
from __future__ import annotations
import argparse, hashlib, importlib.metadata, inspect, json, math, os, re, sys
from pathlib import Path


def load(request):
    official=Path(request['official']);source=official/'bigcodebench'
    sys.path[:0]=[request['package'],str(source),str(official/'dependencies')]
    if sys.platform!='linux' or tuple(sys.version_info[:2])!=tuple(map(int,re.match(r'(\d+)\.(\d+)',request['host_python']).groups())):
        raise ValueError('BigCodeBench grading Python ABI differs from the prepared frozen dependencies')
    import bigcodebench
    from bigcodebench import eval as checker
    from bigcodebench.gen import util as trusted
    if (Path(bigcodebench.__file__).resolve()!=source/'bigcodebench/__init__.py' or
            bigcodebench.__version__=='local-dev' or bigcodebench.__version__!=importlib.metadata.version('bigcodebench')):
        raise ValueError('BigCodeBench source and real distribution metadata differ')
    fields={'code','test_code','entry_point','max_as_limit','max_data_limit','max_stack_limit','min_time_limit','gt_time_limit'}
    if (not fields<=set(inspect.signature(checker.untrusted_check).parameters) or
            not {'code','test_code','task_id','max_as_limit','max_data_limit','max_stack_limit','min_time_limit'}<=
                set(inspect.signature(trusted.trusted_check).parameters) or
            not {'num_samples','num_correct','k'}<=set(inspect.signature(checker.estimate_pass_at_k).parameters) or
            checker.PASS!='pass' or checker.FAIL!='fail' or checker.TIMEOUT!='timeout'):
        raise ValueError('unsupported frozen BigCodeBench local check/estimator API')
    for function,path in ((checker.untrusted_check,'bigcodebench/eval/__init__.py'),
                          (trusted.trusted_check,'bigcodebench/gen/util/__init__.py')):
        if Path(inspect.getfile(function)).resolve()!=source/path:raise ValueError('BigCodeBench checker origin changed')
    return checker,trusted,bigcodebench.__version__


def evaluate(request,official,problem,solution):
    from ctxpress.benchmarks.code_protocol import options,SCHEMA
    checker,trusted,version=official;policy=options(request['options'])
    if problem['task_id']!=request['task_id'] or not isinstance(solution,str):raise ValueError('BigCodeBench sample identity changed')
    limits={key:policy[key] for key in ('max_as_limit','max_data_limit','max_stack_limit','min_time_limit')}
    # Keep the author's fixed default and explicit policy; no image env override.
    os.environ.pop('BIGCODEBENCH_TIMEOUT_PER_TASK',None)
    reference=trusted.trusted_check(code=problem['complete_prompt']+'\n'+problem['canonical_solution'],
        test_code=problem['test'],task_id=problem['task_id'],**limits)
    if not isinstance(reference,dict) or reference.get('task_id')!=problem['task_id']:raise ValueError('official groundtruth check returned another task')
    timing=reference.get('time')
    valid=type(timing) in (int,float) and math.isfinite(timing) and timing>=0
    result=dict(schema=SCHEMA,version=1,task_id=problem['task_id'],sample_id=request['sample_id'],n_samples=request['n_samples'],
        dataset=request['dataset'],options=policy,solution_sha256=hashlib.sha256(solution.encode()).hexdigest(),
        groundtruth_valid=valid,groundtruth=reference,author_version=version,
        grading_runtime=dict(python=sys.executable,version=sys.version,platform=sys.platform),
        author_interfaces=['trusted_check','untrusted_check','estimate_pass_at_k'],protocol='ctxpress_comparison')
    if not valid:
        result.update(status=None,details=None,estimator_table={},infra_invalid=True);return result
    evaluated=problem['code_prompt']+'\n    pass\n'+solution if policy['calibrated'] else solution
    # The official evaluate() uses 20s when a valid reference timing is zero.
    status,details=checker.untrusted_check(code=evaluated,test_code=problem['test'],entry_point=problem['entry_point'],
        gt_time_limit=timing if timing else 20,**limits)
    if status not in (checker.PASS,checker.FAIL,checker.TIMEOUT) or not isinstance(details,dict):
        raise ValueError('unsupported official BigCodeBench sample outcome')
    n=request['n_samples']
    if type(n) is not int or n<1:raise ValueError('BigCodeBench sample count must be positive')
    # A report may later aggregate independent sessions. Preserve a lookup from
    # the *actual frozen author estimator* instead of importing it on the host.
    table={str(k):checker.estimate_pass_at_k(num_samples=n,num_correct=list(range(n+1)),k=k).tolist()
           for k in policy['pass_k'] if k<=n}
    result.update(status=status,details=details,estimator_table=table,infra_invalid=False)
    return result


def write_report(path,result):
    """Keep the official report private and readable by its host output owner."""
    from ctxpress.harness import eval_plan
    path=Path(path)
    if path.name!='sample-result.json':raise ValueError('BigCodeBench requires its official sample report path')
    owner=path.parent.stat()
    eval_plan.atomic_json(path,result)
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:os.fchown(descriptor,owner.st_uid,owner.st_gid)
    finally:os.close(descriptor)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('request');parser.add_argument('--check',action='store_true')
    args=parser.parse_args(argv);request=json.loads(Path(args.request).read_text(encoding='utf-8'))
    if request.get('schema')!='ctxpress.eval.bigcode_grade_request' or request.get('version')!=1:raise ValueError('invalid code grading request')
    official=load(request)
    from ctxpress.harness import eval_plan
    if args.check:
        print(json.dumps(dict(imports='verified',framework='bigcodebench',version=official[2],model_calls=0)));return
    for field in ('problem','solution'):
        if eval_plan.file_sha256(request[field])!=request[field+'_sha256']:raise ValueError('BigCodeBench grading input changed')
    problem=json.loads(Path(request['problem']).read_text(encoding='utf-8'));solution=Path(request['solution']).read_text(encoding='utf-8')
    result=evaluate(request,official,problem,solution);write_report(request['result'],result)


if __name__=='__main__':main()
