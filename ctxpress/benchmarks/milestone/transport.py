"""Owned model-only Unix channel and persistent native-container loopback relay."""
from __future__ import annotations
import json,os,re,tempfile,threading,uuid
from pathlib import Path
from urllib.parse import urlsplit
from ctxpress.harness.runtime import connect_proxy, socket_bridge
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.core import processes
from ctxpress.benchmarks.milestone.resources import docker, identity, SCHEMA as CONTAINER_SCHEMA
from ctxpress.harness.runtime.socket_bridge import cleanup_channel

SCHEMA='ctxpress.eval.milestone_channel'


def destination(upstream):
    if not isinstance(upstream,str):raise ValueError('native model upstream must be explicit')
    value=urlsplit(upstream)
    if value.scheme!='https' or not value.hostname or value.username is not None or value.password is not None or value.fragment:
        raise ValueError('native model channel requires an HTTPS upstream without credentials')
    port=443 if value.port is None else value.port
    if not 0<port<65536:raise ValueError('native model upstream port must be valid')
    host='['+value.hostname+']' if ':' in value.hostname else value.hostname
    target=host+':'+str(port);connect_proxy.authority(target);return target


class Channel:
    def __init__(self,registry,upstream,via=None):
        destination(upstream)
        if via:connect_proxy.proxy_address(via)
        self.registry=registry;self.target=destination(upstream);self.via=via
        self.path=registry.folder/'resources-milestone-channel.json'
        if self.path.exists() or self.path.is_symlink():raise ValueError('native model channel journal already exists')
        self.server=self.thread=None
        self.record=dict(schema=SCHEMA,version=1,project=registry.project,label=registry.label,
            daemon_id=registry.daemon,pid=os.getpid(),identity=processes.identity(os.getpid()),
            channel=None,cleaned=False,phase='prepared')
        self.persist()

    def persist(self):eval_plan.atomic_json(self.path,self.record)

    def open(self):
        if self.record['phase']!='prepared':raise ValueError('native channel can open only once')
        self.registry.verify_daemon()
        # Persist the chosen unused path before mkdir/bind so a killed worker
        # never leaves an unjournaled socket directory. No destination/secret
        # appears in this recovery record.
        for attempt in range(10):
            channel=Path(tempfile.gettempdir()).resolve()/(self.registry.project+'-channel-'+uuid.uuid4().hex)
            if not channel.exists() and not channel.is_symlink():break
        else:raise ValueError('could not allocate an unused native channel')
        self.record.update(channel=str(channel),phase='creating');self.persist()
        channel.mkdir(mode=0o755);channel.chmod(0o755)
        eval_plan.atomic_json(channel/'owner.json',dict(project=self.registry.project,label=self.registry.label))
        template=connect_proxy.make_server('127.0.0.1',0,[self.target],via=self.via)
        handler=template.RequestHandlerClass;template.server_close()
        self.server=socket_bridge.unix_server(channel/'model.sock',handler);(channel/'model.sock').chmod(0o666)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.record['phase']='serving';self.persist();return channel

    def shutdown(self):
        if self.server:
            if self.thread and self.thread.is_alive():self.server.shutdown();self.thread.join(timeout=5)
            if self.thread and self.thread.is_alive():raise RuntimeError('native model server did not stop')
            self.server.server_close();self.server=self.thread=None

    def cleanup(self):
        self.shutdown()
        self.registry.verify_daemon()
        _verify_agent_removed(self.record,self.registry.folder)
        _remove_channel(self.record);self.record.update(cleaned=True,phase='stopped');self.persist()


def _verify_agent_removed(record,folder):
    # Verifiers do not mount this channel. A retained Agent must keep its bind
    # source so Docker can restart it for checked credential/process cleanup.
    name=record['project']+'-agent';path=Path(folder)/('resources-milestone-'+name+'.json')
    if path.exists():
        if path.is_symlink():raise ValueError('native Agent journal cannot be a symlink')
        owner=json.loads(path.read_text(encoding='utf-8'))
        if (owner.get('schema')!=CONTAINER_SCHEMA or owner.get('project')!=record['project'] or
                owner.get('label')!=record['label'] or owner.get('daemon_id')!=record['daemon_id'] or
                owner.get('container')!=name or owner.get('cleaned') is not True or owner.get('credentials_may_exist') is not False):
            raise ValueError('native channel must remain until its Agent is checked and removed')
    if docker('ps','-aq','--filter','name=^/'+name+'$').split():
        raise ValueError('native channel still has an Agent container')


def _remove_channel(record):
    if record['channel'] is None:return
    channel=Path(record['channel'])
    # An interrupted mkdir/owner write is recoverable only for the exact empty
    # owned path. Never delete an unrecognized file or an unbound socket.
    if (channel.is_symlink() or channel.parent!=Path(tempfile.gettempdir()).resolve() or
            not re.fullmatch(re.escape(record['project'])+r'-channel-[0-9a-f]{32}',channel.name)):
        raise ValueError('invalid native channel path')
    if channel.is_dir() and not (channel/'owner.json').exists():
        if (channel/'owner.json').is_symlink():raise ValueError('native channel owner cannot be a symlink')
        if any(channel.iterdir()):raise ValueError('native channel has contents without an owner')
        channel.rmdir();return
    if (channel/'owner.json').is_symlink():raise ValueError('native channel owner cannot be a symlink')
    cleanup_channel(record)


def recover(path,label):
    path=Path(path);record=json.loads(path.read_text(encoding='utf-8'))
    identity(record.get('project',''),record.get('label'))
    if (path.name!='resources-milestone-channel.json' or record.get('schema')!=SCHEMA or
            type(record.get('version')) is not int or record['version']!=1 or record.get('label')!=label or
            not isinstance(record.get('daemon_id'),str) or not record['daemon_id'] or
            type(record.get('pid')) is not int or record['pid']<=0 or type(record.get('cleaned')) is not bool or
            record.get('channel') is not None and not isinstance(record['channel'],str)):
        raise ValueError('invalid native model channel recovery journal')
    if not record['cleaned'] and processes.alive(record['pid'],record.get('identity')):
        raise ValueError('native worker is still alive; channel recovery cannot interrupt it')
    if docker('info','--format','{{.ID}}').strip()!=record['daemon_id']:raise ValueError('native channel Docker daemon changed')
    _verify_agent_removed(record,path.parent);_remove_channel(record)
    record.update(cleaned=True,phase='stopped');eval_plan.atomic_json(path,record)


RELAY_START='''import json,subprocess,time
from pathlib import Path
from ctxpress.core import processes
ready=Path('/ctxpress-private/relay.json')
if ready.exists() or ready.is_symlink():raise RuntimeError('native relay is already initialized')
process=subprocess.Popen(['python3','-m','ctxpress.harness.runtime.agent_process',
    '--pid-file','/ctxpress-private/relay-process.json','--','python3','-m','ctxpress.harness.runtime.socket_bridge',
    '--socket','/ctxpress-channel/model.sock','--ready-file',str(ready)],
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
for attempt in range(100):
    if process.poll() is not None:raise RuntimeError('native relay startup failed')
    if ready.is_file():
        record=json.loads(ready.read_text())
        if not processes.alive(record.get('pid'),record.get('identity')):raise RuntimeError('native relay is not alive')
        print(json.dumps(record));break
    time.sleep(0.1)
else:
    process.terminate();raise RuntimeError('native relay did not become ready')
'''


def initialize(owner,framework,auth_file):
    if owner.record['role']!='agent' or not owner.record['private_initialized'] or not framework.ctxpress_private_runtime:
        raise ValueError('native relay requires initialized owned private Agent storage')
    row=owner.inspect()
    if row.get('HostConfig',{}).get('NetworkMode')!='none':raise ValueError('native model relay requires an IP-free Agent')
    record=json.loads(docker('exec','--user','fakeroot','-e','PYTHONPATH=/ctxpress-runtime',owner.record['container'],
        'python3','-c',RELAY_START))
    if type(record.get('pid')) is not int or record['pid']<=1 or not isinstance(record.get('identity'),str) or not record['identity'].startswith('proc:'):
        raise ValueError('native model relay has no checked process identity')
    framework.set_model_relay(record.get('url',''))
    owner.credentials(auth_file)
    return record['url']
