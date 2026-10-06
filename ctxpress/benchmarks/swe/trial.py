"""New Codex session and the frozen official SWE-bench run_instance grader."""
from __future__ import annotations
import asyncio, inspect, os
from pathlib import Path
from types import SimpleNamespace
from ctxpress.harness.runtime import codex_agent
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.benchmarks.swe.containers import AgentEnvironment, Owner, check_repo


class AgentExitError(RuntimeError):pass


def agent_class(request,credentials):
    class Launcher:
        _OUTPUT_FILENAME='codex.txt'
        def __init__(self):
            self.model_name=request['model'];self.logs_dir=Path(request['folder'])/'agent'
        def _build_register_skills_command(self):return None
        def _build_register_mcp_servers_command(self):return None
        async def run(self,instruction,environment,context):
            for command in self.create_run_agent_commands(instruction):
                result=await environment.exec(command='set -o pipefail; '+command.command,env=command.env)
                if result.return_code:raise AgentExitError('pinned Codex exited unsuccessfully')
    return codex_agent.framework(Launcher,SimpleNamespace,dict(method=request['method'], profiles=request.get('profiles'),model=request['model'],
        reasoning=request['reasoning'],binary_version=request['binary_version'],compact_limit=request['compact_limit'],
        upstream=request['upstream'],auth_file=os.environ['CTXPRESS_CODEX_AUTH_FILE'],max_calls=request['run']['max_calls'],
        **codex_agent.catalog_settings(request)),
        limit_error=AgentExitError,credential_state=credentials)


def grade(request,module,client,spec,owner,agent_owner,prediction):
    if request['api']=='pro-v1':
        from ctxpress.benchmarks.pro.v1_grading import grade as pro_grade
        return pro_grade(request,module,client,spec,owner,agent_owner,prediction)
    if request['api']=='polybench':
        from ctxpress.benchmarks.polybench.grading import grade as poly_grade
        return poly_grade(request,module,client,spec,owner,agent_owner,prediction)
    if not agent_owner.record['cleaned'] or agent_owner.record['credentials_may_exist']:
        raise RuntimeError('official SWE-bench grading requires checked Agent cleanup')
    root=Path(request['folder'])/'official-logs'
    run_id=request['project'];model=request['model'].replace('/','__')
    path=root/run_id/model/request['task']['id']/'report.json'
    if path.exists():raise ValueError('official grading report cache is not fresh')
    old_root=module.RUN_EVALUATION_LOG_DIR;module.RUN_EVALUATION_LOG_DIR=root
    def create(test_spec,client,run_id,logger,*unused,**options_unused):
        if test_spec is not spec or client is not owner.client or run_id!=request['project']:
            raise ValueError('official grader called another instance/client/run identity')
        if request['api']=='legacy':
            import importlib
            package=importlib.import_module(type(spec).__module__)
            config=package.MAP_REPO_VERSION_TO_SPECS[spec.repo][spec.version]
            options=dict(user='nonroot' if config.get('execute_test_as_nonroot') else 'root',
                         nano_cpus=config.get('nano_cpus'),platform=spec.platform)
        else:
            options=dict(user=module.CONTAINER_USER,cap_add=request['verifier_cap_add'])
        container=owner.create(**options)
        start=container.start
        def checked_start():
            start();check_repo(container,request['task']['initial_state']['base_commit'])
            owner.record['phase']='running';owner.persist()
        container.start=checked_start
        return container
    def cleanup(client,container,logger):
        if client is not owner.client or container is not None and container is not owner.container:
            raise ValueError('official grader attempted another environment cleanup')
        owner.cleanup(container)
    field='build_container' if request['api']=='legacy' else 'create_container'
    previous=getattr(module,field);old_cleanup=module.cleanup_container
    setattr(module,field,create);module.cleanup_container=cleanup
    kwargs=dict(test_spec=spec,pred=prediction,client=client,run_id=run_id,timeout=request['run'].get('grading_timeout',1800))
    parameters=inspect.signature(module.run_instance).parameters
    for key in ('rm_image','force_rebuild','rewrite_reports','skip_patch'):
        if key in parameters:kwargs[key]=False
    try:
        module.run_instance(**kwargs)
    finally:
        setattr(module,field,previous);module.cleanup_container=old_cleanup;module.RUN_EVALUATION_LOG_DIR=old_root
        if not owner.record['cleaned']:owner.cleanup()
    return path


async def run(request,module,client,spec,channel,daemon_id):
    request=dict(request,_not_found=client._ctxpress_not_found)
    if request['api']=='pro-v1':request['repository_directory']='/app'
    elif request['api']=='polybench':
        from ctxpress.benchmarks.polybench.grading import repository_directory
        request['repository_directory']=repository_directory(request,client)
    agent_owner=Owner(request,client,'agent',daemon_id,channel)
    grader_owner=Owner(request,client,'verifier',daemon_id)
    environment=AgentEnvironment(agent_owner,channel)
    agent=agent_class(request,agent_owner.credentials)()
    stop='completed';exception=None
    try:
        await environment.start();await agent.setup(environment)
        try:
            await asyncio.wait_for(agent.run(request['task']['initial_state']['problem_statement'],environment,{}),request['run']['timeout'])
        except asyncio.TimeoutError:
            stop='timeout';exception='AgentTimeoutError'
        except AgentExitError:
            stop=agent.ctxpress_stop or 'agent_error';exception='NonZeroAgentExitCodeError'
        # Capture partial work after timeout/budget/exit; do not inject gold.
        patch=await environment.patch()
        prediction=dict(instance_id=request['task']['id'],model_name_or_path=request['model'],model_patch=patch)
        from ctxpress.benchmarks.swe.adapter import SWEBench
        SWEBench().write_predictions(Path(request['folder'])/'predictions.jsonl',[prediction])
        (Path(request['folder'])/'model.patch').write_text(patch,encoding='utf-8')
    finally:
        async def cleanup_agent():
            if agent_owner.container is not None:
                await agent.cleanup_credentials(environment)
            await asyncio.to_thread(agent_owner.cleanup)
        cleanup=asyncio.create_task(cleanup_agent())
        try:await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
    report=None
    if request['run']['grade']:
        report=await asyncio.to_thread(grade,request,module,client,spec,grader_owner,agent_owner,prediction)
    else:grader_owner.cleanup()
    progress=codex_agent.CallProgress(Path(request['folder'])/'agent'/'sessions');calls,_=progress.update()
    root=Path(request['folder']);files=[root/'model.patch',root/'predictions.jsonl']
    files.extend(path for path in (root/'official-logs').rglob('*') if path.is_file() and not path.is_symlink())
    pro={}
    if request['api']=='pro-v1':
        pro=dict(pro_version='v1',submission=dict(path=str((root/'model.patch').resolve()),sha256=eval_plan.file_sha256(root/'model.patch')),
            fresh_regrade=json.loads((root/'official-logs/pro-v1-grade.json').read_text(encoding='utf-8')) if report else False,
            regrade_report=dict(path=str(report.resolve()),sha256=eval_plan.file_sha256(report)) if report else None)
    eval_plan.atomic_json(root/'swe-worker-result.json',dict(stop=stop,calls=calls,agent_exception=exception,
        official_report=str(report) if report else None,
        official_artifacts={path.relative_to(root).as_posix():dict(path=str(path.resolve()),sha256=eval_plan.file_sha256(path)) for path in files},
        separate_verifier=dict(agent_image=request['agent_image'],verifier_image=request['grading_image'],
            checked_cleanup=True,author_patch_application=request['run']['grade'],author_grading=request['run']['grade']),
        protocol='ctxpress_comparison',network_policy='no_tool_network_model_socket_only',
        grading_run_id=request['project'],swe_api=request['api'],**pro))
