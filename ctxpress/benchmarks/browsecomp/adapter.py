"""BrowseComp-Plus with the pinned ACM author agent (lixiaochuan2020/agentic-context-management).

The family runs the author's research loop unchanged, so a trained checkpoint (the ACM Qwen3.5-9B policy) sees
the prompts, tools and token hints it was trained with. ctxpress sits between the loop and the served model:
the planned method rewrites the agent's Chat Completions turns, and every call is logged and billed like the
other families. NoCompaction measures the agent's own context management (with use_memory_tool); other methods
can be compared on the same agent and model. Grading is the author's LLM judge (evaluate_browsecomp_plus).
"""
from __future__ import annotations
import hashlib, json
from pathlib import Path

from ctxpress.harness.jobs.task import validate


class BrowseCompPlus:
    NAME = 'browsecomp-plus'

    def describe(self):
        return dict(name=self.NAME, title='BrowseComp-Plus (ACM author agent)', adapter_version=1, suite='BrowseComp-Plus',
                    mode='task-start', backends=['acm_author'], from_task_start=True, catalog_supported=True,
                    task_plan_supported=True, execution_supported=True, official_grading_supported=True,
                    real_run_verified=False, artifact_kind='answer',
                    official_grading='author evaluate_browsecomp_plus (LLM judge) on the author result file',
                    execution_limits=['Linux; the author environment, BM25 index and a served agent model must already exist',
                                      'methods with their own agent tools are not available: the author loop offers only its tools'],
                    task_scope='explicit local question set in the author format; no downloads',
                    required_resources=['pinned author checkout and its Python environment', 'BrowseComp-Plus questions, '
                                        'decrypted ground truth and qrels', 'local BM25 index', 'served agent model',
                                        'summarizer and judge endpoints'])

    def task_start_description(self):
        return self.describe()

    def tasks(self, scripts=None):
        raise ValueError('BrowseComp-Plus uses original questions; select --start-mode task_start')

    def default_scripts(self):
        self.tasks()

    def compile_plan(self, config, base_dir=None):
        from ctxpress.benchmarks.browsecomp.plan import compile_plan
        return compile_plan(self, config, base_dir)

    def verify_bindings(self, plan):
        if plan['config'].get('start_mode') != 'task_start':
            raise ValueError('this benchmark requires start_mode=task_start')
        for job in plan['jobs']:
            if job['task']['benchmark'] != self.NAME:
                raise ValueError('job belongs to another benchmark')

    def task(self, row, inputs):
        """A question as a task: the text is identified by hash; the reference answer stays in the grading inputs."""
        digest = hashlib.sha256(row['question'].encode('utf-8')).hexdigest()
        return validate(dict(id=row['id'], benchmark=self.NAME, start_mode='task_start',
                             initial_state=dict(query_id=row['id'], question_sha256=digest),
                             inputs=[dict(item) for item in inputs],
                             evaluation=dict(kind='author-llm-judge', grader='evaluate_browsecomp_plus')))

    def question(self, task):
        """The question (with its answer, which the author loop records but never shows the model) from the task data."""
        from ctxpress.benchmarks.browsecomp.plan import questions
        data = next(item['path'] for item in task['inputs'] if item['role'] == 'task')
        row = questions(data)[task['initial_state']['query_id']]
        if hashlib.sha256(row['question'].encode('utf-8')).hexdigest() != task['initial_state']['question_sha256']:
            raise ValueError('question text changed since the plan was compiled')
        return row

    def execute(self, task, entry, config, job, *, paths, folder, label):
        from ctxpress.benchmarks.browsecomp.driver import execute
        return execute(self, task, entry, config, job, folder=folder, label=label)

    def recover(self, path, label):
        from ctxpress.benchmarks.browsecomp.driver import recover
        return recover(path, label)

    @staticmethod
    def result_dir(results, model, run_id):
        """Where the author run writes: results/<benchmark>/<model short name>/<run_dir>/<run_id>."""
        short = model.split('/')[-1]
        return Path(results) / 'browsecomp-plus' / short / 'ctxpress' / run_id

    def author_result(self, results, model, run_id, query_id):
        path = self.result_dir(results, model, run_id) / f'run_{query_id}.json'
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None

    def read_grade(self, task, eval_dir, grading_status='exited'):
        """The judge's verdict for this question; a missing or unparsed verdict is a grading error, not a wrong answer."""
        query = task['initial_state']['query_id']
        path = Path(eval_dir) / f'run_{query}_eval.json'
        base = dict(quality_kind='boolean', grading_status=grading_status)
        if not path.is_file():
            return dict(base, resolved=None, infra_invalid=False, failure_kind='judge_result_missing')
        record = json.loads(path.read_text(encoding='utf-8'))
        judge = record.get('judge_result') if isinstance(record, dict) else None
        if not isinstance(judge, dict) or judge.get('parse_error') or type(judge.get('correct')) is not bool:
            return dict(base, resolved=None, infra_invalid=False, failure_kind='judge_unparsed')
        return dict(base, resolved=judge['correct'], infra_invalid=False, confidence=judge.get('confidence'),
                    retrieval_recall=(record.get('retrieval') or {}).get('recall'),
                    citations=(record.get('citations') or {}).get('metrics'))
