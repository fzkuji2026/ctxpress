"""Codex code-sample sessions followed by a separate prepared official verifier."""
from __future__ import annotations
import asyncio, hashlib, io, json, os, shlex, tarfile
from pathlib import Path
from ctxpress.harness.runtime import codex_agent
from ctxpress.benchmarks.bigcode.adapter import BigCodeBench
from ctxpress.benchmarks.bigcode.protocol import PROOF
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.core import artifacts as artifact_io
from ctxpress.benchmarks.swe.trial import agent_class, AgentExitError
from ctxpress.benchmarks.swe.containers import AgentEnvironment, Owner, completed_thread

TASK_DIRECTORY='/ctxpress-task'


class CodeEnvironment(AgentEnvironment):
    async def start(self):
        request=self.owner.request;logs=Path(request['folder'])/'agent';logs.mkdir()
        volumes={str(Path(request['package'])/'ctxpress'):{'bind':'/ctxpress-runtime/ctxpress','mode':'ro'},
            request['bindir']:{'bind':'/cxbin','mode':'ro'},request['profiles']:{'bind':'/ctxpress-method','mode':'ro'},
            str(self.channel):{'bind':'/ctxpress-channel','mode':'ro'},str(logs):{'bind':'/logs/agent','mode':'rw'}}
        for mount in codex_agent.catalog_mounts(request):
            volumes[mount['source']]={'bind':mount['target'],'mode':'ro'}
        container=await completed_thread(self.owner.create,user='root',volumes=volumes,init=True,entrypoint=[])
        await completed_thread(container.start)
        check=await completed_thread(container.exec_run,['/bin/sh','-c',
            'test ! -e /ctxpress-task && test ! -L /ctxpress-task && mkdir /ctxpress-task'],workdir='/',user='root')
        if check.exit_code:raise ValueError('BigCodeBench Agent task directory is not fresh')
        text=request['task']['initial_state']['seed_code'].encode();stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w') as archive:
            info=tarfile.TarInfo('solution.py');info.size=len(text);info.mode=0o644;archive.addfile(info,io.BytesIO(text))
        if not await completed_thread(container.put_archive,TASK_DIRECTORY,stream.getvalue()):raise RuntimeError('BigCodeBench prompt upload failed')
        self.owner.record.update(phase='running',task_id=request['task']['id']);self.owner.persist()

    async def sample(self):
        script='''import json, pathlib, stat
p=pathlib.Path('/ctxpress-task/solution.py')
if p.is_symlink(): raise ValueError('solution is a symbolic link')
if p.exists() and not stat.S_ISREG(p.stat().st_mode): raise ValueError('solution is not a regular file')
print(json.dumps(p.read_text(encoding='utf-8') if p.exists() else ''))
'''
        result=await self.exec('python3 -c '+shlex.quote(script),timeout_sec=60)
        if result.return_code:raise RuntimeError('BigCodeBench code sample capture failed')
        value=json.loads(result.stdout)
        if not isinstance(value,str):raise ValueError('invalid captured code sample')
        return value


def grade(request,client,problem,solution,owner,agent_owner):
    if not agent_owner.record['cleaned'] or agent_owner.record['credentials_may_exist']:
        raise RuntimeError('BigCodeBench grading requires checked Agent cleanup')
    root=Path(request['folder'])/'official-logs';root.mkdir();payload=root/'inputs';payload.mkdir();output=root/'outputs';output.mkdir()
    problem_path=payload/'problem.json';artifact_io.atomic_json(problem_path,problem)
    solution_path=payload/'solution.py';solution_path.write_text(solution,encoding='utf-8')
    report=output/'sample-result.json'
    resource=Path(request['folder'])/'samples.jsonl'
    args=dict(schema='ctxpress.eval.bigcode_grade_request',version=1,official='/ctxpress-official',package='/ctxpress-runtime',
        task_id=request['task']['id'],sample_id=request['sample_index'],n_samples=request['n_samples'],
        dataset=request['task']['evaluation']['dataset'],options=request['run']['code'],host_python=request['runtime']['version'],
        problem='/ctxpress-grade-input/problem.json',solution='/ctxpress-grade-input/solution.py',
        problem_sha256=eval_plan.file_sha256(problem_path),solution_sha256=eval_plan.file_sha256(solution_path),
        result='/ctxpress-grade-output/sample-result.json')
    artifact_io.atomic_json(payload/'request.json',args)
    command=['python3','-I','-S','-B','/ctxpress-runtime/ctxpress/benchmarks/bigcode/grading.py','/ctxpress-grade-input/request.json']
    timeout=request['run'].get('grading_timeout',1800)
    try:
        container=owner.create(user='root',entrypoint=[],working_dir='/',volumes={
            str(Path(request['package'])/'ctxpress'):{'bind':'/ctxpress-runtime/ctxpress','mode':'ro'},
            request['official']:{'bind':'/ctxpress-official','mode':'ro'},str(payload):{'bind':'/ctxpress-grade-input','mode':'ro'},
            str(output):{'bind':'/ctxpress-grade-output','mode':'rw'}})
        container.start();owner.record.update(phase='running',task_id=request['task']['id']);owner.persist()
        check=container.exec_run(['timeout','--signal=TERM','--kill-after=10s','60',*command,'--check'],user='root')
        (root/'preflight.log').write_bytes(check.output or b'')
        if check.exit_code:raise RuntimeError('frozen BigCodeBench grading imports failed before code execution')
        result=container.exec_run(['timeout','--signal=TERM','--kill-after=10s',str(timeout),*command],user='root')
        (root/'grading.log').write_bytes(result.output or b'')
        if result.exit_code:raise RuntimeError('BigCodeBench official grading failed or outer timeout expired')
        if not report.is_file() or report.is_symlink():raise RuntimeError('BigCodeBench official code sample report missing')
    finally:
        if not owner.record['cleaned']:owner.cleanup()
    # Report sits in outputs/ but its evidence lives next to it; no generic
    # grader may promote a bare pass/fail JSON to an authoritative score.
    def artifact(path):return dict(path=str(path.resolve()),sha256=eval_plan.file_sha256(path))
    record=dict(schema=PROOF,version=1,task_id=request['task']['id'],project=request['project'],report=artifact(report),samples=artifact(resource),
        agent_image=agent_owner.record['image'],verifier_image=owner.record['image'],
        agent_resources_sha256=eval_plan.file_sha256(agent_owner.path),verifier_resources_sha256=eval_plan.file_sha256(owner.path))
    artifact_io.atomic_json(output/'code-resources.json',record)
    return report


async def run(request,module,client,problem,channel,daemon_id):
    request=dict(request,_not_found=client._ctxpress_not_found,repository_directory=TASK_DIRECTORY)
    agent_owner=Owner(request,client,'agent',daemon_id,channel);owner=Owner(request,client,'verifier',daemon_id)
    environment=CodeEnvironment(agent_owner,channel);agent=agent_class(request,agent_owner.credentials)();adapter=BigCodeBench()
    stop='completed';exception=None
    try:
        await environment.start();await agent.setup(environment)
        try:await asyncio.wait_for(agent.run(adapter.agent_instruction(request['task']),environment,{}),request['run']['timeout'])
        except asyncio.TimeoutError:stop='timeout';exception='AgentTimeoutError'
        except AgentExitError:stop=agent.ctxpress_stop or 'agent_error';exception='NonZeroAgentExitCodeError'
        solution=await environment.sample()
        adapter.write_samples(Path(request['folder'])/'samples.jsonl',[dict(task_id=request['task']['id'],solution=solution)])
    finally:
        async def cleanup():
            if agent_owner.container is not None:await agent.cleanup_credentials(environment)
            await completed_thread(agent_owner.cleanup)
        cleanup_task=asyncio.create_task(cleanup())
        try:await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:await cleanup_task;raise
    report=None
    if request['run']['grade']:report=await completed_thread(grade,request,client,problem,solution,owner,agent_owner)
    else:owner.cleanup()
    root=Path(request['folder']);files=[root/'samples.jsonl']
    files.extend(path for path in (root/'official-logs').rglob('*') if path.is_file() and not path.is_symlink())
    progress=codex_agent.CallProgress(root/'agent/sessions');calls,_=progress.update()
    artifact_io.atomic_json(root/'swe-worker-result.json',dict(stop=stop,calls=calls,agent_exception=exception,
        official_report=str(report) if report else None,code_samples=str(root/'samples.jsonl'),sample_id=request['sample_index'],artifact_kind='code_samples',
        official_artifacts={path.relative_to(root).as_posix():dict(path=str(path.resolve()),sha256=eval_plan.file_sha256(path)) for path in files},
        separate_verifier=dict(agent_image=request['agent_image'],verifier_image=request['grading_image'],checked_cleanup=True,author_grading=bool(report)),
        protocol='ctxpress_comparison',network_policy='no_tool_network_model_socket_only',swe_api='codebench',grading_run_id=request['project']))
