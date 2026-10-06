"""SWE-bench Pro V2 locked capture followed by an independent official replay trial."""
from __future__ import annotations
import copy, hashlib, importlib, inspect, json, re, shutil
from pathlib import Path
from ctxpress.harness.jobs import plan as eval_plan


def load(request,official):
    import sys
    root=Path(request['official'])/'pro_tooling';sys.path.insert(0,str(root))
    locked=importlib.import_module('locked_codex');replay=importlib.import_module('patch_replay')
    if (Path(locked.__file__).resolve()!=root/'locked_codex.py' or Path(replay.__file__).resolve()!=root/'patch_replay.py' or
            not issubclass(locked.LockedCodex,official[0]) or not isinstance(locked._CAPTURE,str) or not locked._CAPTURE.strip() or
            not {'source_job','patch_name'}<=set(inspect.signature(replay.PatchReplayAgent.__init__).parameters) or
            any(not callable(getattr(replay.PatchReplayAgent,name,None)) for name in ('run','_find_patch','setup'))):
        raise ValueError('frozen Pro V2 locked/replay tooling is incompatible with the selected Harbor runtime')
    return dict(runtime=official,locked=locked.LockedCodex,capture=locked._CAPTURE,replay=replay.PatchReplayAgent)


async def check_repository(environment,commit):
    if not isinstance(commit,str) or not re.fullmatch(r'[0-9a-f]{40}',commit):raise ValueError('invalid Pro base commit')
    for directory in ('/app','/testbed'):
        result=await environment.exec(command='git -C '+directory+' rev-parse --show-toplevel',user='root',timeout_sec=30)
        if result.return_code:continue
        if (result.stdout or '').strip()!=directory:raise ValueError('prepared Pro repository directory changed')
        result=await environment.exec(command='git -C '+directory+' rev-parse HEAD',user='root',timeout_sec=30)
        if result.return_code or (result.stdout or '').strip()!=commit:raise ValueError('prepared Pro image differs from declared base commit')
        result=await environment.exec(command='git -C '+directory+' status --porcelain --untracked-files=all',user='root',timeout_sec=30)
        if result.return_code or (result.stdout or '').strip():raise ValueError('prepared Pro repository is not clean')
        return directory
    raise ValueError('prepared Pro image has no /app or /testbed repository')


def capture_factory(tooling,modern):
    def factory(base,exec_input,settings,limit_error,credentials):
        from ctxpress.harness.runtime import codex_agent
        from ctxpress.benchmarks.harbor import modern_codex as harbor_modern_codex
        if not issubclass(tooling['locked'],base):raise ValueError('Pro locked Agent parent changed')
        if modern:
            instrumented=harbor_modern_codex.framework(tooling['locked'],settings,limit_error,credentials)
        else:
            instrumented=codex_agent.framework(tooling['locked'],exec_input,settings,limit_error=limit_error,credential_state=credentials)
        class Capture(instrumented):
            async def run(self,instruction,environment,context):
                try:return await super().run(instruction,environment,context)
                finally:
                    if not self.ctxpress_credentials_cleaned:
                        raise RuntimeError('Pro patch capture requires checked Agent quiescence and credential deletion')
                    # Re-capture after the common guard stops every Agent child.
                    # Older LockedCodex.run may also capture before this guard.
                    result=await environment.exec(command='rm -f /logs/agent/model.patch; '+tooling['capture'],user='root',timeout_sec=300)
                    if result.return_code:raise RuntimeError('Pro author patch capture failed')
                    result=await environment.exec(command='test -f /logs/agent/model.patch && test ! -L /logs/agent/model.patch',user='root',timeout_sec=30)
                    if result.return_code:raise RuntimeError('Pro author patch capture artifact missing or unsafe')
        return Capture
    return factory


def replay_factory(tooling,submission,digest,identity):
    def factory(base,exec_input,settings,limit_error,credentials):
        class Replay(tooling['replay']):
            ctxpress_credentials_cleaned=True
            ctxpress_stop=None
            def __init__(self,*args,**kwargs):
                # _find_patch below binds exactly one copied prediction; never
                # search the host's other jobs or ambiguous retry directories.
                super().__init__(*args,source_job=str(submission.parent),patch_name='model.patch',**kwargs)
            def _find_patch(self,task_name):
                if task_name!=identity or eval_plan.file_sha256(submission)!=digest:
                    raise ValueError('Pro replay task or patch transfer changed')
                return submission
            async def cleanup_credentials(self,environment):pass
        return Replay
    return factory


async def run_trial(request,official,channel,journal):
    from ctxpress.benchmarks.harbor import modern as harbor_modern, worker as harbor_worker
    from ctxpress.benchmarks.pro.protocol import SCHEMA
    modern=request.get('harbor_api')=='modern'
    runner=harbor_modern.run_trial if modern else harbor_worker.run_trial
    folder=Path(request['folder']);agent=copy.deepcopy(request)
    agent['run']['grade']=False  # Never run the verifier in the model's sandbox.
    agent['separate_verifier']=False
    await runner(agent,official['runtime'],channel,journal,agent_factory=capture_factory(official,modern))
    state_path=folder/'harbor-worker-result.json';agent_state=json.loads(state_path.read_text(encoding='utf-8'))
    if agent_state['stop'] not in ('completed','timeout','agent_error','max_calls'):
        raise RuntimeError('Pro Agent trial failed before a valid captured submission')
    owner_path=folder/('resources-harbor-'+agent['project']+'.json')
    owner=json.loads(owner_path.read_text(encoding='utf-8'))
    if owner.get('cleaned') is not True or owner.get('credentials_may_exist') is not False or owner.get('pro_base_commit')!=request['pro_base_commit']:
        raise RuntimeError('Pro regrade requires checked Agent environment cleanup and base commit')
    source=folder/agent['project']/'agent'/'model.patch'
    digest=eval_plan.file_sha256(source);submission=folder/'pro-submission';submission.mkdir()
    copied=submission/'model.patch';shutil.copyfile(source,copied)
    if eval_plan.file_sha256(copied)!=digest:raise ValueError('Pro patch changed during transfer')
    def artifact(path):return dict(path=str(path.resolve()),sha256=eval_plan.file_sha256(path))
    agent_report=folder/agent['project']/'result.json'
    # Retain the author execution result even when no quality was computed.
    primary=artifact(agent_report)
    if not request['run']['grade']:
        agent_state.update(agent_report=primary,submission=artifact(copied),pro_version='v2',fresh_regrade=False)
        eval_plan.atomic_json(state_path,agent_state);return
    regrade=copy.deepcopy(request);regrade['project']='ctxp-hb-'+hashlib.sha256((request['project']+':pro-regrade').encode()).hexdigest()[:24]
    regrade.update(pro_replay=True,model='replay',images={'main':request['grading_image']},separate_verifier=False)
    regrade['run']['timeout']=300  # Author replay applies the patch with a 300s limit.
    await runner(regrade,official['runtime'],None,journal,
        agent_factory=replay_factory(official,copied,digest,request['task_id']))
    regrade_report=folder/regrade['project']/'result.json';secondary=artifact(regrade_report)
    regrade_owner=folder/('resources-harbor-'+regrade['project']+'.json')
    record=dict(schema=SCHEMA,version=1,task_id=request['task_id'],benchmark_version='v2',
        base_commit=request['pro_base_commit'],agent_project=agent['project'],regrade_project=regrade['project'],
        agent_image=request['images']['main'],regrade_image=request['grading_image'],
        agent_report=primary,regrade_report=secondary,submission=artifact(copied),
        agent_resources_sha256=eval_plan.file_sha256(owner_path),regrade_resources_sha256=eval_plan.file_sha256(regrade_owner),
        checked_agent_cleanup=True,regrade_model_calls=0,published_protocol_reproduced=False)
    eval_plan.atomic_json(folder/'pro-regrade.json',record)
    grade_state=json.loads(state_path.read_text(encoding='utf-8'))
    agent_state.update(official_report=str(regrade_report.resolve()),agent_report=primary,regrade_report=secondary,
        submission=record['submission'],pro_version='v2',fresh_regrade=record,
        separate_verifier=dict(agent_image=record['agent_image'],verifier_image=record['regrade_image'],
            checked_cleanup=True,author_patch_replay=True,verifier_started=True),
        official_artifacts={**agent_state.get('official_artifacts',{}),
            **{regrade['project']+'/'+key:value for key,value in grade_state.get('official_artifacts',{}).items()},
            'pro-submission/model.patch':record['submission'],'pro-regrade.json':artifact(folder/'pro-regrade.json')})
    eval_plan.atomic_json(state_path,agent_state)
