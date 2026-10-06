"""Assemble the frozen native itinerary lifecycle; never prepare external inputs."""
from __future__ import annotations
import contextlib, datetime, math, os, re, signal
from pathlib import Path
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.benchmarks.milestone import agent as milestone_agent, budget as milestone_budget, containers as milestone_containers
from ctxpress.benchmarks.milestone import grade as milestone_grade, native as milestone_native, resources as milestone_resources, transport as milestone_transport
from ctxpress.benchmarks.milestone import trial as milestone_trial, version as milestone_version
from ctxpress.harness.jobs import resources as task_resources
from ctxpress.benchmarks.milestone import codex_hook as milestone_codex


@contextlib.contextmanager
def activated_policy(author,binding):
    keys=set(author.RUNTIME_POLICY_ENV_KEYS)|{'SWE_MILESTONE_UNPROTECTED'}
    previous={key:os.environ.get(key) for key in keys}
    try:
        author._activate_runtime_policy(binding)
        yield
    finally:
        for key,value in previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value


def validate(request):
    lock,_=task_resources.read(request['resources'],'swe-milestone',[request['original_task']])
    proof=lock.get('native_data_version')
    if proof is None:raise ValueError('capture native_data_version=true before continuous native execution')
    milestone_version.validate(proof)
    image=lock['tasks'][request['task']['id']]['images']['agent']
    if not isinstance(image.get('reference'),str) or not image['reference']:
        raise ValueError('native execution requires the captured Agent image reference for the author version gate')
    settings=request.get('execution')
    required={'project','label','method','model','reasoning','run','compact_limit','binary_version','bindir','profiles','upstream','via'}
    if not isinstance(settings,dict) or required-set(settings) or set(settings)-required-{'model_catalog','model_catalog_sha256'}:
        raise ValueError('invalid native execution settings')
    run=settings['run']
    if not isinstance(run,dict):raise ValueError('invalid native execution budget')
    milestone_resources.identity(settings['project'],settings['label'])
    for name in ('timeout','max_calls'):
        eval_plan._positive(run.get(name),name,integer=name!='timeout')
    eval_plan._positive(settings['compact_limit'],'compact_limit',integer=True)
    if 'grading_timeout' in run:eval_plan._positive(run['grading_timeout'],'grading_timeout',integer=False)
    if run.get('grade') is not True:raise ValueError('native itinerary execution requires its dependency grading')
    milestone_transport.destination(settings['upstream'])
    if settings['via']:
        from ctxpress.harness.runtime.connect_proxy import proxy_address
        proxy_address(settings['via'])
    for key in ('bindir','profiles'):
        path=Path(settings[key])
        if not path.is_absolute() or not path.is_dir() or ':' in str(path):raise ValueError('invalid native '+key+' directory')
    if not re.fullmatch(r'[A-Za-z0-9._+-]+',settings['binary_version']):raise ValueError('invalid native binary version')
    for key in ('model','reasoning'):
        if not isinstance(settings[key],str) or not settings[key]:raise ValueError('invalid native '+key)
    if not isinstance(settings['method'],dict):raise ValueError('invalid native method')
    milestone_codex.check_catalog(settings,probe=True)
    return lock


def run(request, source):
    from harness.e2e import run_e2e, data_version
    source=Path(source).resolve()
    if Path(data_version.__file__).resolve()!=source/'harness/e2e/data_version.py':
        raise ValueError('native data version gate imported undeclared code')
    lock=validate(request);proof=lock['native_data_version']
    settings=request['execution'];run=settings['run'];task=request['task'];folder=Path(request['folder']).resolve()
    catalog_metadata=milestone_codex.check_catalog(settings)
    auth=os.environ.get('CTXPRESS_CODEX_AUTH_FILE')
    if not auth or Path(auth).is_symlink() or not Path(auth).is_file():
        raise ValueError('native dispatch requires separately supplied existing authentication')
    prepared=milestone_native.prepare(task,source,folder/'native-preparation')
    workspace,trial,metadata=prepared['workspace'],prepared['trial'],prepared['metadata']
    (trial/'log').mkdir()
    milestone_version.materialize(proof,workspace.parent)
    with milestone_version.pinned_environment(lock['release']):
        verified=data_version.check_data_version(workspace,context='ctxpress frozen native dispatch')
        image=lock['tasks'][task['id']]['images']['agent']
        # A Docker config/content ID is not a registry manifest digest or a
        # version tag. Check the captured reference, then launch its bound ID.
        image_check=data_version.check_image_tag_consistency(image['reference'],context='ctxpress frozen native dispatch')
    actual=verified.get('data_version',{})
    if actual.get('checked') is not True or actual.get('state')!='match' or actual.get('commit')!=proof['head']:
        raise ValueError('native frozen Git objects failed the author data version gate')
    logs=folder/'agent-logs';store=folder/'agent-store'
    for path in (logs,logs/'sessions',store):path.mkdir(parents=True,exist_ok=True)
    budget=milestone_budget.Budget(logs/'sessions',run['timeout'],run['max_calls'],defer_start=True)
    images=lock['tasks'][task['id']]['images'];registry=channel=None;native=None;success=False;error=None
    previous={number:signal.getsignal(number) for number in (signal.SIGTERM,signal.SIGINT)}
    policy_scope=contextlib.ExitStack()
    try:
        policy_scope.enter_context(activated_policy(run_e2e,prepared['runtime_policy_binding']))
        images=lock['tasks'][task['id']]['images']
        registry=milestone_resources.Registry(settings['project'],settings['label'],folder,images.get('services'))
        channel=milestone_transport.Channel(registry,settings['upstream'],settings.get('via'))
        socket=channel.open()
        private=dict(runtime=request['package'],bindir=settings['bindir'],logs=str(logs),store=str(store),
            method=settings['method'],binary_version=settings['binary_version'],compact_limit=settings['compact_limit'],
            profiles=settings['profiles'],channel=str(socket),private_runtime=True,upstream=settings['upstream'])
        if 'model_catalog' in settings:private['model_catalog']=settings['model_catalog']
        with milestone_codex.installed(private):
            def initialize(owner,framework):
                if catalog_metadata is not None:
                    from ctxpress.harness.runtime.codex_catalog import CONTAINER_PATH
                    arguments=', '.join(repr(value) for value in (CONTAINER_PATH,settings['model'],settings['reasoning']))
                    script='from ctxpress.harness.runtime.codex_catalog import inspect, preflight\n'
                    script+='expected='+repr(settings['model_catalog_sha256'])+'\n'
                    script+='if inspect('+arguments+')["sha256"] != expected:\n    raise ValueError("native container catalog hash mismatch")\n'
                    script+='if preflight("/cxbin", '+arguments+')["sha256"] != expected:\n    raise ValueError("native container catalog changed during preflight")\n'
                    milestone_resources.docker('exec','--user','fakeroot','-e','PYTHONPATH=/ctxpress-runtime',
                        owner.record['container'],'python3','-c',script)
                return milestone_transport.initialize(owner,framework,auth)
            with milestone_containers.installed(source,registry,task['id'],images['agent'],images['grading'],initialize):
                native=run_e2e.E2EOrchestrator(repo_name=task['id'],milestone_version=lock['release'],image_name=image['id'],
                    dag_path=workspace/'dependencies.csv',srs_root=workspace/'srs',trial_root=trial,workspace_root=workspace,
                    agent_name='codex',model=settings['model'],config_path=prepared['config_path'],
                    repo_src_dirs=metadata['repo_src_dirs'],test_dirs=metadata['test_dirs'],exclude_patterns=metadata['exclude_patterns'],
                    generated_patterns=metadata.get('generated_patterns',[]),modifiable_test_patterns=metadata.get('modifiable_test_patterns',[]),
                    reasoning_effort=settings['reasoning'],agent_version=settings['binary_version'],
                    repo_config_binding=prepared['repo_config_binding'],runtime_policy_binding=prepared['runtime_policy_binding'])
                author_metadata=dict(trial_metadata_schema_version=run_e2e.TRIAL_METADATA_SCHEMA_VERSION_WITH_RUNTIME_POLICY_BINDING,
                    trial_name=trial.name,repo_name=task['id'],milestone_version=lock['release'],
                    benchmark_version=verified['benchmark_version'],data_version=actual,image_tag_check=image_check,
                    ctxpress_data_version_evidence=dict(head=proof['head'],tag=proof['tag'],
                        source_dirty=proof['source_dirty'],source_status_sha256=proof['source_status_sha256']),
                    image=image['id'],agent_image_id=None,agent_name='codex',requested_agent_version=settings['binary_version'],
                    agent_version=None,model=settings['model'],prompt_version='v2',timeout_seconds=run['timeout'],
                    reasoning_effort=settings['reasoning'],build_failure_fail_closed=native.build_failure_fail_closed,
                    auto_compact_window=None,enable_tool_search=None,unprotected=prepared['runtime_policy_binding'].mode=='unprotected',
                    start_time=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    dag_path=str(workspace/'dependencies.csv'),srs_root=str(workspace/'srs'),workspace_root=str(workspace),
                    repo_config_binding=prepared['repo_config_binding'].to_metadata(trial),
                    runtime_policy_binding=prepared['runtime_policy_binding'].to_metadata(trial),
                    **{name:metadata.get(name,[]) for name in ('repo_src_dirs','test_dirs','exclude_patterns','generated_patterns','modifiable_test_patterns')})
                if catalog_metadata is not None:author_metadata['ctxpress_model_catalog']=catalog_metadata
                eval_plan.atomic_json(trial/'trial_metadata.json',author_metadata)
                owner=native.container_setup.ctxpress_owner
                drain_seconds=run.get('grading_timeout',native.config.evaluation_timeout)
                with milestone_agent.installed(source,owner,auth,budget), milestone_trial.installed(source,owner,drain_seconds):
                    instance=run_e2e.E2ETrialRunner(orchestrator=native,agent_output_dir=trial/'log',workdir='/testbed',
                        repo_src_dirs=metadata['repo_src_dirs'],agent_name='codex',model=settings['model'],
                        timeout_ms=max(1,math.ceil(run['timeout']*1000)),prompt_version='v2',copy_testbed=True,
                        remove_container=False,reasoning_effort=settings['reasoning'],force=False)
                    success=instance.run()
    except BaseException as failure:
        error=type(failure).__name__
        raise
    finally:
        failures=[]
        try:budget.write(folder/'native-budget.json')
        except Exception as failure:failures.append(failure)
        if registry is not None:
            try:registry.cleanup()
            except Exception as failure:failures.append(failure)
        if channel is not None:
            try:channel.cleanup()
            except Exception as failure:failures.append(failure)
        try:policy_scope.close()
        except Exception as failure:failures.append(failure)
        for number,handler in previous.items():signal.signal(number,handler)
        state=dict(schema='ctxpress.eval.milestone_execution',version=1,task_id=task['id'],trial=str(trial),
            author_success=bool(success),stop=budget.reason or ('trial_error' if error else 'completed' if success else 'agent_incomplete'),
            error_type=error,cleanup_complete=not failures,calls=budget.count,real_run_verified=False)
        if catalog_metadata is not None:state['model_catalog']=catalog_metadata
        eval_plan.atomic_json(folder/'native-execution.json',state)
        if failures:raise RuntimeError('native cleanup incomplete; resource journals retained') from failures[0]
    try:grade=milestone_grade.read(task,source,trial)
    except BaseException as failure:
        state.update(stop='grading_error',error_type=type(failure).__name__)
        eval_plan.atomic_json(folder/'native-execution.json',state)
        raise
    eval_plan.atomic_json(folder/'native-grade.json',grade)
    return state
