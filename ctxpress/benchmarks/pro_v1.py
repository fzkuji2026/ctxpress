"""Pro V1 dataset binding and evidence for its independent author Docker pipeline."""
from __future__ import annotations
import ast, json, re
from pathlib import Path
from ctxpress.harness import eval_plan
from ctxpress.harness.task import validate
from .swe_bench import source_file, rows

SCHEMA='ctxpress.eval.pro_v1_grade'


def initial_state(row):
    if (not isinstance(row,dict) or not isinstance(row.get('instance_id'),str) or
            not re.fullmatch(r'instance_[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9a-f]{40}(?:-v[0-9a-f]{40})?',row['instance_id'])):
        raise ValueError('Pro V1 requires an official instance ID; its SHA is not the base commit')
    if not isinstance(row.get('repo'),str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',row['repo']):
        raise ValueError('Pro V1 requires an explicit repository')
    if not isinstance(row.get('base_commit'),str) or not re.fullmatch(r'[0-9a-f]{40}',row['base_commit']):
        raise ValueError('Pro V1 requires the dataset base_commit')
    problem=row.get('problem_statement')
    if not isinstance(problem,str) or not problem.strip():raise ValueError('Pro V1 requires problem_statement')
    state=dict(repo=row['repo'],base_commit=row['base_commit'],pro_version='v1',problem_statement=problem)
    for key in ('requirements','interface'):
        value=row.get(key) or ''
        if not isinstance(value,str):raise ValueError('invalid Pro V1 '+key)
        state[key]=value
        if value.strip():state['problem_statement']+='\n\n'+key.capitalize()+':\n'+value
    return state


def normalized(row):
    initial_state(row)
    result={key:row.get(key) for key in ('instance_id','repo','base_commit','before_repo_set_cmd')}
    for key in ('fail_to_pass','pass_to_pass','selected_test_files_to_run'):
        value=row.get(key)
        if isinstance(value,str):
            try:value=ast.literal_eval(value)
            except (ValueError,SyntaxError):raise ValueError('invalid Pro V1 '+key) from None
        if (not isinstance(value,list) or any(not isinstance(item,str) or not item for item in value) or
                key=='fail_to_pass' and not value):
            raise ValueError('Pro V1 requires declared '+key+' test IDs')
        # The frozen author uses eval(). Only literal string lists reach it.
        result[key]=repr(value)
    if not isinstance(row.get('before_repo_set_cmd'),str):raise ValueError('Pro V1 requires before_repo_set_cmd')
    return result


def task_instances(data,manifest,meta):
    path=source_file(data);found,digest=rows(path)
    if meta.get('subset')!='public':raise ValueError('Pro V1 supports its explicitly declared public dataset')
    inputs=[dict(role='grading',path=str(path),sha256=digest),
            dict(role='runtime',path=str(manifest),sha256=eval_plan.file_sha256(manifest))]
    tasks=[];seen=set()
    for row in found:
        state=initial_state(row);normalized(row);identity=row['instance_id']
        if identity in seen:raise ValueError('duplicate Pro V1 instance ID')
        seen.add(identity)
        tasks.append(validate(dict(id=identity,benchmark='swe-bench-pro',start_mode='task_start',initial_state=state,
            inputs=[dict(item) for item in inputs],evaluation=dict(kind='official-pro-v1-docker',
                dataset=dict(name='swe-bench-pro',revision=meta['revision'],benchmark_version='v1',subset='public',
                             evidence='local dataset manifest declaration')))))
    return tasks


def instance(task):
    inputs=[item for item in task['inputs'] if item['role']=='grading' and Path(item['path']).suffix in ('.json','.jsonl')]
    if len(inputs)!=1:raise ValueError('Pro V1 requires one frozen dataset')
    source=inputs[0];found,digest=rows(Path(source['path']))
    if digest!=source['sha256']:raise ValueError('Pro V1 grading dataset changed')
    selected=[row for row in found if row.get('instance_id')==task['id']]
    if len(selected)!=1 or initial_state(selected[0])!=task['initial_state']:
        raise ValueError('Pro V1 grading instance differs from its Agent binding')
    return normalized(selected[0])


def requirements(config,tasks,lock):
    if not lock:return ['Pro V1: frozen pro_v1/dependencies trees, Linux Python and prepared Agent/verifier images']
    missing=[];runtime=lock.get('runtime') or {};version=re.match(r'(\d+)\.(\d+)',runtime.get('version',''))
    if runtime.get('platform')!='linux' or not version or tuple(map(int,version.groups()))<(3,10):
        missing.append('Pro V1 requires pinned Linux Python 3.10 or later')
    source=lock['trees'].get('pro_v1',{}).get('files',{})
    for name in ('swe_bench_pro_eval.py','helper_code/image_uri.py'):
        if name not in source:missing.append('Pro V1 author input: '+name)
    dependencies=lock['trees'].get('dependencies',{}).get('files',{})
    for library in ('pandas','docker','tqdm'):
        if not any(re.fullmatch(library+r'-[^/]+\.dist-info/METADATA',name) for name in dependencies):
            missing.append('Pro V1 frozen dependencies: '+library+' metadata and transitive dependencies')
    for task in tasks:
        try:instance(task)
        except (ValueError,KeyError) as error:missing.append(str(error)+': '+task['id'])
        for name in ('run_scripts/'+task['id']+'/run_script.sh','run_scripts/'+task['id']+'/parser.py',
                     'dockerfiles/base_dockerfile/'+task['id']+'/Dockerfile','dockerfiles/instance_dockerfile/'+task['id']+'/Dockerfile'):
            if name not in source:missing.append('Pro V1 selected task input: '+name)
        resource=lock['tasks'][task['id']]
        if set(resource['images']['grading'])!={'verifier'} or resource['images'].get('services'):
            missing.append('Pro V1 requires one Agent and grading.verifier image: '+task['id'])
        if resource.get('gpu_device_ids') or resource.get('verifier_gpu_device_ids') or resource.get('verifier_cap_add'):
            missing.append('Pro V1 uses unprivileged CPU environments: '+task['id'])
        if task['evaluation']['dataset']['revision']!=lock['release']:
            missing.append('Pro V1 resources must match the dataset release: '+task['id'])
    return missing


def read_grade(task,report):
    path=Path(report);failure=dict(resolved=None,infra_invalid=None,failure_kind='grading_error')
    try:
        digest=eval_plan.file_sha256(path);raw=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(raw,dict) or set(raw)!={task['id']} or type(raw[task['id']]) is not bool:
            raise ValueError('author verdict does not bind one boolean to the selected task')
        root=path.parent;source=root/'pro-v1-grade.json';evidence_digest=eval_plan.file_sha256(source)
        record=json.loads(source.read_text(encoding='utf-8'));attempt=root.parent
        if (not isinstance(record,dict) or record.get('schema')!=SCHEMA or record.get('version')!=1 or record.get('benchmark_version')!='v1' or
                record.get('task_id')!=task['id'] or record.get('base_commit')!=task['initial_state']['base_commit'] or
                record.get('report')!={'path':str(path.resolve()),'sha256':digest}):
            raise ValueError('Pro V1 independent grading identity changed')
        owners=[];project=record.get('project')
        if not isinstance(project,str) or not re.fullmatch(r'ctxp-sw-[0-9a-f]{24}',project):raise ValueError('invalid Pro V1 project')
        for role in ('agent','verifier'):
            journal=attempt/('resources-swe-'+role+'.json')
            if eval_plan.file_sha256(journal)!=record[role+'_resources_sha256']:raise ValueError('Pro V1 cleanup evidence changed')
            owner=json.loads(journal.read_text(encoding='utf-8'))
            if (owner.get('schema')!='ctxpress.eval.swe_resources' or owner.get('version')!=1 or owner.get('role')!=role or
                    owner.get('project')!=project or owner.get('container')!=project+('-agent' if role=='agent' else '-grade') or
                    not isinstance(owner.get('container_id'),str) or not owner['container_id'] or owner.get('cleaned') is not True or
                    owner.get('credentials_may_exist') is not False or owner.get('image')!=record[role+'_image'] or
                    owner.get('base_commit')!=record['base_commit']):raise ValueError('Pro V1 clean environment is not proven')
            owners.append(owner)
        if (not owners[0].get('daemon_id') or owners[0]['daemon_id']!=owners[1].get('daemon_id') or
                not owners[0].get('label') or owners[0]['label']!=owners[1].get('label') or
                owners[0]['container_id']==owners[1]['container_id'] or owners[1].get('channel') is not None):
            raise ValueError('Pro V1 phases have different owners or verifier model access')
        for field,expected in (('prediction',attempt/'predictions.jsonl'),
                               ('parser_output',root/task['id']/'_output.json')):
            item=record[field]
            if item['path']!=str(expected) or eval_plan.file_sha256(expected)!=item['sha256']:
                raise ValueError('Pro V1 submission or parser output changed')
        parsed=json.loads((root/task['id']/'_output.json').read_text(encoding='utf-8'))
        validate_output(parsed)
        if eval_plan.file_sha256(path)!=digest or eval_plan.file_sha256(source)!=evidence_digest:
            raise ValueError('Pro V1 grading evidence changed while reading')
    except (OSError,ValueError,KeyError,TypeError) as error:return dict(failure,error=str(error))
    return dict(resolved=raw[task['id']],infra_invalid=False,instance_id=task['id'],report=str(path.resolve()),report_sha256=digest,
        raw_instance_report=parsed,fresh_regrade=dict(record,path=str(source),sha256=evidence_digest),
        authoritative_phase='fresh_regrade',published_protocol_reproduced=False)


def validate_output(output):
    if not isinstance(output,dict) or not isinstance(output.get('tests'),list):raise ValueError('Pro V1 parser tests are missing')
    names=[]
    for row in output['tests']:
        if (not isinstance(row,dict) or not isinstance(row.get('name'),str) or not row['name'] or
                not isinstance(row.get('status'),str) or not row['status']):raise ValueError('invalid Pro V1 parser test row')
        names.append(row['name'])
    if len(set(names))!=len(names):raise ValueError('ambiguous Pro V1 duplicate test outcomes')
