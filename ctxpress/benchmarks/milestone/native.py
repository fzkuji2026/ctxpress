"""Prepare one frozen itinerary through author bindings, without starting a trial.

Container ownership and Agent/grading dispatch are deliberately separate from
this module. Import it in the isolated worker after verifying official trees.
"""
from __future__ import annotations
import shutil, sys
from pathlib import Path
from ctxpress.benchmarks.milestone.protocol import metadata_source
from ctxpress.harness.jobs import plan as eval_plan, trees as eval_trees


def layout(task):
    """Derive copied paths from bound inputs, never opaque original workspace paths."""
    source,_=metadata_source(task);root=source.parent
    identity=eval_trees.relative(task['id'])
    if len(identity.parts)!=1:raise ValueError('native itinerary identity must be one directory name')
    result={};targets=set()
    for item in task['inputs']:
        path=Path(item['path'])
        outside=root not in path.parents
        if (outside and item['role']=='grading' and path.name==task['id']+'.yaml' and
                item['sha256']==task['initial_state'].get('sibling_repo_config_sha256')):
            relative='config/'+task['id']+'.yaml'
        else:
            try:relative=task['id']+'/'+path.relative_to(root).as_posix()
            except ValueError:raise ValueError('itinerary input escapes its declared native layout') from None
        eval_trees.relative(relative)
        if relative in targets:raise ValueError('duplicate native itinerary input destination')
        boundary=path.parent if outside else root
        if any(parent.is_symlink() for parent in (path,*path.parents) if parent==boundary or boundary in parent.parents):
            raise ValueError('native itinerary input cannot use symbolic links')
        if eval_plan.file_sha256(path)!=item['sha256']:raise ValueError('native itinerary input changed')
        targets.add(relative);result[relative]=item
    return result


def materialize(task,directory):
    """Only selected, hash-bound files enter this private native input tree."""
    directory=Path(directory).resolve();inputs=layout(task)
    if directory.exists():raise ValueError('native itinerary preparation requires a fresh directory')
    directory.mkdir(parents=True)
    try:
        workspace=directory/task['id'];workspace.mkdir()
        for name in task['initial_state'].get('input_directories',[]):
            (workspace/eval_trees.relative(name)).mkdir(parents=True,exist_ok=True)
        for relative,item in inputs.items():
            path=directory/relative;path.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(item['path'],path)
            if eval_plan.file_sha256(path)!=item['sha256']:raise ValueError('native itinerary copy changed')
        return workspace
    except BaseException:
        shutil.rmtree(directory)
        raise


def prepare(task,code,directory):
    """Use actual native metadata/DAG/config binders; no containers or model calls."""
    from harness.e2e import run_e2e
    from harness.e2e.config import E2EConfig
    from harness.e2e.dag import DAGManager
    from harness.e2e.repo_config_binding import resolve_repo_config,freeze_repo_config
    from harness.e2e.runtime_policy_binding import resolve_runtime_policy,freeze_runtime_policy,runtime_policy_coverage_errors
    from harness.e2e.evaluator import validate_workspace_filter_lists
    code=Path(code).resolve();directory=Path(directory).resolve()
    for name in ('run_e2e','config','dag','repo_config_binding','runtime_policy_binding','evaluator'):
        module=sys.modules['harness.e2e.'+name]
        if Path(module.__file__).resolve()!=code/'harness/e2e'/(name+'.py'):
            raise ValueError('native preparation imported an undeclared official source: '+name)
    if directory.exists():raise ValueError('native preparation cannot reuse another trial')
    directory.mkdir(parents=True)
    workspace=materialize(task,directory/'native-inputs');trial=directory/'trial';trial.mkdir()
    repo=resolve_repo_config(task['id'],workspace,project_root=code)
    policy=resolve_runtime_policy(task['id'],code)
    errors=runtime_policy_coverage_errors(policy) if policy.mode=='protected' else []
    if errors:raise ValueError('native runtime policy does not meet author coverage: '+task['id'])
    config_binding=freeze_repo_config(trial,repo);policy_binding=freeze_runtime_policy(trial,policy)
    metadata=run_e2e.load_workspace_metadata(workspace,repo_config=dict(repo.config))
    if any(not isinstance(metadata.get(field),list) for field in ('repo_src_dirs','test_dirs','exclude_patterns')):
        raise ValueError('native metadata must declare source/test/exclusion lists')
    if validate_workspace_filter_lists(workspace):raise ValueError('native classification filter list is invalid')
    # Generate an explicit selection even when the original reader selected all
    # CSV rows. The author DAG otherwise loses isolated milestones with no edge.
    selected=trial/'selected_milestone_ids.txt'
    selected.write_text(''.join(value+'\n' for value in task['evaluation']['active_milestones']),encoding='utf-8')
    for name in ('milestones.csv','dependencies.csv','additional_dependencies.csv','non-graded_milestone_ids.txt'):
        path=workspace/name
        if path.is_file():shutil.copy2(path,trial/name)
    candidate=workspace/'e2e_config.yaml'
    if not candidate.is_file():candidate=code/'harness/e2e/e2e_config.yaml'
    if not candidate.is_file():raise ValueError('native e2e_config.yaml must be frozen explicitly')
    import yaml
    parsed=yaml.safe_load(candidate.read_bytes())
    if not isinstance(parsed,dict):raise ValueError('native trial configuration must be a YAML mapping')
    config_path=trial/'e2e_config.yaml';shutil.copy2(candidate,config_path)
    config=E2EConfig(config_path)
    dag=DAGManager(workspace/'dependencies.csv',selected_ids_file=selected,
        ignore_weak_dependencies=config.ignore_weak_dependencies,
        additional_dependencies_csv=trial/'additional_dependencies.csv' if (trial/'additional_dependencies.csv').is_file() else None)
    if dag.all_milestones!=set(task['evaluation']['active_milestones']):
        raise ValueError('native DAG differs from selected itinerary')
    script=repo.config.get('evaluation_post_snapshot_script')
    if script and not (workspace/eval_trees.relative(script)).is_file():
        raise ValueError('native post-snapshot script is not a frozen grading input')
    audit=dict(schema='ctxpress.eval.milestone_preparation',version=1,task_id=task['id'],
        repository=task['initial_state']['repo'],active_milestones=task['evaluation']['active_milestones'],
        graded_milestones=task['evaluation']['graded_milestones'],
        repo_config_sha256=config_binding.sha256,runtime_policy_sha256=policy_binding.sha256,runtime_policy_mode=policy_binding.mode,
        e2e_config_sha256=eval_plan.file_sha256(config_path),
        inputs={name:dict(path=str(directory/'native-inputs'/name),sha256=item['sha256'],role=item['role']) for name,item in layout(task).items()},
        native_dag=True,containers_started=0,model_calls=0,execution_supported=False,real_run_verified=False)
    eval_plan.atomic_json(directory/'preparation.json',audit)
    return dict(workspace=workspace,trial=trial,metadata=metadata,config_path=config_path,
        repo_config_binding=config_binding,runtime_policy_binding=policy_binding,dag=dag,audit=audit)
