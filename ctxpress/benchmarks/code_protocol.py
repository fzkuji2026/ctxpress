"""Frozen BigCodeBench data, evaluation policy and independent-score evidence."""
from __future__ import annotations
import hashlib, json, math, re
from pathlib import Path
from ctxpress.harness import eval_plan
from .bigcode_bench import initial_state
from .swe_bench import rows

SCHEMA='ctxpress.eval.bigcode_sample'
PROOF='ctxpress.eval.bigcode_resources'
DEFAULTS=dict(pass_k=[1,5,10],calibrated=True,min_time_limit=1,max_as_limit=30*1024,max_data_limit=30*1024,max_stack_limit=10)


def options(value=None):
    if value is None:value={}
    if not isinstance(value,dict) or set(value)-set(DEFAULTS):raise ValueError('invalid BigCodeBench evaluation options')
    result=dict(DEFAULTS)|value;ks=result['pass_k']
    if (not isinstance(ks,list) or not ks or any(type(k) is not int or k<1 for k in ks) or len(set(ks))!=len(ks)):
        raise ValueError('declare unique positive BigCodeBench pass_k values')
    result['pass_k']=list(ks)
    if type(result['calibrated']) is not bool:raise ValueError('BigCodeBench calibrated must be boolean')
    for key in ('min_time_limit','max_as_limit','max_data_limit','max_stack_limit'):
        eval_plan._positive(result[key],key,integer=key!='min_time_limit')
    return result


def instance(task):
    files=[item for item in task['inputs'] if item['role']=='grading' and Path(item['path']).suffix in ('.json','.jsonl')]
    if len(files)!=1:raise ValueError('BigCodeBench requires one frozen dataset')
    found,digest=rows(Path(files[0]['path']))
    if digest!=files[0]['sha256']:raise ValueError('BigCodeBench grading dataset changed')
    selected=[row for row in found if row.get('task_id')==task['id']]
    if len(selected)!=1 or initial_state(selected[0],task['initial_state']['split'])!=task['initial_state']:
        raise ValueError('BigCodeBench grading instance differs from its Agent binding')
    row=selected[0]
    for key in ('complete_prompt','instruct_prompt','code_prompt','canonical_solution','test','entry_point'):
        if not isinstance(row.get(key),str) or not row[key].strip():raise ValueError('BigCodeBench grading requires '+key)
    return {key:row[key] for key in ('task_id','complete_prompt','instruct_prompt','code_prompt','canonical_solution','test','entry_point')}


def requirements(config,tasks,lock):
    if not lock:return ['BigCodeBench: frozen bigcodebench/dependencies, Linux Python and prepared Agent/verifier images']
    missing=[];runtime=lock.get('runtime') or {};version=re.match(r'(\d+)\.(\d+)',runtime.get('version',''))
    if runtime.get('platform')!='linux' or not version or tuple(map(int,version.groups()))<(3,10):
        missing.append('BigCodeBench requires pinned Linux Python 3.10 or later')
    source=lock['trees'].get('bigcodebench',{}).get('files',{})
    for name in ('pyproject.toml','bigcodebench/__init__.py','bigcodebench/_version.py','bigcodebench/eval/__init__.py',
                 'bigcodebench/eval/utils.py','bigcodebench/eval/_special_oracle.py','bigcodebench/gen/__init__.py','bigcodebench/gen/util/__init__.py'):
        if name not in source:missing.append('BigCodeBench frozen source: '+name)
    dependencies=lock['trees'].get('dependencies',{}).get('files',{})
    for library in ('bigcodebench','docker','numpy'):
        if not any(re.fullmatch(library+r'-[^/]+\.dist-info/METADATA',name) for name in dependencies):
            missing.append('BigCodeBench frozen dependencies: '+library+' metadata and full evaluation libraries')
    for task in tasks:
        try:instance(task)
        except (ValueError,KeyError) as error:missing.append(str(error)+': '+task['id'])
        resource=lock['tasks'][task['id']]
        if set(resource['images']['grading'])!={'verifier'} or resource['images'].get('services'):
            missing.append('BigCodeBench requires one Agent and grading.verifier image: '+task['id'])
        if resource.get('gpu_device_ids') or resource.get('verifier_gpu_device_ids') or resource.get('verifier_cap_add'):
            missing.append('BigCodeBench uses unprivileged CPU environments: '+task['id'])
        if task['evaluation']['dataset']['revision']!=lock['release']:
            missing.append('BigCodeBench resources must match dataset revision: '+task['id'])
    return missing


def read_grade(task,report):
    path=Path(report);failure=dict(resolved=None,infra_invalid=None,failure_kind='grading_error')
    try:
        digest=eval_plan.file_sha256(path);raw=json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(raw,dict) or raw.get('schema')!=SCHEMA or raw.get('version')!=1 or raw.get('task_id')!=task['id'] or
                raw.get('dataset')!=task['evaluation']['dataset'] or type(raw.get('sample_id')) is not int or raw['sample_id']<0 or
                type(raw.get('n_samples')) is not int or raw['n_samples']<=raw['sample_id']):
            raise ValueError('BigCodeBench result does not bind the task and sample identity')
        policy=options(raw['options']);root=path.parent;attempt=root.parent.parent
        source=root/'code-resources.json';source_digest=eval_plan.file_sha256(source);record=json.loads(source.read_text(encoding='utf-8'))
        if (not isinstance(record,dict) or record.get('schema')!=PROOF or record.get('version')!=1 or record.get('task_id')!=task['id'] or
                record.get('report')!={'path':str(path.resolve()),'sha256':digest} or
                not isinstance(record.get('project'),str) or not re.fullmatch(r'ctxp-sw-[0-9a-f]{24}',record['project'])):
            raise ValueError('BigCodeBench independent grading proof changed')
        owners=[]
        for role in ('agent','verifier'):
            journal=attempt/('resources-swe-'+role+'.json')
            if eval_plan.file_sha256(journal)!=record[role+'_resources_sha256']:raise ValueError('BigCodeBench cleanup evidence changed')
            owner=json.loads(journal.read_text(encoding='utf-8'))
            if (not isinstance(owner,dict) or owner.get('schema')!='ctxpress.eval.swe_resources' or owner.get('role')!=role or
                    owner.get('project')!=record['project'] or owner.get('task_id')!=task['id'] or owner.get('cleaned') is not True or
                    owner.get('credentials_may_exist') is not False or not owner.get('container_id') or
                    owner.get('image')!=record[role+'_image']):raise ValueError('BigCodeBench phase identity or cleanup is missing')
            owners.append(owner)
        if (not owners[0].get('daemon_id') or owners[0]['daemon_id']!=owners[1].get('daemon_id') or
                not owners[0].get('label') or owners[0]['label']!=owners[1].get('label') or owners[1].get('channel') is not None or
                owners[0]['container_id']==owners[1]['container_id']):raise ValueError('BigCodeBench verifier isolation is not proven')
        samples=attempt/'samples.jsonl';samples_digest=eval_plan.file_sha256(samples)
        if record.get('samples')!={'path':str(samples.resolve()),'sha256':samples_digest}:raise ValueError('BigCodeBench sample export changed')
        selected=[json.loads(line) for line in samples.read_text(encoding='utf-8').splitlines() if line.strip()]
        if (len(selected)!=1 or selected[0].get('task_id')!=task['id'] or
                not isinstance(selected[0].get('solution'),str) or raw.get('solution_sha256')!=
                hashlib.sha256(selected[0]['solution'].encode()).hexdigest()):raise ValueError('BigCodeBench scored another solution')
        if eval_plan.file_sha256(path)!=digest or eval_plan.file_sha256(source)!=source_digest:raise ValueError('BigCodeBench evidence changed while reading')
        if raw.get('groundtruth_valid') is not True:
            return dict(failure,infra_invalid=True,error='official reference solution did not pass in the evaluation environment',
                        raw_instance_report=raw,report=str(path.resolve()),report_sha256=digest)
        if raw.get('status') not in ('pass','fail','timeout') or not isinstance(raw.get('details'),dict):raise ValueError('official code sample result missing')
        table=raw['estimator_table'];n=raw['n_samples']
        if not isinstance(table,dict) or set(table)!={str(k) for k in policy['pass_k'] if k<=n}:raise ValueError('official estimator table is incomplete')
        for values in table.values():
            if (not isinstance(values,list) or len(values)!=n+1 or any(type(value) not in (int,float) or not math.isfinite(value) or
                    not 0<=value<=1 for value in values)):raise ValueError('invalid official estimator values')
    except (OSError,ValueError,KeyError,TypeError) as error:return dict(failure,error=str(error))
    return dict(resolved=None,infra_invalid=False,quality_kind='code_sample',sample_passed=raw['status']=='pass',
        sample_id=raw['sample_id'],task_id=task['id'],n_samples=n,options=policy,estimator_table=table,
        raw_instance_report=raw,report=str(path.resolve()),report_sha256=digest,
        independent_grading=dict(record,path=str(source),sha256=source_digest),published_protocol_reproduced=False)
