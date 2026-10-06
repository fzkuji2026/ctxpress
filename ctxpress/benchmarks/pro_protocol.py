"""Pro V2 frozen tooling and mandatory fresh regrade evidence."""
from __future__ import annotations
import copy, json, re
from pathlib import Path
from ctxpress.harness import eval_plan

SCHEMA='ctxpress.eval.pro_regrade'


def requirements(config,tasks,lock):
    if not lock:return ['Pro V2: captured harbor/pro_tooling/dependencies, declared base commits and prepared regrade image']
    # Pro's second trial is the independent verifier. Generic Harbor shared
    # image checks apply within each trial, not across the two trial images.
    from .harbor_driver import requirements as harbor_requirements
    standard=copy.deepcopy(lock)
    for task in tasks:
        images=standard['tasks'][task['id']]['images']
        images['grading']={'verifier':copy.deepcopy(images['agent'])}
    missing=harbor_requirements(dict(config,benchmark='terminal-bench'),tasks,standard)
    source=lock['trees'].get('pro_tooling',{}).get('files',{})
    for name in ('locked_codex.py','patch_replay.py'):
        if name not in source:missing.append('Pro V2 frozen tooling: '+name)
    for task in tasks:
        record=lock['tasks'][task['id']]
        if set(record['images']['grading'])!={'verifier'} or record['images'].get('services'):
            missing.append('Pro V2 requires one Agent image and one pristine grading.verifier image: '+task['id'])
        if record.get('gpu_device_ids') or record.get('verifier_gpu_device_ids'):
            missing.append('Pro V2 uses CPU task images: '+task['id'])
        if task['evaluation']['dataset']['revision']!=lock['release']:
            missing.append('Pro resource release must match declared dataset revision: '+task['id'])
    return missing


def regrade_evidence(task,report,digest):
    root=report.resolve().parent.parent;source=root/'pro-regrade.json'
    source_digest=eval_plan.file_sha256(source);record=json.loads(source.read_text(encoding='utf-8'))
    if (not isinstance(record,dict) or record.get('schema')!=SCHEMA or record.get('version')!=1 or record.get('task_id')!=task['id'] or
            record.get('benchmark_version')!='v2' or record.get('base_commit')!=task['initial_state']['base_commit'] or
            record.get('checked_agent_cleanup') is not True or type(record.get('regrade_model_calls')) is not int or record['regrade_model_calls']!=0 or
            record.get('agent_project')==record.get('regrade_project')):
        raise ValueError('fresh replay identity or lifecycle is not proven')
    owners=[]
    for phase,project in (('agent',record['agent_project']),('regrade',record['regrade_project'])):
        if not re.fullmatch(r'ctxp-hb-[0-9a-f]{24}',project):raise ValueError('invalid Pro phase project')
        result=record[phase+'_report'];path=root/project/'result.json'
        if result['path']!=str(path) or eval_plan.file_sha256(path)!=result['sha256']:
            raise ValueError('Pro phase result changed')
        raw=json.loads(path.read_text(encoding='utf-8'))
        if raw.get('task_name')!=task['initial_state'].get('official_task_name',task['id']):
            raise ValueError('Pro phase result belongs to another task')
        resources=root/('resources-harbor-'+project+'.json')
        if eval_plan.file_sha256(resources)!=record[phase+'_resources_sha256']:raise ValueError('Pro phase resources changed')
        owner=json.loads(resources.read_text(encoding='utf-8'))
        if (owner.get('schema')!='ctxpress.eval.harbor_resources' or owner.get('version')!=1 or owner.get('project')!=project or
                owner.get('role','agent')!=('agent' if phase=='agent' else 'verifier') or
                owner.get('cleaned') is not True or owner.get('credentials_may_exist') is not False or
                owner.get('images')!={'main':record[phase+'_image']} or owner.get('pro_base_commit')!=record['base_commit']):
            raise ValueError('Pro phase cleanup or base repository is not proven')
        owners.append(owner)
    if (not owners[0].get('daemon_id') or owners[0]['daemon_id']!=owners[1].get('daemon_id') or
            not owners[0].get('label') or owners[0]['label']!=owners[1].get('label') or owners[1].get('channel') is not None):
        raise ValueError('Pro phases have different resource owners or replay model access')
    if record['regrade_report']!={'path':str(report.resolve()),'sha256':digest}:
        raise ValueError('authoritative result is not the recorded regrade')
    submission=root/'pro-submission'/'model.patch'
    if record['submission']['path']!=str(submission) or eval_plan.file_sha256(submission)!=record['submission']['sha256']:
        raise ValueError('Pro patch transfer changed')
    if eval_plan.file_sha256(source)!=source_digest:raise ValueError('Pro regrade evidence changed while reading')
    return dict(record,path=str(source),sha256=source_digest)
