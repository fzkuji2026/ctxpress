"""Owned Docker SDK environments for SWE-bench; prepared images only."""
from __future__ import annotations
import asyncio, io, os, tarfile
from pathlib import Path
from types import SimpleNamespace
from ctxpress.core import artifacts as artifact_io
from ctxpress.core import processes
from ctxpress.harness.runtime import codex_agent

SCHEMA='ctxpress.eval.swe_resources'


async def completed_thread(function,*args,**kwargs):
    """Finish resource mutations before cancellation can trigger cleanup."""
    operation=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
    try:return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await operation
        raise


def check_repo(container,commit,directory='/testbed'):
    for command,expected in (('git rev-parse HEAD',commit),('git status --porcelain --untracked-files=all','')):
        result=container.exec_run(['/bin/bash','-lc',command],workdir=directory,user='root')
        if result.exit_code or result.output.decode(errors='replace').strip()!=expected:
            raise ValueError('prepared SWE-bench repository must be clean at the declared base_commit')


class Owner:
    def __init__(self,request,client,role,daemon_id,channel=None):
        self.request,self.client,self.role=request,client,role
        name=request['project']+('-grade' if role=='verifier' else '-agent')
        image=request['grading_image'] if role=='verifier' else request['agent_image']
        self.path=Path(request['folder'])/('resources-swe-'+role+'.json')
        self.record=dict(schema=SCHEMA,version=1,project=request['project'],container=name,label=request['label'],
            image=image,role=role,daemon_id=daemon_id,channel=str(channel) if channel else None,
            credentials_may_exist=False,cleaned=False,phase='prepared',pid=os.getpid(),identity=processes.identity(os.getpid()))
        if role=='agent' and (catalog:=codex_agent.check_catalog(request)):
            self.record['model_catalog']=catalog
        self.container=None;self.persist()
    def persist(self):artifact_io.atomic_json(self.path,self.record)
    def credentials(self,value):
        self.record['credentials_may_exist']=value;self.persist()
    def inspect(self,container):
        container.reload();attrs=container.attrs;labels=attrs.get('Config',{}).get('Labels') or {}
        if (attrs.get('Image')!=self.record['image'] or labels.get('ctxpress.run')!=self.record['label'] or
                labels.get('ctxpress.managed')!='true' or labels.get('ctxpress.swe.role')!=self.role or
                attrs.get('Name','').lstrip('/')!=self.record['container']):
            raise ValueError('SWE-bench environment ownership or image changed')
        if self.record.get('container_id') and container.id!=self.record['container_id']:
            raise ValueError('SWE-bench container identity changed')
    def create(self,**kwargs):
        image=self.client.images.get(self.record['image'])
        if image.id!=self.record['image']:
            raise ValueError('prepared SWE-bench image changed')
        if image.attrs.get('Os')!='linux':raise ValueError('SWE-bench requires prepared Linux images')
        self.record['phase']='creating';self.persist()
        self.container=self.client.containers.create(image=self.record['image'],name=self.record['container'],
            detach=True,command='tail -f /dev/null',network_mode='none',
            labels={'ctxpress.managed':'true','ctxpress.run':self.record['label'],'ctxpress.swe.role':self.role},
            environment={'NVIDIA_VISIBLE_DEVICES':'void'},**kwargs)
        self.record['container_id']=self.container.id;self.record['phase']='created';self.persist()
        self.inspect(self.container);return self.container
    def cleanup(self,container=None):
        container=container if container is not None else self.container
        if container is None:
            # Creation may have reached Docker even if its response was lost.
            try:container=self.client.containers.get(self.record['container'])
            except self.request['_not_found']:pass
        if container is not None:
            self.inspect(container)
            if self.record['credentials_may_exist']:
                raise RuntimeError('SWE-bench private credentials must be removed before container deletion')
            container.remove(force=True,v=True)
            # Confirm removal through the same daemon; do not mark a failed
            # best-effort cleanup as complete.
            try:self.client.containers.get(self.record['container'])
            except self.request['_not_found']:pass
            else:raise RuntimeError('SWE-bench container still exists after cleanup')
        self.record.update(cleaned=True,phase='stopped');self.persist()


class AgentEnvironment:
    default_user='root'
    def __init__(self,owner,channel):
        self.owner,self.channel=owner,channel
    async def start(self):
        request=self.owner.request
        directory=request.get('repository_directory','/testbed')
        logs=Path(request['folder'])/'agent';logs.mkdir()
        volumes={str(Path(request['package'])/'ctxpress'):{'bind':'/ctxpress-runtime/ctxpress','mode':'ro'},request['bindir']:{'bind':'/cxbin','mode':'ro'},
            request['profiles']:{'bind':'/ctxpress-method','mode':'ro'},str(self.channel):{'bind':'/ctxpress-channel','mode':'ro'},
            str(logs):{'bind':'/logs/agent','mode':'rw'}}
        for mount in codex_agent.catalog_mounts(request):
            volumes[mount['source']]={'bind':mount['target'],'mode':'ro'}
        options=dict(entrypoint=[]) if request['api']=='pro-v1' else {}
        container=await completed_thread(self.owner.create,user='root',volumes=volumes,init=True,**options)
        await completed_thread(container.start)
        await completed_thread(check_repo,container,request['task']['initial_state']['base_commit'],directory)
        if request['api']=='pro-v1':self.owner.record['base_commit']=request['task']['initial_state']['base_commit']
        self.owner.record['phase']='running';self.owner.persist()
    async def exec(self,command,env=None,timeout_sec=None,user=None):
        result=asyncio.to_thread(self.owner.container.exec_run,['/bin/bash','-lc',command],workdir=self.owner.request.get('repository_directory','/testbed'),
            environment=env or {},user=user or 'root',demux=True)
        value=await asyncio.wait_for(result,timeout_sec) if timeout_sec else await result
        output,errors=value.output
        return SimpleNamespace(return_code=value.exit_code,stdout=(output or b'').decode(errors='replace'),
                               stderr=(errors or b'').decode(errors='replace'))
    async def upload_file(self,source,target):
        path=Path(source)
        if path.is_symlink() or not path.is_file() or not target.startswith('/ctxpress-private/'):
            raise ValueError('Agent upload requires a regular private runtime file')
        data=path.read_bytes();stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w') as archive:
            info=tarfile.TarInfo(Path(target).name);info.mode=0o600;info.size=len(data)
            archive.addfile(info,io.BytesIO(data))
        success=await completed_thread(self.owner.container.put_archive,str(Path(target).parent),stream.getvalue())
        if not success:raise RuntimeError('private Agent credential upload failed')
    async def patch(self):
        commit=self.owner.request['task']['initial_state']['base_commit']
        # Intent-to-add includes new files without creating a commit. Diffing
        # against the immutable start includes Agent commits and staged edits.
        result=await self.exec('git add -N -- . && git -c core.autocrlf=false diff --binary '+commit+' --',timeout_sec=60)
        if result.return_code:raise RuntimeError('could not capture SWE-bench prediction patch')
        return result.stdout
