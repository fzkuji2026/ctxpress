"""Owned native itinerary containers/networks; creation requires prepared inputs."""
from __future__ import annotations
import io,json,os,re,subprocess,tarfile,threading,uuid
from pathlib import Path,PurePosixPath
from ctxpress.benchmarks.harbor.driver import docker
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.core import processes

SCHEMA='ctxpress.eval.milestone_resources'
NETWORK_SCHEMA='ctxpress.eval.milestone_network'
PRIVATE='/home/fakeroot/.codex/auth.json'


def identity(project,label):
    if not re.fullmatch(r'ctxp-ms-[0-9a-f]{24}',project) or not isinstance(label,str) or not label:
        raise ValueError('native resources require an explicit run identity')


def image_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',value):
        raise ValueError('native resources require immutable image IDs')
    return value


def labels(project,label,role):
    return {'ctxpress.managed':'true','ctxpress.run':label,'ctxpress.milestone.project':project,'ctxpress.milestone.role':role}


class Registry:
    def __init__(self,project,label,folder,service_images=None):
        identity(project,label)
        if service_images:
            from ctxpress.harness.runtime.service_resources import selected_images
            selected_images(service_images)
        self.service_images=service_images or {};self.service_gateways=[]
        self.project,self.label,self.folder=project,label,Path(folder).resolve()
        self.folder.mkdir(parents=True,exist_ok=True)
        self.daemon=docker('info','--format','{{.ID}}').strip()
        if not self.daemon:raise ValueError('native resources require an identified Docker daemon')
        self.lock=threading.RLock();self.owners=[];self.network=None;self.native_trials=set()

    def verify_daemon(self):
        if docker('info','--format','{{.ID}}').strip()!=self.daemon:raise ValueError('native Docker daemon changed')

    def owner(self,role,image):
        with self.lock:
            if role=='agent' and any(owner.record['role']=='agent' for owner in self.owners):
                raise ValueError('one itinerary must retain one Agent container')
            owner=Owner(self,role,image);self.owners.append(owner);return owner

    def service_gateway(self,owner):
        from ctxpress.harness.runtime.service_gateway import Gateway
        with self.lock:
            if owner not in self.owners or owner.record['role']!='verifier' or owner.record.get('service_scope'):
                raise ValueError('native services require a fresh owned verifier')
            gateway=Gateway(self,self.service_images);self.service_gateways.append(gateway)
            gateway.bind_verifier(owner)
            owner.record['service_scope']=gateway.record['scope'];owner.persist()
            gateway.open();return gateway

    def grading_network(self):
        with self.lock:
            self.verify_daemon()
            if self.network is None:
                name=self.project+'-grade'
                self.network=dict(schema=NETWORK_SCHEMA,version=1,project=self.project,label=self.label,
                    daemon_id=self.daemon,name=name,cleaned=False,phase='creating',pid=os.getpid(),identity=processes.identity(os.getpid()))
                self.persist_network()
                # A foreign/stale name is an error; no reuse and no force-removal.
                if docker('network','ls','-q','--filter','name=^'+name+'$').split():
                    raise ValueError('native grading network name is already occupied')
                args=['network','create','--internal']
                for key,value in labels(self.project,self.label,'grading-network').items():args+=['--label',key+'='+value]
                identifier=docker(*args,name).strip()
                self.network.update(network_id=identifier,phase='created');self.persist_network()
            self.inspect_network()
            return self.network['name']

    def persist_network(self):eval_plan.atomic_json(self.folder/'resources-milestone-network.json',self.network)

    def inspect_network(self):
        self.verify_daemon();rows=json.loads(docker('network','inspect',self.network['name']))
        if len(rows)!=1:raise ValueError('ambiguous native grading network')
        row=rows[0]
        if (row.get('Name')!=self.network['name'] or row.get('Internal') is not True or
                self.network.get('network_id') and row.get('Id')!=self.network['network_id'] or
                any((row.get('Labels') or {}).get(k)!=v for k,v in labels(self.project,self.label,'grading-network').items())):
            raise ValueError('native grading network ownership changed')
        return row

    def protect_trial(self,container):
        with self.lock:
            if container in self.native_trials or not any(owner.record['role']=='agent' and
                    owner.record['container']==container for owner in self.owners):
                raise ValueError('native trial requires its unique registered Agent')
            self.native_trials.add(container)

    def release_trial(self,record):
        with self.lock:
            container=record.get('container')
            if (record.get('schema')!='ctxpress.eval.milestone_drain' or type(record.get('version')) is not int or record['version']!=1 or
                    record.get('phase')!='author-cleanup-returned' or
                    any(record.get(field) is not True for field in ('agent_quiesced','watcher_joined','author_cleanup_returned')) or
                    container not in self.native_trials):
                raise ValueError('native trial cleanup requires completed drain evidence')
            self.native_trials.remove(container)

    def cleanup(self):
        with self.lock:
            if self.native_trials:
                raise RuntimeError('native trial cleanup is incomplete; retain owned resources')
            return self._cleanup()

    def _cleanup(self):
        errors=[]
        for gateway in getattr(self,'service_gateways',()):
            try:gateway.stop()
            except Exception as error:errors.append(error)
        for owner in list(self.owners):
            try:owner.cleanup()
            except Exception as error:errors.append(error)
        if not errors:
            for gateway in getattr(self,'service_gateways',()):
                try:gateway.cleanup()
                except Exception as error:errors.append(error)
        with self.lock:
            if not errors and self.network is not None and not self.network['cleaned']:
                if self.inspect_network().get('Containers'):raise RuntimeError('native grading network still has attached containers')
                docker('network','rm',self.network['name'])
                if docker('network','ls','-q','--filter','name=^'+self.network['name']+'$').split():
                    raise RuntimeError('native grading network removal did not complete')
                self.network.update(cleaned=True,phase='stopped');self.persist_network()
        if errors:raise errors[0]


class Owner:
    def __init__(self,registry,role,image):
        if role not in ('agent','verifier'):raise ValueError('invalid native container role')
        self.registry=registry;name=registry.project+('-agent' if role=='agent' else '-eval-'+uuid.uuid4().hex[:16])
        self.path=registry.folder/('resources-milestone-'+name+'.json')
        self.record=dict(schema=SCHEMA,version=1,project=registry.project,label=registry.label,role=role,
            container=name,image=image_id(image),daemon_id=registry.daemon,pid=os.getpid(),identity=processes.identity(os.getpid()),
            credentials_may_exist=False,private_initialized=False,cleaned=False,phase='prepared')
        self.persist()

    def persist(self):eval_plan.atomic_json(self.path,self.record)

    def existing(self):
        self.registry.verify_daemon()
        values=docker('ps','-aq','--filter','name=^/'+self.record['container']+'$').split()
        if len(values)>1:raise ValueError('ambiguous native owned container')
        return values[0] if values else None

    def inspect(self):
        self.registry.verify_daemon();values=json.loads(docker('container','inspect',self.record['container']))
        if len(values)!=1:raise ValueError('ambiguous native container inspection')
        row=values[0];expected=labels(self.record['project'],self.record['label'],self.record['role'])
        if (row.get('Name','').lstrip('/')!=self.record['container'] or row.get('Image')!=self.record['image'] or
                self.record.get('container_id') and row.get('Id')!=self.record['container_id'] or
                any((row.get('Config',{}).get('Labels') or {}).get(key)!=value for key,value in expected.items())):
            raise ValueError('native container ownership or image changed')
        network=self.record.get('network_mode')
        if network is not None and row.get('HostConfig',{}).get('NetworkMode')!=network:
            raise ValueError('native container network mode changed')
        return row

    def create(self,*,mounts=(),environment=None,cpus=None,service_gateway=None):
        if self.record['phase']!='prepared' or self.record['cleaned']:raise ValueError('native owner can create only one fresh container')
        if self.existing():raise ValueError('native container name is already occupied')
        images=json.loads(docker('image','inspect',self.record['image']))
        if len(images)!=1 or images[0].get('Id')!=self.record['image'] or images[0].get('Os')!='linux':
            raise ValueError('native prepared Linux image is unavailable or changed')
        # Agent model transport is a Unix socket; verifier tests retain a private
        # eth0 on an owned internal network, with no external route.
        network='none' if self.record['role']=='agent' else self.registry.project+'-grade'
        if service_gateway is not None:
            if (self.record['role']!='verifier' or service_gateway not in self.registry.service_gateways or
                    service_gateway.scope.registry is not self.registry or service_gateway.record['verifier']!=self.record['container'] or
                    service_gateway.record['scope']!=self.record.get('service_scope') or service_gateway.record['phase']!='serving'):
                raise ValueError('native host-network verifier requires its serving owned service gateway')
            network='host'
        args=['create','--pull=never','--init','--name',self.record['container'],'--network',network,
            '--ulimit','nofile=65535:65535','--workdir','/testbed']
        for key,value in labels(self.record['project'],self.record['label'],self.record['role']).items():args+=['--label',key+'='+value]
        for source,target,mode in mounts:
            source=Path(source);target=PurePosixPath(target)
            from ctxpress.harness.runtime.codex_catalog import CONTAINER_PATH
            catalog_file = (self.record['role'] == 'agent' and str(target) == CONTAINER_PATH and mode == 'ro'
                            and source.is_file() and source.name.lower() != 'auth.json')
            if (not source.is_absolute() or not (source.is_dir() or catalog_file) or source.is_symlink() or ':' in str(source) or not target.is_absolute() or '..' in target.parts or
                    ':' in str(target) or mode not in ('ro','rw') or str(target) in ('/var/run/docker.sock','/run/docker.sock')):
                raise ValueError('native mounts require declared directories or the read-only Agent model catalog; Docker sockets are forbidden')
            args+=['--volume',str(source)+':'+str(target)+':'+mode]
        env=dict(environment or {})
        if any(not isinstance(key,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key) or not isinstance(value,str) or '\x00' in value or
                any(word in key.upper() for word in ('API_KEY','AUTH_TOKEN','ACCESS_TOKEN')) for key,value in env.items()):
            raise ValueError('native environment cannot contain provider credentials')
        env['NVIDIA_VISIBLE_DEVICES']='void'
        for key,value in sorted(env.items()):args+=['--env',key+'='+value]
        if cpus is not None:
            eval_plan._positive(cpus,'native grading CPUs',integer=False);args+=['--cpus',str(cpus)]
        if self.record['role']=='verifier' and service_gateway is None:self.registry.grading_network()
        self.record.update(network_mode=network,phase='creating');self.persist()
        identifier=docker(*args,self.record['image'],'tail','-f','/dev/null').strip()
        self.record.update(container_id=identifier,phase='created');self.persist();self.inspect()
        docker('start',self.record['container']);self.record['phase']='running';self.persist()
        return self.record['container']

    def credentials(self,source):
        if self.record['role']!='agent' or not self.record['private_initialized']:
            raise ValueError('native credentials require initialized private Agent storage')
        source=Path(source)
        if source.is_symlink() or not source.is_file():raise ValueError('native runtime authentication requires a regular existing file')
        self.inspect();self.record['credentials_may_exist']=True;self.persist()
        # Recovery refresh reuses this home after tools have run. Refuse a
        # replaced home/auth symlink before reading or copying host auth.
        docker('exec','--user','root',self.record['container'],'sh','-c',
            'test -d /home/fakeroot/.codex && test ! -L /home/fakeroot/.codex && test ! -L '+PRIVATE+
            ' && (test ! -e '+PRIVATE+' || test -f '+PRIVATE+')')
        content=source.read_bytes();stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w') as archive:
            entry=tarfile.TarInfo('auth.json');entry.mode=0o600;entry.size=len(content);archive.addfile(entry,io.BytesIO(content))
        subprocess.run(['docker','cp','-',self.record['container']+':/home/fakeroot/.codex'],input=stream.getvalue(),
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=30)
        docker('exec','--user','root',self.record['container'],'chown','fakeroot:0',PRIVATE)

    def cleanup(self):
        if self.record['role']=='agent' and self.record['container'] in self.registry.native_trials:
            raise RuntimeError('native trial cleanup is incomplete; retain owned resources')
        if self.record['cleaned']:return
        if self.existing():
            self.stop_agent()
            if self.record['private_initialized']:
                docker('exec','--user','root','-e','PYTHONPATH=/ctxpress-runtime',self.record['container'],
                    'python3','-m','ctxpress.harness.runtime.agent_process','--pid-file','/ctxpress-private/relay-process.json','--stop')
            if self.record['credentials_may_exist']:raise RuntimeError('native credentials have not been checked and removed')
            docker('rm','-f','-v',self.record['container'])
            if self.existing():raise RuntimeError('native container removal did not complete')
        self.record.update(cleaned=True,phase='stopped',credentials_may_exist=False);self.persist()

    def stop_agent(self):
        """Quiesce the Agent and remove auth while retaining its task/sessions."""
        row=self.inspect()
        if self.record['private_initialized']:
            if not row.get('State',{}).get('Running'):docker('start',self.record['container'])
            docker('exec','--user','root','-e','PYTHONPATH=/ctxpress-runtime',self.record['container'],
                'python3','-m','ctxpress.harness.runtime.agent_process','--pid-file','/ctxpress-private/agent.json','--stop')
            docker('exec','--user','root',self.record['container'],'sh','-c',
                'rm -f '+PRIVATE+'; test ! -e '+PRIVATE+' && test ! -L '+PRIVATE)
            self.record['credentials_may_exist']=False;self.persist()


def recover(path,label):
    """Recover a stopped worker's journal; never interrupt an identified live owner."""
    path=Path(path);record=json.loads(path.read_text(encoding='utf-8'))
    identity(record.get('project',''),record.get('label'))
    if record.get('version')!=1 or type(record.get('version')) is not int or record.get('label')!=label:
        raise ValueError('native recovery belongs to another run or schema')
    if record.get('schema') not in (SCHEMA,NETWORK_SCHEMA) or not isinstance(record.get('daemon_id'),str) or not record['daemon_id']:
        raise ValueError('invalid native recovery resource declaration')
    if type(record.get('cleaned')) is not bool or type(record.get('pid')) is not int or record['pid']<=0:
        raise ValueError('invalid native recovery lifecycle declaration')
    if not record['cleaned'] and processes.alive(record['pid'],record.get('identity')):
        raise ValueError('native worker is still alive; recovery cannot interrupt it')
    registry=object.__new__(Registry);registry.project=record['project'];registry.label=label
    registry.folder=path.resolve().parent;registry.daemon=record['daemon_id'];registry.owners=[]
    # The owning worker is confirmed dead above, so its watcher and graders
    # cannot still be using these resources. Live trial guards are not revived.
    registry.lock=threading.RLock();registry.network=None;registry.native_trials=set()
    if record['schema']==SCHEMA:
        role=record.get('role');name=record.get('container','');image_id(record.get('image'))
        expected=registry.project+('-agent' if role=='agent' else '-eval-')
        if (role not in ('agent','verifier') or role=='agent' and name!=expected or
                role=='verifier' and not re.fullmatch(re.escape(expected)+r'[0-9a-f]{16}',name) or
                path.name!='resources-milestone-'+name+'.json' or
                type(record.get('credentials_may_exist')) is not bool or type(record.get('private_initialized')) is not bool):
            raise ValueError('invalid native recovery container owner')
        if record.get('network_mode')=='host' and (role!='verifier' or not isinstance(record.get('service_scope'),str) or
                not re.fullmatch(r'[0-9a-f]{24}',record['service_scope'])):
            raise ValueError('native host-network verifier recovery requires its service scope')
        if record['cleaned']:return
        owner=object.__new__(Owner);owner.registry=registry;owner.path=path;owner.record=record;owner.cleanup()
    else:
        if record.get('name')!=registry.project+'-grade' or path.name!='resources-milestone-network.json':
            raise ValueError('invalid native recovery network owner')
        if record['cleaned']:return
        registry.verify_daemon();registry.network=record
        if not docker('network','ls','-q','--filter','name=^'+record['name']+'$').split():
            record.update(cleaned=True,phase='stopped');registry.persist_network()
        else:registry.cleanup()
