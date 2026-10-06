"""Shared plan interface for adapters that consume original tasks."""
from __future__ import annotations


class FreshAdapter:
    def compile_plan(self, config, base_dir=None):
        from .task_plan import compile_plan
        return compile_plan(config, base_dir)

    def verify_bindings(self, plan):
        from ctxpress.harness.jobs import resources as task_resources
        if plan['config'].get('start_mode') != 'task_start':
            raise ValueError('this benchmark requires start_mode=task_start')
        task_resources.verify_plan(plan)

    def task_start_description(self):
        return self.describe()

    def tasks(self, scripts=None):
        raise ValueError('this benchmark uses original tasks; select --start-mode task_start --data')

    def default_scripts(self):
        self.tasks()

    def execute(self, task, entry, config, job, *, paths, folder, label):
        raise ValueError(self.describe()['name'] + ': task-start executor integration pending')

    def recover(self, path, label):
        # No runtime resources can be created before an executor is implemented.
        raise ValueError(self.describe()['name'] + ': task-start executor integration pending')


def dataset_file(root, relative):
    path = root / relative
    resolved = path.resolve()
    if root not in resolved.parents or any(parent.is_symlink() for parent in (path, *path.parents) if parent != root and root in parent.parents):
        raise ValueError(f'dataset input escapes the workspace or uses a symlink: {relative}')
    if not path.is_file():
        raise FileNotFoundError(path)
    return path
