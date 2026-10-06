"""Shared local Harbor task format, input freezing and official result reading."""
from __future__ import annotations
import json, math
from pathlib import Path
from ctxpress.core import toml
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.harness.jobs.task import validate
from ctxpress.benchmarks.fresh import FreshAdapter
from ctxpress.benchmarks.fresh import dataset_file


class HarborTasks(FreshAdapter):
    NAME = 'terminal-bench'
    TITLE = 'Terminal-Bench'
    REQUIRE_MANIFEST = False

    def describe(self):
        return dict(name=self.NAME,title=self.TITLE,adapter_version=4,
                    suite='explicit local Harbor tasks; NVIDIA GPUs require pinned device UUIDs',mode='task-start',backends=['codex_docker'],from_task_start=True,
                    catalog_supported=True,task_plan_supported=True,execution_supported=True,official_grading_supported=True,real_run_verified=False,
                    grading_report_reader_supported=True,
                    runtime_apis=['legacy_trial','single_step'],
                    verifier_modes=['shared','separate: modern SingleStepTrial only'],
                    execution_limits=['single-step Linux CPU/NVIDIA','single-service separate verifier',
                        'private service networks; no task allowlists or dynamic network switching'],
                    official_grading='official Harbor Trial/verifier; frozen runtime required; real execution not yet verified',
                    task_scope='Harbor task format; original Terminal-Bench 1.x format is not supported by this adapter',
                    required_resources=['versioned local Harbor tasks','prepared images','Codex binary','official Harbor runtime'])

    def plan_requirements(self, config, tasks, lock):
        from ctxpress.benchmarks.harbor.driver import requirements
        return requirements(config, tasks, lock)

    def execute(self, task, entry, config, job, *, paths, folder, label):
        from ctxpress.benchmarks.harbor.driver import execute
        return execute(self, task, entry, config, job, paths=paths, folder=folder, label=label)

    def recover(self, path, label):
        from ctxpress.benchmarks.harbor.driver import recover
        return recover(path, label)

    def task_instances(self, data):
        root=Path(data).expanduser().resolve()
        if not root.is_dir():raise FileNotFoundError(root)
        # Science releases retain domain/field folders. Discover their original
        # task roots without flattening or changing the publisher's task IDs.
        directories=[root] if (root/'task.toml').is_file() else [path.parent for path in sorted(root.rglob('task.toml')) if path.is_file()]
        if not directories:raise ValueError('no local Harbor-format tasks found')
        if len({path.name for path in directories})!=len(directories):
            raise ValueError('duplicate Harbor task IDs in dataset directories')
        manifest=root/'dataset_manifest.json'
        provenance=dict(name=self.NAME,revision=None,evidence='local tasks; dataset release not declared')
        declared=[]
        if self.REQUIRE_MANIFEST and not manifest.is_file():
            raise ValueError(self.TITLE + ' requires a dataset_manifest.json identifying the dataset and release')
        if manifest.is_file():
            path=dataset_file(root,manifest.name);digest=eval_plan.file_sha256(path)
            meta=json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(meta,dict) or meta.get('dataset')!=self.NAME or
                    not isinstance(meta.get('revision'),str) or not meta['revision'].strip()):
                raise ValueError('declare the ' + self.TITLE + ' dataset release explicitly')
            if eval_plan.file_sha256(path)!=digest:raise ValueError('dataset provenance changed during discovery')
            provenance=dict(name=self.NAME,revision=meta['revision'],evidence='local dataset manifest declaration')
            declared=[dict(role='runtime',path=str(path),sha256=digest)]
        found=[]
        for directory in directories:
            if any(path.is_symlink() for path in [directory, *directory.parents] if path==root or root in path.parents):
                raise ValueError('task directories cannot contain symbolic links')
            inputs=[dict(item) for item in declared]
            def add(name,role):
                path=dataset_file(directory,name)
                inputs.append(dict(role=role,path=str(path),sha256=eval_plan.file_sha256(path)))
                return path
            instruction=add('instruction.md','task').read_text(encoding='utf-8')
            if not instruction.strip():raise ValueError('Harbor task instructions must not be empty')
            config=toml.load(add('task.toml','runtime'))
            if not isinstance(config.get('environment',{}),dict):raise ValueError('invalid Harbor environment configuration')
            from ctxpress.harness.runtime.gpu import requirements
            requirements(config.get('environment', {}))
            from ctxpress.benchmarks.harbor.protocol import verifier_environment
            grading_environment = verifier_environment(config)
            if grading_environment is not None:
                requirements(grading_environment)
            directories=[]
            for folder,role in (('environment','runtime'),('tests','grading')):
                source=directory/folder
                if not source.is_dir() or source.is_symlink():raise ValueError('Harbor task requires a regular '+folder+' directory')
                directories.append(folder)
                for path in sorted(source.rglob('*')):
                    if path.is_symlink():raise ValueError('Harbor inputs cannot contain symbolic links')
                    if path.is_dir():directories.append(path.relative_to(directory).as_posix())
                    if path.is_file():add(path.relative_to(directory).as_posix(),role)
            if not (directory/'tests/test.sh').is_file():raise ValueError('Harbor task requires tests/test.sh')
            # solution/ is only for the oracle; never a model task input.
            state = dict(task_directory=str(directory),environment=config.get('environment',{}),input_directories=directories)
            if grading_environment is not None:
                state['verifier_environment'] = grading_environment
            if config.get('steps'):
                state['steps'] = config['steps']
            if config.get('task', {}).get('name'):
                name = config['task']['name']
                if not isinstance(name, str) or name.split('/')[-1] != directory.name:
                    raise ValueError('Harbor official task name differs from its directory ID')
                state['official_task_name'] = name
            found.append(validate(dict(id=directory.name,benchmark=self.NAME,start_mode='task_start',
                initial_state=state,inputs=inputs,
                evaluation=dict(kind='official-harbor-verifier',dataset=provenance,verifier=config.get('verifier',{})))))
        return found

    def data_directories(self, tasks, root):
        result=[]
        for task in tasks:
            prefix=Path(task['initial_state']['task_directory']).relative_to(root)
            result += [(prefix / name).as_posix() for name in task['initial_state']['input_directories']]
        return result

    def agent_instruction(self, task):
        validate(task)
        if task['benchmark']!=self.NAME:raise ValueError('task belongs to another benchmark')
        inputs=[item for item in task['inputs'] if item['role']=='task' and Path(item['path']).name=='instruction.md']
        if len(inputs)!=1:raise ValueError('task must bind one instruction.md')
        path=Path(inputs[0]['path'])
        if eval_plan.file_sha256(path)!=inputs[0]['sha256']:raise ValueError('task instruction changed')
        return path.read_text(encoding='utf-8')

    def read_grade(self, task, report):
        validate(task)
        if task['benchmark']!=self.NAME:raise ValueError('task belongs to another benchmark')
        path=Path(report)
        if not path.is_file():return dict(resolved=None,infra_invalid=None,failure_kind='grading_error',error='official Harbor trial result missing')
        digest=eval_plan.file_sha256(path)
        try:raw=json.loads(path.read_text(encoding='utf-8'))
        except (ValueError,UnicodeError):return dict(resolved=None,infra_invalid=None,failure_kind='grading_error',error='invalid official Harbor trial result')
        common=dict(resolved=None,report=str(path.resolve()),report_sha256=digest)
        if not isinstance(raw,dict) or raw.get('task_name')!=self.official_task_name(task):
            return dict(common,infra_invalid=None,failure_kind='grading_error',error='Harbor result belongs to another task')
        exception = raw.get('exception_info')
        if exception and (not isinstance(exception, dict) or exception.get('exception_type') not in
                          ('AgentTimeoutError', 'NonZeroAgentExitCodeError')):
            return dict(common,infra_invalid=None,failure_kind='grading_error',error='official Harbor trial recorded an exception',exception_info=raw['exception_info'])
        verifier=raw.get('verifier_result')
        rewards=verifier.get('rewards') if isinstance(verifier,dict) else None
        if (not isinstance(rewards,dict) or not rewards or any(not isinstance(key,str) or type(value) not in (int,float) or
                not math.isfinite(value) for key,value in rewards.items())):
            return dict(common,infra_invalid=None,failure_kind='grading_error',error='official verifier rewards missing or invalid')
        if eval_plan.file_sha256(path)!=digest:raise ValueError('Harbor result changed while reading')
        # Reward is a numeric official outcome, not implicitly a solved boolean.
        result = dict(common,infra_invalid=False,valid_rewards=True,rewards=rewards,trial_name=raw.get('trial_name'))
        if exception:
            result['agent_exception'] = exception
        return result

    def official_task_name(self, task):
        return task['initial_state'].get('official_task_name', task['id'])
