"""Actual Registry/Channel/Gateway plus Docker CLI against one fake Unix Engine.

The Engine is a test double: no real daemon, image, container, or model is used.
The production resource providers and HTTP transport are composed together.
"""
import contextlib,io,json,os,shutil,socket,subprocess,sys,tarfile,tempfile,threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote,urlsplit
import pytest
from ctxpress.benchmarks.milestone import resources, transport
from ctxpress.harness.runtime import service_gateway as gateway, service_resources as services, socket_bridge
from ctxpress.benchmarks.milestone import driver as milestone_driver
from test_service_resources import Engine,IMAGE,PROJECT

pytestmark=pytest.mark.skipif(sys.platform!='linux' or shutil.which('docker') is None,reason='existing Linux Docker CLI and Unix sockets required')
SYNAPSE='ghcr.io/element-hq/synapse:develop@sha256:66955f34a593cfc3b6e77b8d5510c60c6094f5bade8a17d2feaefbb8662ccf09'


from milestone_mock_engine import Daemon


@contextlib.contextmanager
def pipeline(tmp_path,monkeypatch):
    engine=Daemon()
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def respond(self):
            size=int(self.headers.get('Content-Length','0'));body=self.rfile.read(size)
            response=engine.request(self.command,self.path,body)
            self.send_response(response.status)
            for key,value in response.headers.items():self.send_header(key,value)
            self.send_header('Content-Length',str(len(response.body)));self.send_header('Connection','close');self.end_headers()
            if self.command!='HEAD':self.wfile.write(response.body)
            self.close_connection=True
        do_GET=do_HEAD=do_POST=do_PUT=do_DELETE=respond
    # A user checkout or --basetemp may exceed AF_UNIX's pathname limit.
    # The Engine endpoint is independent of the durable fixture artifacts.
    socket_home=tempfile.TemporaryDirectory(prefix='ctxpress-fake-engine-')
    endpoint=Path(socket_home.name)/'fake.sock';server=socket_bridge.unix_server(endpoint,Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    config=tmp_path/'docker-config';config.mkdir();binary=shutil.which('docker');cli=[]
    def docker(*args):
        cli.append(args)
        result=subprocess.run([binary,'--host','unix://'+str(endpoint),'--config',str(config),*args],
            env={'PATH':os.defpath,'HOME':str(config),'DOCKER_API_VERSION':'1.47'},capture_output=True,text=True,timeout=10)
        if result.returncode:raise RuntimeError(result.stderr)
        return result.stdout
    for module in (resources,transport):monkeypatch.setattr(module,'docker',docker)
    monkeypatch.setenv('DOCKER_HOST','unix://'+str(endpoint))
    registry=resources.Registry(PROJECT,'fixture-run',tmp_path/'run',{'homeserver':{'id':IMAGE,'reference':SYNAPSE}})
    channel=transport.Channel(registry,'https://fixture.invalid/responses')
    try:yield registry,channel,engine,cli
    finally:
        # Close the fake Engine even when a production cleanup rejects state.
        try:
            engine.daemon=registry.daemon;registry.native_trials.clear();registry.cleanup();channel.cleanup()
        finally:
            server.shutdown();server.server_close();thread.join(3)
            socket_home.cleanup()
            assert not thread.is_alive()


def sdk(root,method,path,value=None):
    connection=gateway.Connection(root/'docker.sock',timeout=3)
    try:
        body=json.dumps(value).encode() if isinstance(value,dict) else value or b''
        connection.request(method,path,body,{'Content-Type':'application/json' if isinstance(value,dict) else 'application/x-tar'})
        response=connection.getresponse();content=response.read()
        assert response.status<400,(response.status,content)
        return json.loads(content) if content and response.getheader('Content-Type')=='application/json' else content
    finally:connection.close()


def prepared_verifier(registry,tmp_path):
    owner=registry.owner('verifier',IMAGE);service=registry.service_gateway(owner)
    owner.create(mounts=[(service.record['channel'],'/ctxpress-services-api','ro'),(str(tmp_path),'/output','rw')],
        environment={'DOCKER_HOST':'unix:///ctxpress-services-api/docker.sock'},service_gateway=service)
    return owner,service,Path(service.record['channel'])


def start_service(root):
    sdk(root,'GET','/v1.47/images/'+SYNAPSE.replace('/','%2F').replace('@','%40')+'/json')
    identifier=sdk(root,'POST','/v1.47/containers/create',{'Image':SYNAPSE,'ExposedPorts':{'8008/tcp':{}},
        'HostConfig':{'PortBindings':{'8008/tcp':[{'HostIp':'0.0.0.0','HostPort':'48008'}]},'AutoRemove':False}})['Id']
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w') as archive:
        content=b'server_name: localhost\npublic_baseurl: http://localhost:48008\n'
        item=tarfile.TarInfo('data/homeserver.yaml');item.size=len(content);archive.addfile(item,io.BytesIO(content))
    sdk(root,'PUT','/v1.47/containers/'+identifier+'/archive?path=%2F',stream.getvalue())
    sdk(root,'POST','/v1.47/containers/'+identifier+'/start')
    return identifier


def test_cli_resources_and_api_share_ownership_and_stop_in_order(tmp_path,monkeypatch):
    with pipeline(tmp_path,monkeypatch) as (registry,channel,engine,cli):
        model=channel.open();agent=registry.owner('agent',IMAGE);agent.create()
        owner,service,root=prepared_verifier(registry,tmp_path)
        # Synapse VOLUME data is declared before create, not left anonymous.
        engine.image_config={'Volumes':{'/data':{}}}
        identifier=start_service(root);inspection=sdk(root,'GET','/containers/'+identifier+'/json')
        assert inspection['Image']==IMAGE and inspection['HostConfig']['PortBindings']['8008/tcp'][0]['HostIp']=='127.0.0.1'
        assert engine.archives and service.record['objects']['volumes']
        assert owner.record['network_mode']=='host' and agent.record['network_mode']=='none'
        registry.protect_trial(agent.record['container'])
        with pytest.raises(RuntimeError,match='retain owned resources'):registry.cleanup()
        assert root.is_dir() and model.is_dir() and len(engine.objects['containers'])==3
        registry.release_trial(dict(schema='ctxpress.eval.milestone_drain',version=1,container=agent.record['container'],
            phase='author-cleanup-returned',agent_quiesced=True,watcher_joined=True,author_cleanup_returned=True))
        registry.cleanup();channel.cleanup()
        deletes=[path for method,path in engine.operations if method=='DELETE' and path.startswith('/containers/')]
        assert deletes.index('/containers/'+owner.record['container'])<deletes.index('/containers/'+identifier)
        assert all(not objects for objects in engine.objects.values()) and not root.exists() and not model.exists()


def test_worker_recovery_orders_real_native_and_service_journals(tmp_path,monkeypatch):
    with pipeline(tmp_path,monkeypatch) as (registry,channel,engine,_):
        model=channel.open();agent=registry.owner('agent',IMAGE);agent.create()
        owner,service,root=prepared_verifier(registry,tmp_path);start_service(root)
        service.stop();channel.shutdown()
        monkeypatch.setattr(resources.processes,'alive',lambda *args:False)
        milestone_driver.recover_attempt(registry.folder,registry.label)
        assert not root.exists() and not model.exists() and all(not objects for objects in engine.objects.values())
        assert all(json.loads(path.read_text(encoding='utf-8'))['cleaned'] for path in registry.folder.glob('resources-milestone-*.json'))


def test_failed_verifier_removal_retains_services_and_mounted_api(tmp_path,monkeypatch):
    with pipeline(tmp_path,monkeypatch) as (registry,channel,engine,_):
        owner,service,root=prepared_verifier(registry,tmp_path);start_service(root)
        native_docker=resources.docker
        def refuse(*args):
            if args[:1]==('rm',):raise RuntimeError('synthetic native removal failure')
            return native_docker(*args)
        monkeypatch.setattr(resources,'docker',refuse)
        try:
            with pytest.raises(RuntimeError,match='removal failure'):registry.cleanup()
            assert root.is_dir() and not service.record['cleaned'] and len(engine.objects['containers'])==2
        finally:monkeypatch.setattr(resources,'docker',native_docker)
