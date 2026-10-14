"""Process-local native container hooks; preserve author initialization and grading."""
from __future__ import annotations
import ast,contextlib,subprocess,types
from pathlib import Path
from ctxpress.benchmarks.milestone import images as milestone_images
from ctxpress.benchmarks.milestone.resources import docker


def check_prepared_tools(container):
    """Report missing offline prerequisites without installing during a run."""
    missing = docker('exec', container, 'sh', '-c',
        'for tool in python3 sudo git bash tar; do '
        'command -v "$tool" >/dev/null 2>&1 || printf "%s\\n" "$tool"; done').split()
    if missing:
        raise RuntimeError('prepared milestone Agent image lacks required tools: '
                           + ', '.join(missing) + '; prepare and freeze a new image before retrying')


# On overlayfs every chown copies the whole file into the container layer, even when the owner is
# unchanged: the author's `chown -R /testbed` alone took ~500 s per fresh Navidrome container.
CHOWN_CHANGED = '''
def _ctxpress_chown_changed(command, check=False, **_):
    """Same end state as `chown -R UID:GID PATH` (physical walk; a link is changed itself), but an
    entry is touched only when its owner or group differs, or it is a regular file with set-id bits,
    which chown clears. Entries already in that state stay in the image layer."""
    import os, stat, subprocess
    owner, path = command[2], command[3]
    uid, gid = (int(part) for part in owner.split(':'))
    errors = []
    def visit(entry):
        try:
            st = os.lstat(entry)
            if (st.st_uid != uid or st.st_gid != gid or
                    (stat.S_ISREG(st.st_mode) and st.st_mode & (stat.S_ISUID | stat.S_ISGID))):
                os.lchown(entry, uid, gid)
        except OSError as error:
            errors.append(str(error))
    visit(path)
    if os.path.isdir(path) and not os.path.islink(path):
        for root, dirs, files in os.walk(path, onerror=lambda error: errors.append(str(error))):
            for name in dirs + files:
                visit(os.path.join(root, name))
    result = subprocess.CompletedProcess(command, 1 if errors else 0, '', '\\n'.join(errors))
    if check and errors:
        raise subprocess.CalledProcessError(1, command, '', result.stderr)
    return result
'''


def _recursive_chown(node):
    return (isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='run' and
            isinstance(node.func.value,ast.Name) and node.func.value.id=='subprocess' and node.args and
            isinstance(node.args[0],ast.List) and len(node.args[0].elts)==4 and
            all(isinstance(e,ast.Constant) for e in node.args[0].elts[:2]) and
            [e.value for e in node.args[0].elts[:2]]==['chown','-R'])


def prepared_initialization(source):
    """Remove the one author sudo installer and make each recursive chown skip entries already in
    their final state; user/cache/git setup and the resulting ownership are unchanged."""
    tree=ast.parse(source);found=[]
    for index,node in enumerate(tree.body):
        if isinstance(node,ast.Try):
            strings={item.value for item in ast.walk(node) if isinstance(item,ast.Constant) and isinstance(item.value,str)}
            if {'which','sudo','apt-get','apk'}<=strings:found.append(index)
    if len(found)!=1:raise ValueError('native sudo initialization contract changed')
    tree.body[found[0]:found[0]+1]=ast.parse("if shutil.which('sudo') is None:\n    raise RuntimeError('prepared native image requires sudo')").body
    calls=[node for node in ast.walk(tree) if _recursive_chown(node)]
    lists=[node for node in ast.walk(tree) if isinstance(node,ast.List) and len(node.elts)>=2 and
           all(isinstance(e,ast.Constant) for e in node.elts[:2]) and [e.value for e in node.elts[:2]]==['chown','-R']]
    if not calls or len(lists)!=len(calls):raise ValueError('native recursive chown contract changed')
    for call in calls:
        call.func=ast.Name(id='_ctxpress_chown_changed',ctx=ast.Load())
    tree.body[0:0]=ast.parse(CHOWN_CHANGED).body
    return ast.unparse(tree)


class Boundary:
    def __init__(self,owner,stage,service_gateway=None,*,cargo_offline=False,cargo_toolchain_default=False):
        self.owner,self.stage,self.service_gateway=owner,stage,service_gateway
        self.cargo_offline=cargo_offline
        self.cargo_toolchain_default=cargo_toolchain_default
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
            if self.stage=='verifier' and self.cargo_offline:
                env['CARGO_NET_OFFLINE']='true'
                if self.cargo_toolchain_default:
                    # Vendor overlays copy the Agent's rustup installation and
                    # default. Do not let a parent image select an absent toolchain.
                    env['RUSTUP_TOOLCHAIN']=''
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
            check_prepared_tools(self.container_name)
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
            offline=isinstance(self.quarantine_config,dict) and self.quarantine_config.get('cargo_offline') is True
            policy=self.quarantine_config if isinstance(self.quarantine_config,dict) else {}
            vendor=('cargo' in policy.get('ecosystem',[]) and
                    (policy.get('closure') or {}).get('cargo_mechanism','vendor')=='vendor')
            return bound(original_eval.start_container,Boundary(self.ctxpress_owner,'verifier',self.ctxpress_services,cargo_offline=offline,cargo_toolchain_default=vendor),
                ensure_internal_evaluation_network=registry.grading_network)(self)
        def run_tests(self):
            from ctxpress.benchmarks.milestone.grading_diagnostics import bind
            return bind(original_eval.run_tests)(self)
        def _maybe_prune_residue(self,base_suffix):
            # source_policy binds every trial to residue_prune_scope=reference-added: the author's START provenance guard is
            # lifted (documented phase 2); a repo config without it keeps the author's own pass.
            if (self.repo_config or {}).get('residue_prune_scope')!='reference-added':
                return original_eval._maybe_prune_residue(self,base_suffix)
            from ctxpress.benchmarks.milestone.source_policy import lifted
            return lifted(original_eval._maybe_prune_residue)(self,base_suffix)
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
