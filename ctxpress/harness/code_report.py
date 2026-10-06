"""Aggregate independent code samples using frozen author estimator evidence."""
from __future__ import annotations
import json


def summarize(plan,jobs):
    if plan['config'].get('benchmark')!='bigcodebench':return {}
    config=plan['config'];n=config['repeats'];policy=config['run']['code'];groups={};expected={item['id']:item for item in plan['jobs']}
    for spec in plan['jobs']:
        group=groups.setdefault(spec['label'],dict(method=spec['label'],tasks={}))
        identity=spec['task']['id']
        group['tasks'].setdefault(identity,dict(task_id=identity,planned=n,valid=0,passed=0,repeats=set(),table=None,errors=[]))
    for job in jobs:
        spec=json.loads(job['spec']);key=spec['label'];identity=spec['task']['id']
        if spec!=expected.get(spec.get('id')) or job.get('id',spec['id'])!=spec['id']:
            for group in groups.values():
                for task in group['tasks'].values():task['errors'].append('unexpected or changed job binding')
            continue
        group=groups[key];task=group['tasks'][identity]
        if spec['repeat'] in task['repeats']:task['errors'].append('duplicate sample index');continue
        task['repeats'].add(spec['repeat'])
        result=json.loads(job['result']) if job.get('result') else {};grade=result.get('grade') or {}
        if (job['status']!='completed' or grade.get('error') or grade.get('infra_invalid') is not False or
                grade.get('quality_kind')!='code_sample' or type(grade.get('sample_passed')) is not bool or
                grade.get('task_id')!=identity or grade.get('sample_id')!=spec['repeat'] or grade.get('n_samples')!=n or grade.get('options')!=policy):
            task['errors'].append('sample missing or invalid: '+str(spec['repeat']));continue
        from ctxpress.benchmarks.bigcode_bench import BigCodeBench
        observed=BigCodeBench().read_grade(spec['task'],grade.get('report','missing-code-report.json'))
        binding=observed.get('independent_grading') or {};resources=spec.get('resources') or {};images=resources.get('images') or {}
        if (observed.get('report_sha256')!=grade.get('report_sha256') or observed.get('error') or
                observed.get('quality_kind')!='code_sample' or observed.get('sample_passed')!=grade['sample_passed'] or
                observed.get('sample_id')!=spec['repeat'] or observed.get('n_samples')!=n or observed.get('options')!=policy or
                binding.get('agent_image')!=(images.get('agent') or {}).get('id') or
                binding.get('verifier_image')!=((images.get('grading') or {}).get('verifier') or {}).get('id')):
            task['errors'].append('official sample evidence missing, changed or unbound');continue
        table=observed.get('estimator_table')
        if not isinstance(table,dict) or set(table)!={str(k) for k in policy['pass_k'] if k<=n}:
            task['errors'].append('official estimator evidence missing');continue
        if task['table'] is not None and task['table']!=table:
            task['errors'].append('official estimator evidence differs across samples');continue
        task['table']=table;task['valid']+=1;task['passed']+=grade['sample_passed']
    result={}
    for key,group in groups.items():
        tasks=[]
        for row in group['tasks'].values():
            complete=row['valid']==n and not row['errors'] and row['repeats']==set(range(n))
            metrics={str(k):row['table'][str(k)][row['passed']] if complete and k<=n else None for k in policy['pass_k']}
            tasks.append(dict(task_id=row['task_id'],planned_samples=n,valid_samples=row['valid'],passed_samples=row['passed'],
                              complete=complete,pass_at_k=metrics,errors=row['errors']))
        complete=bool(tasks) and all(row['complete'] for row in tasks)
        means={str(k):sum(row['pass_at_k'][str(k)] for row in tasks)/len(tasks) if complete and k<=n else None for k in policy['pass_k']}
        result[key]=dict(kind='code_samples',samples_per_task=n,task_count=len(tasks),complete=complete,options=policy,tasks=tasks,
            planned_samples=n*len(tasks),valid_samples=sum(row['valid_samples'] for row in tasks),pass_at_k=means,
            unavailable={str(k):'insufficient samples' if k>n else 'planned cohort incomplete' for k in policy['pass_k'] if means[str(k)] is None},
            estimator='frozen author bigcodebench.eval.estimate_pass_at_k',generation=dict(host='codex',independent_sessions=True,
                samples_per_task=n,decoding='provider-controlled; temperature/top-p are not configured by this CLI'),
            protocol='ctxpress_comparison')
    return result
