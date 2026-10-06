"""SWE-PolyBench's multilingual task schema and instance-level verdicts."""
from __future__ import annotations
import json, re
from pathlib import Path
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.harness.jobs.task import validate
from ctxpress.benchmarks.swe.adapter import SWEBench, source_file, rows

DATASETS={'AmazonScience/SWE-PolyBench','AmazonScience/SWE-PolyBench_500','AmazonScience/SWE-PolyBench_Verified'}
LANGUAGES={'python','java','javascript','typescript'}
CATEGORIES={'Bug Fix','Feature','Refactoring'}


def initial_state(row):
    identity=row.get('instance_id')
    if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+',identity):
        raise ValueError('PolyBench requires official instance IDs')
    if not isinstance(row.get('repo'),str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',row['repo']):
        raise ValueError('PolyBench repository must be explicit')
    if not isinstance(row.get('base_commit'),str) or not re.fullmatch(r'[0-9a-f]{40}',row['base_commit']):
        raise ValueError('PolyBench base_commit requires a full Git SHA')
    if not isinstance(row.get('problem_statement'),str) or not row['problem_statement'].strip():
        raise ValueError('PolyBench problem_statement must be explicit')
    language=row.get('language')
    if not isinstance(language,str) or language.lower() not in LANGUAGES:
        raise ValueError('unsupported PolyBench language')
    if row.get('task_category') not in CATEGORIES:
        raise ValueError('unsupported PolyBench task_category')
    return dict(repo=row['repo'],base_commit=row['base_commit'],problem_statement=row['problem_statement'],
                language=language.lower(),task_category=row['task_category'])


class PolyBench(SWEBench):
    name='swe-polybench'

    def describe(self):
        return dict(name=self.name,title='SWE-PolyBench',adapter_version=1,suite='Full/500/Verified; explicit local dataset',
            mode='task-start',backends=['codex_docker'],from_task_start=True,catalog_supported=True,task_plan_supported=True,
            execution_supported=True,official_grading_supported=True,real_run_verified=False,
            prediction_export_supported=True,grading_report_reader_supported=True,
            runtime_apis=['poly_bench_evaluation.evaluate_instance'],languages=sorted(LANGUAGES),task_categories=sorted(CATEGORIES),
            official_grading='frozen author evaluate_instance, DockerManager patch/test operations, language parsers and scorer',
            execution_limits=['prepared Linux CPU images','repository WorkingDir must match in both images',
                'no retrieval metrics or runtime package repairs; no downloads/builds'],
            task_scope='declared Full/500/Verified membership and revision; explicit instance IDs',
            required_resources=['versioned local instances JSON/JSONL','prepared Agent/verifier images',
                                'Codex binary','frozen polybench/dependencies trees'])

    def task_instances(self,data,*,languages=None,categories=None):
        path=source_file(data);found,digest=rows(path)
        manifest=path.parent/'dataset_manifest.json'
        manifest_digest=eval_plan.file_sha256(manifest)
        meta=json.loads(manifest.read_text(encoding='utf-8'))
        if not isinstance(meta,dict) or meta.get('dataset') not in DATASETS or not isinstance(meta.get('revision'),str) or not meta['revision'].strip():
            raise ValueError('declare PolyBench dataset and immutable revision in dataset_manifest.json')
        if eval_plan.file_sha256(manifest)!=manifest_digest:raise ValueError('PolyBench provenance changed during discovery')
        selected_languages=set(LANGUAGES if languages is None else languages)
        selected_categories=set(CATEGORIES if categories is None else categories)
        if not selected_languages or selected_languages-LANGUAGES or not selected_categories or selected_categories-CATEGORIES:
            raise ValueError('invalid PolyBench language/category selection')
        inputs=[dict(role='grading',path=str(path),sha256=digest),dict(role='runtime',path=str(manifest),sha256=manifest_digest)]
        tasks=[];identities=set()
        for row in found:
            if not isinstance(row,dict):raise ValueError('invalid PolyBench instance')
            state=initial_state(row);identity=row['instance_id']
            if identity in identities:raise ValueError('duplicate PolyBench instance ID')
            identities.add(identity)
            if state['language'] not in selected_languages or state['task_category'] not in selected_categories:continue
            tasks.append(validate(dict(id=identity,benchmark=self.name,start_mode='task_start',initial_state=state,
                inputs=[dict(item) for item in inputs],evaluation=dict(kind='official-polybench',
                dataset=dict(name=meta['dataset'],revision=meta['revision'],evidence='local dataset manifest declaration')))))
        return tasks

    def plan_requirements(self,config,tasks,lock):
        from ctxpress.benchmarks.polybench.protocol import requirements
        return requirements(config,tasks,lock)

    def agent_instruction(self,task):
        validate(task)
        if task['benchmark']!=self.name:raise ValueError('task belongs to another benchmark')
        return task['initial_state']['problem_statement']

    def read_grade(self,task,report):
        self.agent_instruction(task);path=Path(report)
        failure=dict(resolved=None,infra_invalid=None,failure_kind='grading_error')
        if not path.is_file():return dict(failure,error='official PolyBench instance report missing')
        digest=eval_plan.file_sha256(path)
        try:row=json.loads(path.read_text(encoding='utf-8'))
        except (ValueError,UnicodeError):return dict(failure,error='invalid official PolyBench report')
        booleans=('patch_applied','generation','with_logs','all_f2p_passed','no_p2p_failed','resolved')
        if (not isinstance(row,dict) or row.get('instance_id')!=task['id'] or
                any(type(row.get(key)) is not bool for key in booleans) or
                any(not isinstance(row.get(key),list) or any(not isinstance(item,str) for item in row[key])
                    for key in ('passed_tests','failed_tests')) or
                row['resolved'] and not all(row[key] for key in booleans)):
            return dict(failure,error='official PolyBench report does not bind a consistent instance outcome')
        if eval_plan.file_sha256(path)!=digest:raise ValueError('PolyBench report changed while reading')
        return dict(resolved=row['resolved'],infra_invalid=False,instance_id=task['id'],raw_instance_report=row,
                    report=str(path.resolve()),report_sha256=digest,retrieval_metrics_supported=False)
