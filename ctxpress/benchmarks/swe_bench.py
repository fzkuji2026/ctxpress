"""Local SWE-bench data, agent-visible instructions and official prediction artifacts."""
from __future__ import annotations
import json, os, re, tempfile
from pathlib import Path
from ctxpress.harness import eval_plan
from ctxpress.harness.task import validate
from .fresh import FreshAdapter

VARIANTS = {'swe-bench-verified':('SWE-bench Verified','SWE-bench_Verified'),
            'swe-bench-lite':('SWE-bench Lite','SWE-bench_Lite'),
            'swe-bench':('SWE-bench','SWE-bench')}


def source_file(data):
    path = Path(data).expanduser().resolve()
    if path.is_dir():
        files = [path/name for name in ('instances.jsonl','instances.json') if (path/name).is_file()]
        if len(files) != 1:
            raise ValueError('local SWE-bench data must contain exactly one instances.jsonl or instances.json')
        path = files[0]
    if path.suffix.lower() not in ('.json', '.jsonl') or not path.is_file():
        raise ValueError('provide a local SWE-bench JSON/JSONL dataset')
    return path


def rows(path):
    # Hash before reading to reject credentials and bind exactly the opened input.
    digest = eval_plan.file_sha256(path)
    text = path.read_text(encoding='utf-8')
    found = json.loads(text) if path.suffix.lower() == '.json' else [json.loads(line) for line in text.splitlines() if line.strip()]
    if not isinstance(found, list) or not found:
        raise ValueError('SWE-bench dataset must be a nonempty list of instances')
    if eval_plan.file_sha256(path) != digest:
        raise ValueError('SWE-bench dataset changed during discovery')
    return found, digest


class SWEBench(FreshAdapter):
    name = 'swe-bench'

    def describe(self):
        title, dataset = VARIANTS[self.name]
        return dict(name=self.name, title=title, adapter_version=2, suite=dataset, mode='task-start',
                    backends=['codex_docker'], from_task_start=True, catalog_supported=True,task_plan_supported=True,
                    execution_supported=True, official_grading_supported=True, real_run_verified=False,
                    prediction_export_supported=True, grading_report_reader_supported=True,
                    official_grading='frozen SWE-bench run_instance; independent prepared verifier',
                    runtime_apis=['legacy serialized TestSpec','prepared TestSpec dataset'],
                    execution_limits=['Linux CPU; prepared images only','no multimodal asset downloads',
                                      'ctxpress Codex comparison; release Agent protocol is declared separately'],
                    task_scope='explicit local JSON/JSONL task set; variant membership is declared by dataset provenance',
                    required_resources=['local dataset','prepared agent and grading images','Codex binary','official SWE-bench harness'])

    def plan_requirements(self, config, tasks, lock):
        from .swe_protocol import requirements
        return requirements(config, tasks, lock)

    def execute(self, task, entry, config, job, *, paths, folder, label):
        from .swe_driver import execute
        return execute(self, task, entry, config, job, paths=paths, folder=folder, label=label)

    def recover(self, path, label):
        from .swe_driver import recover
        return recover(path, label)

    def task_instances(self, data):
        path = source_file(data)
        found, digest = rows(path)
        manifest = path.parent/'dataset_manifest.json'
        provenance = dict(dataset=VARIANTS[self.name][1], revision=None, evidence='variant selected by caller; source release not declared')
        inputs = [dict(role='grading',path=str(path),sha256=digest)]
        if manifest.is_file():
            manifest_digest = eval_plan.file_sha256(manifest)
            meta = json.loads(manifest.read_text(encoding='utf-8'))
            expected = VARIANTS[self.name][1]
            if (not isinstance(meta, dict) or not isinstance(meta.get('dataset'), str) or
                    meta['dataset'] not in (expected, 'princeton-nlp/'+expected, 'SWE-bench/'+expected) or
                    not isinstance(meta.get('revision'), str) or not meta['revision'].strip()):
                raise ValueError('dataset provenance does not match the selected SWE-bench variant')
            if eval_plan.file_sha256(manifest) != manifest_digest:
                raise ValueError('dataset provenance changed during discovery')
            provenance = dict(dataset=meta['dataset'],revision=meta['revision'],evidence='local dataset manifest declaration')
            inputs.append(dict(role='runtime',path=str(manifest),sha256=manifest_digest))
        result, ids = [], set()
        for row in found:
            if not isinstance(row, dict):
                raise ValueError('invalid SWE-bench instance')
            identity = row.get('instance_id')
            if (not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+',identity) or identity in ids):
                raise ValueError('SWE-bench instances require unique official instance IDs')
            ids.add(identity)
            if not isinstance(row.get('repo'), str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',row['repo']):
                raise ValueError('instance repository must be explicit')
            if not isinstance(row.get('base_commit'), str) or not re.fullmatch(r'[0-9a-f]{40}',row['base_commit']):
                raise ValueError('instance base commit requires a full immutable Git SHA')
            if not isinstance(row.get('problem_statement'), str) or not row['problem_statement'].strip():
                raise ValueError('instance problem_statement must be explicit')
            # Whitelist fields; gold patch and test patch stay in grading-only data.
            state = dict(repo=row['repo'],base_commit=row['base_commit'],problem_statement=row['problem_statement'])
            result.append(validate(dict(id=identity,benchmark=self.name,start_mode='task_start',initial_state=state,
                inputs=[dict(item) for item in inputs],evaluation=dict(kind='official-swe-bench',dataset=provenance))))
        return result

    def agent_instruction(self, task):
        validate(task)
        if task['benchmark'] != self.name:
            raise ValueError('task belongs to another SWE-bench variant')
        return task['initial_state']['problem_statement']

    def prediction(self, task, model, patch):
        self.agent_instruction(task)
        if not isinstance(model, str) or not model.strip() or not isinstance(patch, str):
            raise ValueError('prediction requires a model identity and patch text')
        return dict(instance_id=task['id'], model_name_or_path=model, model_patch=patch)

    def write_predictions(self, path, predictions):
        """Validate and atomically export the official JSONL format, including empty patches."""
        predictions = list(predictions)
        ids = set()
        for item in predictions:
            if (not isinstance(item, dict) or set(item) != {'instance_id','model_name_or_path','model_patch'} or
                    not all(isinstance(item[key], str) for key in item) or not item['instance_id'] or
                    not item['model_name_or_path'].strip() or item['instance_id'] in ids):
                raise ValueError('invalid or duplicate official SWE-bench prediction')
            ids.add(item['instance_id'])
        path = Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.predictions-',dir=path.parent)
        try:
            with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
                for item in predictions:
                    stream.write(json.dumps(item,ensure_ascii=False,allow_nan=False)+'\n')
            os.replace(temporary,path)
        finally:
            if os.path.exists(temporary):os.remove(temporary)
        return str(path.resolve())

    def read_grade(self, task, report):
        """Read one official instance report; missing or ambiguous results remain ungraded."""
        self.agent_instruction(task)
        path = Path(report)
        if not path.is_file():
            return dict(resolved=None, infra_invalid=None, failure_kind='grading_error', error='official instance report missing')
        digest = eval_plan.file_sha256(path)
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError,UnicodeError):
            return dict(resolved=None, infra_invalid=None, failure_kind='grading_error', error='invalid official instance report',report=str(path),report_sha256=digest)
        row = raw.get(task['id']) if isinstance(raw,dict) else None
        if not isinstance(row,dict) or type(row.get('resolved')) is not bool:
            return dict(resolved=None, infra_invalid=None, failure_kind='grading_error', error='official report does not bind a boolean outcome to the selected instance',
                        report=str(path),report_sha256=digest)
        if eval_plan.file_sha256(path) != digest:
            raise ValueError('official instance report changed while reading')
        return dict(resolved=row['resolved'],infra_invalid=row.get('infra_failure') is True,report=str(path.resolve()),report_sha256=digest,
                    instance_id=task['id'],raw_instance_report=row)


class SWEBenchVerified(SWEBench):
    name = 'swe-bench-verified'


class SWEBenchLite(SWEBench):
    name = 'swe-bench-lite'
