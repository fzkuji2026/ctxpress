"""Native itinerary requirements and author API checks; no optional imports."""
from __future__ import annotations
import ast, json, re
from pathlib import Path
from ctxpress.harness.jobs import plan as eval_plan

CONTRACTS = {
    'harness/e2e/orchestrator.py': {'E2EOrchestrator': {
        '__init__': {'repo_name','milestone_version','image_name','dag_path','srs_root','trial_root','workspace_root',
            'repo_src_dirs','test_dirs','config_path','repo_config_binding','runtime_policy_binding'},
        'setup_environment': {'force'}, '_handle_submission': {'mid','agent_tag','executor','pending_futures','attempt','expected_tag_hash'}}},
    'harness/e2e/run_e2e.py': {'E2ETrialRunner': {
        '__init__': {'orchestrator','agent_output_dir','timeout_ms','prompt_version','copy_testbed','remove_container','reasoning_effort'},
        'run': set(), 'run_agent_with_recovery': {'resume_session_first'}, 'cleanup': set()}},
    'harness/e2e/container_setup.py': {'ContainerSetup': {
        '__init__': {'container_name','image_name','agent_name','repo_name','runtime_policy_binding'},
        'start_container': {'extra_mounts','force'}, 'truncate_git_history': {'main_branch'},
        'verify_runtime_environment': set(), 'prepare_agent_invocation': set(), 'lock_network': set(),
        '_get_base_init_script': set(),'_ensure_python3': set(),'_wait_for_fakeroot': {'max_wait'},
        'verify_network_lockdown': set(),'cleanup': {'remove'}}},
    'harness/e2e/evaluator.py': {'PatchEvaluator': {
        '__init__': {'workspace_root','milestone_id','patch_file','baseline_classification','repo_config_path',
                     'repo_config_sha256','runtime_policy_path','runtime_policy_sha256','runtime_policy_mode'},
        'start_container': set(), 'evaluate': set(), 'cleanup': set()}},
    'harness/e2e/agents/codex.py': {'CodexFramework': {
        'get_container_mounts': set(), 'get_container_init_script': {'agent_name'},
        'build_run_command': {'model','session_id','prompt_path'},
        'build_resume_command': {'model','session_id','message_path'}}},
    'harness/e2e/agent_runner.py': {
        'AgentRunner': {'resume_session': {'session_id','message','timeout_ms'},
            '_refresh_codex_credentials': set(),'_handle_invocation_timeout': {'context'}},
        'E2EAgentRunner': {'__init__': {'container_name','agent_name','timeout_ms'},
            'run': {'prompt','session_id'},'send_recover_message': {'has_new_tasks','timeout_ms'}}},
}
FUNCTION_CONTRACTS = {
    'harness/e2e/run_e2e.py': {'_activate_runtime_policy': {'binding'}},
    'harness/e2e/data_version.py': {'check_data_version': {'data_path','context'},
        'check_image_tag_consistency': {'image','context'}},
    'harness/e2e/evaluator.py': {'ensure_offline_evaluation_image': {
        'repo_name','milestone_id','milestone_image','quarantine_config','expected_closure_image_id'}},
    'harness/e2e/collect_results.py': {
        'load_e2e_results': {'workspace_root','trial','prefer_filtered'},
        'authoritative_cells': {'eval_dir','prefer_filtered'},
        'compute_repo_summary': {'workspace_root','trials','trial_type','prefer_filtered'},
        'is_resolved': {'result'},'is_infra_invalid': {'result'}},
}


def contract_errors(root):
    """Inspect frozen source signatures without importing or executing the harness."""
    errors=[];root=Path(root)
    for name, classes in CONTRACTS.items():
        try:tree=ast.parse((root/name).read_text(encoding='utf-8'),filename=name)
        except (OSError,ValueError,SyntaxError) as error:
            errors.append('native SWE-Milestone source unavailable/invalid: '+name);continue
        for class_name, methods in classes.items():
            definitions=[node for node in tree.body if isinstance(node,ast.ClassDef) and node.name==class_name]
            if len(definitions)!=1:
                errors.append('native SWE-Milestone class: '+class_name);continue
            for method, required in methods.items():
                functions=[node for node in definitions[0].body if isinstance(node,ast.FunctionDef) and node.name==method]
                if len(functions)!=1:
                    errors.append('native SWE-Milestone method: '+class_name+'.'+method);continue
                args=functions[0].args
                fields={arg.arg for arg in args.posonlyargs+args.args+args.kwonlyargs}
                if not required <= fields:
                    errors.append('native SWE-Milestone signature: '+class_name+'.'+method)
    for name,functions in FUNCTION_CONTRACTS.items():
        try:tree=ast.parse((root/name).read_text(encoding='utf-8'),filename=name)
        except (OSError,ValueError,SyntaxError):
            errors.append('native SWE-Milestone collector source unavailable/invalid: '+name);continue
        for function,required in functions.items():
            nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name==function]
            if len(nodes)!=1:
                errors.append('native SWE-Milestone collector function: '+function);continue
            args=nodes[0].args;fields={arg.arg for arg in args.posonlyargs+args.args+args.kwonlyargs}
            if not required<=fields:errors.append('native SWE-Milestone collector signature: '+function)
    return errors


def metadata_source(task):
    found=[item for item in task['inputs'] if Path(item['path']).name=='metadata.json' and item['role']=='runtime']
    if len(found)!=1:raise ValueError('itinerary requires one bound metadata.json')
    item=found[0];path=Path(item['path'])
    if eval_plan.file_sha256(path)!=item['sha256']:raise ValueError('itinerary metadata changed')
    metadata=json.loads(path.read_text(encoding='utf-8'))
    if metadata.get('repo_name')!=task['initial_state']['repo'] or metadata.get('base_tag')!=task['initial_state']['base_ref']:
        raise ValueError('itinerary metadata differs from the task start state')
    return path,metadata


def service_milestones(task):
    """Mirror the author's list-format socket flag without importing the harness."""
    source,_=metadata_source(task);result=[]
    for mid in task['evaluation']['active_milestones']:
        path=source.parent/'dockerfiles'/mid/'test_config.json'
        bindings=[item for item in task['inputs'] if Path(item['path'])==path]
        if not bindings and not path.exists():continue # Author's missing-file case is false.
        if len(bindings)!=1:raise ValueError('native service configuration is not uniquely bound')
        if eval_plan.file_sha256(path)!=bindings[0]['sha256']:raise ValueError('native service configuration changed')
        config=json.loads(path.read_text(encoding='utf-8'))
        if isinstance(config,dict):continue # Author legacy format has no socket flag.
        if not isinstance(config,list) or any(not isinstance(mode,dict) or
                type(mode.get('requires_docker_socket',False)) is not bool for mode in config):
            raise ValueError('invalid native testcontainers configuration')
        if any(mode.get('requires_docker_socket',False) for mode in config):result.append(mid)
    return result


def execution_requirements(config, tasks, lock):
    missing = requirements(config, tasks, lock)
    if config['run'].get('grade') is not True:
        missing.append('SWE-Milestone native itinerary requires run.grade=true for DAG dependencies')
    if lock is not None:
        if lock.get('native_data_version') is None:
            missing.append('SWE-Milestone capture native_data_version=true before continuous native execution')
        else:
            from ctxpress.benchmarks.milestone import version as milestone_version
            milestone_version.validate(lock['native_data_version'])
        for task in tasks:
            if not lock['tasks'][task['id']]['images']['agent'].get('reference'):
                missing.append('SWE-Milestone requires captured Agent image reference for author version gate: ' + task['id'])
    return missing


def requirements(config,tasks,lock):
    if lock is None:return ['SWE-Milestone: native code/dependencies, versioned itinerary and per-milestone prepared images']
    missing=[];runtime=lock.get('runtime') or {};version=re.match(r'(\d+)\.(\d+)',runtime.get('version',''))
    if runtime.get('platform')!='linux' or not version or tuple(map(int,version.groups()))<(3,10):
        missing.append('SWE-Milestone requires a pinned Linux Python 3.10 or later')
    code=lock['trees'].get('code',{});files=code.get('files',{})
    required=['pyproject.toml','manifests/BENCHMARK_VERSION','harness/__init__.py','harness/e2e/dag.py',
        'harness/e2e/config.py','harness/e2e/agent_runner.py','harness/e2e/repo_config_binding.py',
        'harness/e2e/runtime_policy_binding.py','harness/e2e/quarantine.py','harness/e2e/e2e_config.yaml',
        'harness/e2e/prompt/v2.md','harness/e2e/collect_results.py','harness/e2e/trial_metrics.py',
        'harness/e2e/pricing.py',*CONTRACTS,*FUNCTION_CONTRACTS]
    for name in required:
        if name not in files:missing.append('SWE-Milestone native source: '+name)
    if (set(CONTRACTS)|set(FUNCTION_CONTRACTS))<=set(files):missing.extend(contract_errors(code['root']))
    deps=lock['trees'].get('dependencies',{}).get('files',{})
    for package in ('yaml','pathspec'):
        if package+'/__init__.py' not in deps:missing.append('SWE-Milestone frozen dependencies: '+package)
    for task in tasks:
        identity=task['id'];record=lock['tasks'][identity]
        expected=set(task['evaluation']['active_milestones'])
        if set(record['images']['grading'])!=expected:
            missing.append('SWE-Milestone native watcher requires one prepared grading image per active DAG milestone: '+identity)
        if record.get('gpu_device_ids') or record.get('verifier_gpu_device_ids'):
            missing.append('SWE-Milestone native itinerary resources require explicit CPU Agent/grading environments: '+identity)
        try:
            service_images=record['images'].get('services')
            if service_milestones(task) and not service_images:
                missing.append('SWE-Milestone testcontainers requires captured service_images: '+identity)
            if service_images:
                from ctxpress.harness.runtime.service_resources import selected_images
                selected_images(service_images)
        except (OSError,ValueError,KeyError):missing.append('SWE-Milestone service configuration/images are invalid: '+identity)
        try:
            source,meta=metadata_source(task)
            names={Path(item['path']).relative_to(source.parent).as_posix() for item in task['inputs']
                   if source.parent in Path(item['path']).parents}
            for mid in task['evaluation']['active_milestones']:
                for name in (f'dockerfiles/{mid}/test_config.json',f'test_results/{mid}/{mid}_classification.json'):
                    if name not in names:missing.append('SWE-Milestone native submission input: '+name)
            for field in ('repo_src_dirs','test_dirs','exclude_patterns'):
                if not isinstance(meta.get(field),list) or any(not isinstance(value,str) for value in meta[field]):
                    missing.append('SWE-Milestone metadata capture field '+field+': '+identity)
            manifests=[item for item in task['inputs'] if Path(item['path']).name=='dataset_manifest.json']
            if len(manifests)!=1:missing.append('SWE-Milestone explicit dataset_manifest.json release: '+identity)
            else:
                item=manifests[0];path=Path(item['path'])
                if eval_plan.file_sha256(path)!=item['sha256']:raise ValueError('itinerary provenance changed')
                meta=json.loads(path.read_text(encoding='utf-8'))
                if meta.get('dataset')!='swe-milestone' or meta.get('revision')!=lock['release']:
                    missing.append('SWE-Milestone resource release must match dataset provenance: '+identity)
        except (OSError,ValueError,KeyError):missing.append('SWE-Milestone bound metadata/provenance is invalid: '+identity)
    return missing
