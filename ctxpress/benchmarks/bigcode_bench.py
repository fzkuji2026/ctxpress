"""Versioned BigCodeBench prompts and code samples, separate from issue/patch tasks."""
from __future__ import annotations
import json, re
from pathlib import Path
from ctxpress.harness import eval_plan
from ctxpress.harness.task import validate
from .fresh import FreshAdapter
from .swe_bench import source_file, rows


def initial_state(row,split):
    if not isinstance(row,dict) or not isinstance(row.get('task_id'),str) or not re.fullmatch(r'BigCodeBench/\d+',row['task_id']):
        raise ValueError('BigCodeBench requires official task IDs')
    if split not in ('complete','instruct'):raise ValueError('choose BigCodeBench complete/instruct')
    prompt=row.get(split+'_prompt');entry=row.get('entry_point')
    if not isinstance(prompt,str) or not prompt.strip() or not isinstance(entry,str) or not entry.isidentifier():
        raise ValueError('BigCodeBench requires its selected prompt and entry_point')
    return dict(split=split,problem_statement=prompt,entry_point=entry,seed_code=prompt if split=='complete' else '')


class BigCodeBench(FreshAdapter):
    NAME='bigcodebench'

    def describe(self):
        return dict(name=self.NAME,title='BigCodeBench',adapter_version=1,suite='Complete/Instruct; Full/Hard',mode='task-start',
            backends=['codex_docker'],from_task_start=True,catalog_supported=True,task_plan_supported=True,
            execution_supported=True,official_grading_supported=True,real_run_verified=False,
            prediction_export_supported=True,grading_report_reader_supported=True,artifact_kind='code_samples',
            official_grading='frozen author trusted_check, untrusted_check and estimate_pass_at_k in an independent container',
            runtime_apis=['bigcodebench.gen.util.trusted_check','bigcodebench.eval.untrusted_check'],
            execution_limits=['prepared Linux CPU images and complete evaluation libraries',
                'independent Codex sessions per sample; provider decoding is not raw model-generation replication'],
            task_scope='explicit versioned local dataset; split/subset declared in manifest; no downloads',
            required_resources=['local JSON/JSONL and dataset manifest','prepared Agent/verifier images',
                'fixed Codex','frozen bigcodebench/dependencies trees including real package metadata'])

    def task_instances(self,data):
        path=source_file(data);found,digest=rows(path);manifest=path.parent/'dataset_manifest.json'
        declared=eval_plan.file_sha256(manifest);meta=json.loads(manifest.read_text(encoding='utf-8'))
        if (not isinstance(meta,dict) or meta.get('split') not in ('complete','instruct') or meta.get('subset') not in ('full','hard') or
                meta.get('dataset')!=('bigcode/bigcodebench-hard' if meta['subset']=='hard' else 'bigcode/bigcodebench') or
                not isinstance(meta.get('revision'),str) or not meta['revision'].strip()):
            raise ValueError('declare BigCodeBench dataset, revision, split=complete/instruct and subset=full/hard')
        if eval_plan.file_sha256(manifest)!=declared:raise ValueError('BigCodeBench provenance changed')
        inputs=[dict(role='grading',path=str(path),sha256=digest),dict(role='runtime',path=str(manifest),sha256=declared)]
        tasks=[];seen=set()
        for row in found:
            state=initial_state(row,meta['split']);identity=row['task_id']
            if identity in seen:raise ValueError('duplicate BigCodeBench task ID')
            seen.add(identity)
            tasks.append(validate(dict(id=identity,benchmark=self.NAME,start_mode='task_start',initial_state=state,
                inputs=[dict(item) for item in inputs],evaluation=dict(kind='official-bigcodebench-code',
                    dataset=dict(name=meta['dataset'],revision=meta['revision'],split=meta['split'],subset=meta['subset'],
                                 evidence='local dataset manifest declaration')))))
        return tasks

    def agent_instruction(self,task):
        validate(task)
        if task['benchmark']!=self.NAME:raise ValueError('task belongs to another benchmark')
        return task['initial_state']['problem_statement']+'\n\nWrite the complete self-contained Python solution to /ctxpress-task/solution.py.'

    def run_fields(self):return {'code'}

    def normalize_run(self,settings,repeats):
        from .code_protocol import options
        return dict(settings,code=options(settings.get('code')))

    def plan_requirements(self,config,tasks,lock):
        from .code_protocol import requirements
        return requirements(config,tasks,lock)

    def execute(self,task,entry,config,job,*,paths,folder,label):
        from .swe_driver import execute
        return execute(self,task,entry,config,job,paths=paths,folder=folder,label=label)

    def recover(self,path,label):
        from .swe_driver import recover
        return recover(path,label)

    def write_samples(self,path,samples):
        samples=list(samples)
        for sample in samples:
            if (not isinstance(sample,dict) or set(sample)!={'task_id','solution'} or not isinstance(sample['task_id'],str) or
                    not re.fullmatch(r'BigCodeBench/\d+',sample['task_id']) or not isinstance(sample['solution'],str)):
                raise ValueError('invalid BigCodeBench code sample')
        from ctxpress.core.artifacts import atomic_write
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        atomic_write(path,''.join(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n' for row in samples))
        return str(path.resolve())

    def read_grade(self,task,report):
        self.agent_instruction(task)
        from .code_protocol import read_grade
        return read_grade(task,report)
