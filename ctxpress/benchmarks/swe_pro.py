"""Versioned Pro pipelines; both grade in independent prepared environments."""
from __future__ import annotations
import json, re
from pathlib import Path
from ctxpress.core import toml
from ctxpress.harness import eval_plan
from .harbor_tasks import HarborTasks


class SWEPro(HarborTasks):
    NAME='swe-bench-pro'
    TITLE='SWE-bench Pro'
    REQUIRE_MANIFEST=True

    def describe(self):
        result=super().describe()
        result.update(adapter_version=1,suite='V1 public; V2 public/HARD-51',
            implemented_versions=['v1','v2'],pending_versions=[],
            official_grading='V1 author main/local Docker; V2 frozen LockedCodex and fresh PatchReplayAgent trial',
            task_scope='explicit versioned local datasets, task IDs and declared base commits',
            runtime_apis=['V1 author local Docker pipeline','V2 legacy_trial/single_step'],
            prediction_export_supported=True,
            required_resources=['versioned local Pro data','prepared Agent/verifier images','fixed Codex binary',
                'V1: pro_v1/dependencies; V2: harbor/pro_tooling/dependencies'],
            execution_limits=['Linux CPU; V2 single-step tasks','prepared Agent and fresh regrade images',
                'private networks in all phases; ctxpress_comparison, not the Modal published protocol'])
        return result

    def task_instances(self,data):
        root=Path(data).expanduser().resolve();root=root if root.is_dir() else root.parent;manifest=root/'dataset_manifest.json'
        digest=eval_plan.file_sha256(manifest);meta=json.loads(manifest.read_text(encoding='utf-8'))
        if (not isinstance(meta,dict) or meta.get('dataset')!=self.NAME or meta.get('benchmark_version') not in ('v1','v2') or
                not isinstance(meta.get('revision'),str) or not meta['revision'].strip()):
            raise ValueError('declare Pro dataset, revision and benchmark_version=v1/v2')
        if meta['benchmark_version']=='v1':
            from . import pro_v1
            tasks=pro_v1.task_instances(data,manifest,meta)
            if eval_plan.file_sha256(manifest)!=digest:raise ValueError('Pro manifest changed during discovery')
            return tasks
        if meta.get('subset') not in ('public','hard51') or not isinstance(meta.get('base_commits'),dict):
            raise ValueError('Pro V2 requires subset=public/hard51 and declared base_commits')
        tasks=super().task_instances(data)
        if eval_plan.file_sha256(manifest)!=digest:raise ValueError('Pro dataset manifest changed during discovery')
        hard=None;extra=[]
        if meta['subset']=='hard51':
            source=root/'hard51_ids.txt';source_digest=eval_plan.file_sha256(source)
            identifiers=source.read_text(encoding='utf-8').splitlines()
            if (not identifiers or len(set(identifiers))!=len(identifiers) or
                    any(not re.fullmatch(r'[A-Za-z0-9_.-]+',value) for value in identifiers)):
                raise ValueError('Pro HARD-51 requires an explicit unique task ID list')
            if eval_plan.file_sha256(source)!=source_digest:raise ValueError('Pro subset membership changed')
            hard=set(identifiers);extra=[dict(role='runtime',path=str(source),sha256=source_digest)]
        found=[]
        for task in tasks:
            if hard is not None and task['id'] not in hard:continue
            commit=meta['base_commits'].get(task['id'])
            if not isinstance(commit,str) or not re.fullmatch(r'[0-9a-f]{40}',commit):
                raise ValueError('declare each Pro task base_commit from the matching official dataset revision')
            from .harbor_driver import task_directory
            directory=task_directory(task);config=toml.load(directory/'task.toml')
            if config.get('agent',{}).get('network_mode')!='no-network':
                raise ValueError('Pro V2 task must declare the official offline Agent phase')
            if task['initial_state'].get('steps') or 'verifier_environment' in task['initial_state']:
                raise ValueError('Pro V2 uses two fresh single-step trials, not Harbor collect/separate mode')
            environment=task['initial_state']['environment']
            if environment.get('os','linux')!='linux' or environment.get('gpus',0) or environment.get('tpu'):
                raise ValueError('Pro V2 public tasks require Linux CPU images')
            for name in ('test.sh','run_script.sh','parser.py','config.json','test_patch.patch'):
                if not any(item['role']=='grading' and Path(item['path'])==directory/'tests'/name for item in task['inputs']):
                    raise ValueError('Pro V2 requires frozen author tests/'+name)
            if (directory/'environment/docker-compose.yaml').exists():raise ValueError('Pro V2 requires its single prepared task image')
            task['initial_state'].update(pro_version='v2',base_commit=commit)
            task['inputs'].extend(dict(item) for item in extra)
            task['evaluation'].update(kind='official-pro-v2-fresh-regrade')
            task['evaluation']['dataset'].update(benchmark_version='v2',subset=meta['subset'])
            found.append(task)
        return found

    def plan_requirements(self,config,tasks,lock):
        if all(task['initial_state']['pro_version']=='v1' for task in tasks):
            from .pro_v1 import requirements
            return requirements(config,tasks,lock)
        from .pro_protocol import requirements
        return requirements(config,tasks,lock)

    def data_directories(self,tasks,root):
        if all(task['initial_state']['pro_version']=='v1' for task in tasks):return []
        return super().data_directories(tasks,root)

    def agent_instruction(self,task):
        if task['initial_state']['pro_version']=='v1':
            from ctxpress.harness.task import validate
            validate(task)
            if task['benchmark']!=self.NAME:raise ValueError('task belongs to another benchmark')
            return task['initial_state']['problem_statement']
        return super().agent_instruction(task)

    def execute(self,task,entry,config,job,*,paths,folder,label):
        if task['initial_state']['pro_version']=='v1':
            from .swe_driver import execute
            return execute(self,task,entry,config,job,paths=paths,folder=folder,label=label)
        return super().execute(task,entry,config,job,paths=paths,folder=folder,label=label)

    def recover(self,path,label):
        if Path(path).name.startswith('resources-swe-'):
            from .swe_driver import recover
            return recover(path,label)
        return super().recover(path,label)

    def read_grade(self,task,report):
        if task['initial_state']['pro_version']=='v1':
            self.agent_instruction(task)
            from .pro_v1 import read_grade
            return read_grade(task,report)
        grade=super().read_grade(task,report)
        if not grade.get('valid_rewards'):return grade
        reward=grade['rewards'].get('reward')
        if reward not in (0,1):
            return dict(grade,resolved=None,valid_rewards=False,infra_invalid=None,failure_kind='grading_error',error='Pro binary reward missing or invalid')
        from .pro_protocol import regrade_evidence
        try:evidence=regrade_evidence(task,Path(report),grade['report_sha256'])
        except (OSError,ValueError,KeyError,TypeError) as error:
            return dict(grade,resolved=None,valid_rewards=False,infra_invalid=None,failure_kind='grading_error',error='Pro fresh regrade evidence invalid: '+str(error))
        return dict(grade,resolved=bool(reward),authoritative_phase='fresh_regrade',fresh_regrade=evidence,
                    published_protocol_reproduced=False)
