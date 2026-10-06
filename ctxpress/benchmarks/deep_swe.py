"""DeepSWE v1.1: author collect hooks and Pier's separate verifier protocol."""
from __future__ import annotations
import re
from pathlib import Path
from ctxpress.core import toml
from ctxpress.harness import eval_plan
from .harbor_tasks import HarborTasks


def requirements(config, tasks, lock):
    if not lock:
        return ['DeepSWE: captured pier/dependencies trees, Linux Python and separate verifier image']
    missing = []
    runtime = lock.get('runtime', {})
    version = re.match(r'([0-9]+)\.([0-9]+)', runtime.get('version', ''))
    if runtime.get('platform') != 'linux' or not version or tuple(map(int, version.groups())) < (3, 12):
        missing.append('DeepSWE requires a pinned Linux Python 3.12 or later runtime')
    sources = lock['trees'].get('pier', {}).get('files', {})
    for name in ('pyproject.toml', 'src/pier/__init__.py', 'src/pier/trial/trial.py',
                 'src/pier/trial/artifact_handler.py', 'src/pier/models/task/verifier_mode.py',
                 'src/pier/agents/installed/codex.py', 'src/pier/environments/docker/docker.py'):
        if name not in sources:
            missing.append('DeepSWE Pier source tree: ' + name)
    dependencies = lock['trees'].get('dependencies', {}).get('files', {})
    if not any(re.fullmatch(r'datacurve_pier-[^/]+\.dist-info/METADATA', name) for name in dependencies):
        missing.append('DeepSWE dependencies: real datacurve-pier >0.3.0 metadata and transitive dependencies')
    for task in tasks:
        images = lock['tasks'][task['id']]['images']
        if set(images['grading']) != {'verifier'} or images['grading'].get('verifier', {}).get('id') == images['agent']['id']:
            missing.append('DeepSWE requires a distinct prepared verifier image containing tests: ' + task['id'])
        if images.get('services'):
            missing.append('DeepSWE separate verifier currently supports single-service tasks: ' + task['id'])
        if task['evaluation']['dataset']['revision'] != lock['release']:
            missing.append('DeepSWE resource release differs from dataset provenance: ' + task['id'])
        for phase in ('environment', 'verifier_environment'):
            if task['initial_state'][phase].get('gpus', 0):
                missing.append('DeepSWE GPU agent/verifier allocation is not implemented: ' + task['id'])
    return missing


class DeepSWE(HarborTasks):
    NAME = 'deep-swe'
    TITLE = 'DeepSWE'
    REQUIRE_MANIFEST = True

    def describe(self):
        result = super().describe()
        result.update(adapter_version=1, suite='explicit local DeepSWE v1.1 single-service CPU tasks',
            runtime_apis=['Pier >0.3.0 Trial.create'],verifier_modes=['separate'],
            execution_limits=['single-service Linux CPU','prepared distinct Agent/verifier images'],
            official_grading='Pier >0.3.0 official collect/artifact transfer and pristine separate verifier; real execution not yet verified',
            task_scope='DeepSWE separate-verifier tasks; Codex runs are ctxpress_comparison, not mini-swe-agent/Modal leaderboard reproduction',
            required_resources=['versioned local DeepSWE tasks', 'distinct prepared agent/verifier images',
                                'Codex binary', 'official Pier runtime and dependencies'])
        return result

    def task_instances(self, data):
        tasks = super().task_instances(data)
        for task in tasks:
            from .harbor_driver import task_directory
            root = task_directory(task)
            config = toml.load(root / 'task.toml')
            name = config.get('task', {}).get('name', task['id'])
            if not isinstance(name, str) or name.split('/')[-1] != task['id']:
                raise ValueError('DeepSWE official task name differs from its directory ID')
            verifier = config.get('verifier', {})
            if config.get('steps'):
                raise ValueError('DeepSWE multi-step execution is not implemented')
            if verifier.get('environment_mode') != 'separate' or not isinstance(verifier.get('environment'), dict):
                raise ValueError('DeepSWE v1.1 requires explicit separate verifier environment')
            hooks = verifier.get('collect')
            if not isinstance(hooks, list) or not hooks or any(not isinstance(hook, dict) or
                    not isinstance(hook.get('command'), str) or not hook['command'].strip() or
                    hook.get('service', 'main') != 'main' for hook in hooks):
                raise ValueError('DeepSWE requires author collect hooks targeting main')
            if config.get('artifacts') != ['/logs/artifacts/model.patch']:
                raise ValueError('DeepSWE adapter transfers only the declared model.patch submission')
            if not (root / 'tests' / 'Dockerfile').is_file():
                raise ValueError('DeepSWE requires tests/Dockerfile for its prepared verifier image')
            for phase in (config.get('environment', {}), verifier['environment']):
                if phase.get('os', 'linux') != 'linux':
                    raise ValueError('DeepSWE adapter requires Linux task and verifier images')
            if any((root / folder / 'docker-compose.yaml').exists() for folder in ('environment', 'tests')):
                raise ValueError('DeepSWE adapter currently requires single-service Dockerfile tasks')
            task['initial_state']['verifier_environment'] = verifier['environment']
            task['initial_state']['pier_task_name'] = name
            task['evaluation']['kind'] = 'official-pier-separate-verifier'
        return tasks

    def official_task_name(self, task):
        return task['initial_state']['pier_task_name']

    def read_grade(self, task, report):
        grade = super().read_grade(task, report)
        if not grade.get('valid_rewards'):
            return grade
        reward = grade['rewards'].get('reward')
        if reward == -1:
            grade.update(resolved=None, infra_invalid=True, valid_rewards=False,
                         failure_kind='grading_error', error='DeepSWE official verifier crash sentinel')
        elif reward not in (0, 1):
            grade.update(resolved=None, infra_invalid=None, valid_rewards=False,
                         failure_kind='grading_error', error='DeepSWE official binary reward missing or invalid')
        else:
            # The author explicitly defines this reward as binary task success.
            # Keep pass fractions/counts verbatim; never re-grade CTRF ourselves.
            grade['resolved'] = bool(reward)
        folder = Path(report).resolve().parent / 'verifier'
        grade['official_artifacts'] = {path.relative_to(folder).as_posix():
            dict(path=str(path.resolve()), sha256=eval_plan.file_sha256(path))
            for path in sorted(folder.rglob('*')) if path.is_file() and not path.is_symlink()}
        return grade
