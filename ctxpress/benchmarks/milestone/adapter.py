"""SWE-Milestone adapter for the existing local recorded-boundary suite.

Discovery reads metadata only. Execution and official grading still require the
declared local helpers, recorded histories, images and grading resources.
"""
from __future__ import annotations
import json
from pathlib import Path

POINTS = {(0, 129): ("milestone_001", "ours_sol_low_navidrome_003", 60), (0, 273): ("milestone_003_sub-01", "ours_sol_low_navidrome_003", 63),
          (0, 368): ("milestone_003_sub-02", "ours_sol_low_navidrome_003", 40), (0, 435): ("milestone_003_sub-03", "ours_sol_low_navidrome_003", 41),
          (1, 13): ("milestone_002", "official_sol_low_navidrome_001", 35), (3, 14): ("milestone_002", "ours_sol_low_navidrome_002", 29),
          (3, 136): ("milestone_003_sub-01", "ours_sol_low_navidrome_002", 45), (3, 214): ("milestone_003_sub-02", "ours_sol_low_navidrome_002", 40),
          (2, 31): ("milestone_002", "official_sol_low_navidrome_002", 35), (2, 197): ("milestone_003_sub-01", "official_sol_low_navidrome_002", 41)}


def boundaries(scripts):
    rows = json.loads((Path(scripts) / "valid_points.json").read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("boundary catalog must be a list")
    records = {}
    for row in rows:
        if (not isinstance(row, dict) or type(row.get('n')) is not int or row['n'] < 0
                or type(row.get('j')) is not int or row['j'] <= 0):
            raise ValueError("invalid boundary catalog coordinates")
        coordinate = row['n'], row['j']
        if coordinate in records:
            raise ValueError("duplicate catalog coordinates")
        records[coordinate] = row
    return records


class SWEMilestone:
    """Adapter contract: describe, task discovery/selection, input files, run and recover."""

    def describe(self):
        return dict(name="swe-milestone", adapter_version=1, title="SWE-Milestone",
                    suite="local-recorded-boundaries", mode="checkpoint-continuation",
                    backends=["codex_docker"], from_task_start=False, catalog_supported=True,
                    execution_supported=True, official_grading_supported=True, real_run_verified=False,
                    execution_real_run_verified=True, grading_real_run_verified=False,
                    official_grading="local official harness, only mapped boundaries",
                    task_scope="local Navidrome recordings; not the complete benchmark task set",
                    required_resources=["benchmark helpers", "recorded histories", "local Docker images",
                                        "Codex binary", "official grading inputs when scoring"])

    def default_scripts(self):
        from ctxpress.benchmarks.milestone.checkpoint_run import SCRIPTS
        return str(SCRIPTS)

    def compile_plan(self, config, base_dir=None):
        if config.get('start_mode') == 'task_start':
            from ctxpress.benchmarks.task_plan import compile_plan
        else:
            from ctxpress.benchmarks.checkpoint import compile_plan
        return compile_plan(config, base_dir)

    def task_instances(self, data):
        from ctxpress.benchmarks.milestone.data import tasks
        return tasks(data)

    def extra_input_trees(self, selected, root):
        from ctxpress.benchmarks.milestone.data import extra_input_trees
        return extra_input_trees(selected, root)

    def data_directories(self, selected, root):
        from ctxpress.benchmarks.milestone.data import data_directories
        return data_directories(selected, root)

    def plan_requirements(self, config, selected, lock):
        from ctxpress.benchmarks.milestone.protocol import execution_requirements
        return execution_requirements(config, selected, lock)

    def task_start_description(self):
        return dict(self.describe(), suite='repository-itineraries', mode='task-start',
                    from_task_start=True, execution_supported=True, official_grading_supported=True, task_plan_supported=True,
                    native_preparation_supported=True,preflight_supported=True,grading_report_reader_supported=True,
                    native_worker_implemented=True,
                    native_service_executor_implemented=True,
                    real_run_verified=False, execution_real_run_verified=False,
                    task_scope='prepared Linux CPU itineraries with per-milestone verifier images and declared testcontainers service images; real task runs unverified')

    def preflight(self, task, config, job, folder):
        from ctxpress.benchmarks.milestone.driver import preflight
        return preflight(task, config, job, folder)

    def read_itinerary_grade(self, task, config, job, folder, trial):
        from ctxpress.benchmarks.milestone.driver import read_grade
        return read_grade(task, config, job, folder, trial)

    def verify_bindings(self, plan):
        if plan['config'].get('start_mode') == 'task_start':
            from ctxpress.harness.jobs import resources as task_resources
            task_resources.verify_plan(plan)
            return
        coordinates = [(point['n'], point['j']) for point in plan['config']['boundaries']]
        if plan.get('environment_snapshot'):
            from ctxpress.harness.jobs import environment as eval_environment
            eval_environment.verify(plan['environment_snapshot'], coordinates)
        if plan.get('grading_snapshot'):
            from ctxpress.harness.jobs import grading_inputs
            grading_inputs.verify(plan['grading_snapshot'], coordinates)

    def execute(self, task, entry, config, job, *, paths, folder, label):
        if task['start_mode'] == 'task_start':
            from ctxpress.benchmarks.milestone.driver import execute
            return execute(self,task,entry,config,job,paths=paths,folder=folder,label=label)
        point = task['initial_state']['recorded_boundary']
        metadata = self.boundaries(config['environment']['scripts'])[(point['n'], point['j'])]
        measured = metadata.get('prefix_tokens')
        if config['scope'] == 'formal' and (type(measured) not in (int, float) or measured < 128000):
            raise ValueError('boundary catalog does not establish at least 128k context')
        settings = dict(config['run'], compact_limit=job['compact_limit'])
        result = self.run(point, entry, **settings, **config['environment'], input_paths=paths,
            model=config['model'], reasoning=config.get('reasoning', 'low'), outdir=folder,
            log=folder / 'requests.jsonl', run_label=label)
        result['start_mode'] = task['start_mode']
        result['task_id'] = task['id']
        result['boundary_context_evidence'] = dict(declared=point['context_tokens'], catalog_prefix_tokens=measured)
        return result

    def required_files(self, environment):
        return [Path(environment['scripts']) / name for name in
                ('valid_points.json', 'miss_probe_docker.py', 'c03_run.py', 'multi_probe.py', 'make_sidecar.py', 'grade.sh')]

    def boundaries(self, scripts):
        return boundaries(scripts)

    def tasks(self, scripts):
        result = []
        for (n, j), row in sorted(self.boundaries(scripts).items()):
            milestone, trial, _ = POINTS.get((n, j), (None, None, None))
            result.append(dict(id=f"n{n}-j{j}", n=n, j=j, context_tokens=row.get('prefix_tokens'),
                               official_grading_mapped=milestone is not None, milestone=milestone, trial=trial))
        return result

    def select_tasks(self, scripts, ids):
        if (not isinstance(ids, list) or not ids or any(not isinstance(value, str) for value in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("tasks must be a nonempty list of unique task IDs")
        available = {task['id']: task for task in self.tasks(scripts)}
        selected = []
        for task_id in ids:
            if task_id not in available:
                raise ValueError(f"unknown benchmark task {task_id!r}; inspect ctxpress eval tasks")
            task = available[task_id]
            if type(task['context_tokens']) is not int or task['context_tokens'] <= 0:
                raise ValueError(f"task {task_id!r} has no valid catalog context token count")
            selected.append({key: task[key] for key in ('id', 'n', 'j', 'context_tokens')})
        return selected

    def run(self, point, entry, **kwargs):
        from ctxpress.benchmarks.milestone import checkpoint_run as codex_docker
        result = codex_docker.run(point['n'], point['j'], entry, **kwargs)
        result['benchmark'] = dict(self.describe(), task_id=point['id'])
        return result

    def recorded_points(self):
        return POINTS

    def recover_native(self, folder, label):
        """Recover a native itinerary attempt from its resource journals."""
        from ctxpress.benchmarks.milestone.driver import recover_attempt
        recover_attempt(folder, label)

    def validate_native_version(self, record):
        from ctxpress.benchmarks.milestone import version
        version.validate(record)

    def capture_native_version(self, selected, release):
        from ctxpress.benchmarks.milestone import version
        return version.capture(selected, release)

    def recover(self, path, label):
        if Path(path).name.startswith('resources-milestone-'):
            from ctxpress.benchmarks.milestone.driver import recover_attempt
            return recover_attempt(Path(path).parent,label)
        from ctxpress.benchmarks.milestone import checkpoint_run as codex_docker
        return codex_docker.recover(path, label)
