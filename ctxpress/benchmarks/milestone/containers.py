"""Process-local native container hooks; preserve author initialization and grading."""
from __future__ import annotations
import ast,contextlib,subprocess,types
from pathlib import Path
from ctxpress.benchmarks.milestone import images as milestone_images
from ctxpress.benchmarks.milestone.resources import docker


def prepared_initialization(source):
    """Remove the one author sudo installer, preserving user/cache/git setup."""
    tree=ast.parse(source);found=[]
    for index,node in enumerate(tree.body):
        if isinstance(node,ast.Try):
            strings={item.value for item in ast.walk(node) if isinstance(item,ast.Constant) and isinstance(item.value,str)}
            if {'which','sudo','apt-get','apk'}<=strings:found.append(index)
    if len(found)!=1:raise ValueError('native sudo initialization contract changed')
    tree.body[found[0]:found[0]+1]=ast.parse("if shutil.which('sudo') is None:\n    raise RuntimeError('prepared native image requires sudo')").body
    return ast.unparse(tree)


class Boundary:
    def __init__(self,owner,stage,service_gateway=None):self.owner,self.stage,self.service_gateway=owner,stage,service_gateway
    def __getattr__(self,name):
        if name not in ('DEVNULL','PIPE','STDOUT','TimeoutExpired'):
            raise ValueError('native container start requested an unsupported subprocess operation')
        return getattr(subprocess,name)

    def run(self,command,**options):
        owner=self.owner;name=owner.record['container']
        if command[:2] in (['docker','stop'],['docker','rm']):
            if command[-1]!=name:raise ValueError('native start tried another container cleanup')
            if owner.record['phase']=='prepared':
                if owner.existing():raise ValueError('native container start cannot reuse a stale/foreign name')
                return subprocess.CompletedProcess(command,1,b'',b'')
            if command[:2]!=['docker','rm']:raise ValueError('unexpected native stop after creation')
            owner.cleanup();return subprocess.CompletedProcess(command,0,b'',b'')
        if command[:2]==['docker','run']:
            mounts=[];env={};cpus=None;image=None;index=2
            while index<len(command):
                value=command[index]
                if value in ('-d','--init','--pull=never'):index+=1;continue
                if value=='--cap-add=NET_ADMIN':
                    if self.stage!='agent':raise ValueError('unexpected native verifier capability')
                    index+=1;continue
                if value=='--add-host=host.docker.internal:host-gateway':
                    if self.stage!='agent':raise ValueError('unexpected native verifier host gateway')
                    index+=1;continue
                if value in ('--name','--cpus','--ulimit','-v','-e','--network','--sysctl','--add-host','-w'):
                    entry=command[index+1];index+=2
                    if value=='--name' and entry!=name:raise ValueError('native launch changed container identity')
                    if value=='--cpus':cpus=float(entry)
                    if value=='--ulimit' and entry!='nofile=65535:65535':raise ValueError('native ulimit contract changed')
                    if value=='-w' and entry!='/testbed':raise ValueError('native Agent workdir changed')
                    if value in ('--sysctl','--add-host') and (self.stage!='agent' or entry not in (
                            'net.ipv6.conf.all.disable_ipv6=1','host.docker.internal:host-gateway')):
                        raise ValueError('native Agent network declaration changed')
                    if value=='--network' and (self.stage!='verifier' or
                            entry!=owner.registry.project+'-grade' and not (entry=='host' and self.service_gateway is not None)):
                        raise ValueError('native verifier requested an unowned network')
                    if value=='-v':
                        if entry=='/var/run/docker.sock:/var/run/docker.sock' and self.stage=='verifier' and self.service_gateway is not None:
                            mounts.append((self.service_gateway.record['channel'],'/ctxpress-services-api','ro'));continue
                        parts=entry.split(':')
                        if len(parts)==2:parts+=['rw']
                        if len(parts)!=3 or parts[1].startswith('/tmp/host-codex'):
                            raise ValueError('native launch cannot mount host authentication/configuration')
                        mounts.append(tuple(parts))
                    if value=='-e':
                        key,separator,data=entry.partition('=')
                        if not separator or key in env:raise ValueError('native environment declaration changed')
                        env[key]=data
                    continue
                image=value
                if command[index+1:]!=['tail','-f','/dev/null']:raise ValueError('native container command changed')
                break
            if image!=owner.record['image']:raise ValueError('native launch image differs from its resource owner')
            if self.service_gateway is not None:
                if self.stage!='verifier':raise ValueError('native service API is reserved for its verifier')
                env.update(DOCKER_HOST='unix:///ctxpress-services-api/docker.sock',TESTCONTAINERS_HOST_OVERRIDE='127.0.0.1',
                    TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE='/ctxpress-services-api/docker.sock',TESTCONTAINERS_RYUK_DISABLED='true')
            extra={'service_gateway':self.service_gateway} if self.service_gateway is not None else {}
            owner.create(mounts=mounts,environment=env,cpus=cpus,**extra)
            return subprocess.CompletedProcess(command,0,owner.record['container_id'], '')
        if (self.stage=='agent' and command[:3]==['docker','exec',name] and
                command[3:5]==['python3','-c'] and len(command)==6):
            value=subprocess.run(command,**options)
            if value.returncode:raise RuntimeError('native prepared initialization failed')
            # The private framework, checked before launch, creates owned home
            # and process storage. No credentials are mounted from the host.
            owner.record['private_initialized']=True;owner.persist();return value
        raise ValueError('native container start attempted an undeclared Docker operation')


def bound(function,boundary,**overrides):
    scope=dict(function.__globals__,subprocess=boundary,**overrides)
    clone=types.FunctionType(function.__code__,scope,function.__name__,function.__defaults__,function.__closure__)
    clone.__kwdefaults__=function.__kwdefaults__;return clone


@contextlib.contextmanager
def installed(code,registry,repo,agent,grading,initialize_agent=None):
    from harness.e2e import container_setup,evaluator,orchestrator,run_e2e
    code=Path(code).resolve()
    for module in (container_setup,evaluator,orchestrator,run_e2e):
        if Path(module.__file__).resolve()!=code/'harness/e2e'/(module.__name__.rsplit('.',1)[1]+'.py'):
            raise ValueError('native container hook imported an undeclared source')
    original_agent=container_setup.ContainerSetup;original_eval=evaluator.PatchEvaluator
    original_orchestrator=orchestrator.E2EOrchestrator

    class Agent(original_agent):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            if self.repo_name!=repo or self.image_name!=agent['id']:
                raise ValueError('native Agent constructor differs from the frozen repository/image')
            self.ctxpress_owner=registry.owner('agent',agent['id']);self.container_name=self.ctxpress_owner.record['container']
        def container_exists(self):
            if self.ctxpress_owner.existing():raise ValueError('native fresh Agent container is already occupied')
            return False
        def _ensure_python3(self):
            docker('exec',self.container_name,'sh','-c','set -e; command -v python3; command -v sudo; command -v git; command -v bash; command -v tar')
        def _get_base_init_script(self):return prepared_initialization(super()._get_base_init_script())
        def _wait_for_fakeroot(self,*args,**kwargs):
            if not super()._wait_for_fakeroot(*args,**kwargs):raise RuntimeError('native fakeroot user was not prepared')
            return True
        def start_container(self,extra_mounts=None,force=False):
            if force or extra_mounts:raise ValueError('native owned Agent cannot force-reset or add undeclared mounts')
            if getattr(self._framework,'ctxpress_private_runtime',False) is not True:
                raise ValueError('native owned Agent requires the private Codex/socket framework')
            if initialize_agent is None:raise ValueError('native owned Agent model initialization has not been connected')
            result=bound(original_agent.start_container,Boundary(self.ctxpress_owner,'agent'))(self,None,False)
            initialize_agent(self.ctxpress_owner,type(self._framework))
            return result
        def lock_network(self):
            # The comparison uses a stricter IP-free Agent namespace. Author
            # package/cache policy and prepare_agent_invocation remain active.
            self.verify_network_lockdown()
            docker('exec','--user','root',self.container_name,'sh','-c','rm -f /etc/sudoers.d/fakeroot')
        def verify_network_lockdown(self):
            if self.ctxpress_owner.inspect().get('HostConfig',{}).get('NetworkMode')!='none':
                raise ValueError('native Agent tool networking is not disabled')
            return True
        def cleanup(self,remove=True):self.ctxpress_owner.cleanup()

    class Orchestrator(original_orchestrator):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.container_name=self.container_setup.container_name

    class Evaluator(original_eval):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            if self.repo_name!=repo or self.milestone_id not in grading:
                raise ValueError('native evaluator constructor differs from the selected itinerary')
            self.ctxpress_owner=registry.owner('verifier',grading[self.milestone_id]['id'])
            self.container_name=self.ctxpress_owner.record['container']
            self.ctxpress_services=registry.service_gateway(self.ctxpress_owner) if self.needs_docker_socket else None
        def start_container(self):
            return bound(original_eval.start_container,Boundary(self.ctxpress_owner,'verifier',self.ctxpress_services),
                ensure_internal_evaluation_network=registry.grading_network)(self)
        def cleanup(self):
            if self.ctxpress_services is not None:self.ctxpress_services.stop()
            self.ctxpress_owner.cleanup()
            if self.ctxpress_services is not None:self.ctxpress_services.cleanup()

    changes=[(container_setup,'ContainerSetup',Agent),(orchestrator,'ContainerSetup',Agent),
        (orchestrator,'E2EOrchestrator',Orchestrator),(run_e2e,'E2EOrchestrator',Orchestrator),
        (evaluator,'PatchEvaluator',Evaluator),(orchestrator,'PatchEvaluator',Evaluator)]
    originals=[]
    with milestone_images.installed(evaluator,repo,agent,grading):
        try:
            for module,name,value in changes:
                originals.append((module,name,getattr(module,name)));setattr(module,name,value)
            yield types.SimpleNamespace(Agent=Agent,Evaluator=Evaluator,Orchestrator=Orchestrator)
        finally:
            for module,name,value in reversed(originals):setattr(module,name,value)
