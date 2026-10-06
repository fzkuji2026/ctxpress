"""Real Unix HTTP, chunked archives, streaming logs and exec upgrade; fake daemon."""
import contextlib,json,os,shutil,socket,subprocess,sys,tempfile,threading
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace
from pathlib import Path
import pytest
from ctxpress.harness.runtime import service_gateway as gateway, socket_bridge
from test_service_resources import Engine,IMAGE,PROJECT

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='native Unix Docker service transport')


@contextlib.contextmanager
def running(tmp_path):
    engine=Engine();stop=threading.Event();uploads=[]
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1';rbufsize=0
        def log_message(self,*args):pass
        def respond(self):
            size=int(self.headers.get('Content-Length','0'));body=self.rfile.read(size)
            if self.path.endswith('/logs?follow=1'):
                self.send_response(200);self.send_header('Connection','close');self.end_headers()
                self.wfile.write(b'ready\n');self.wfile.flush();stop.wait(5);self.close_connection=True;return
            if self.headers.get('Upgrade')=='tcp':
                self.send_response(101);self.send_header('Connection','Upgrade');self.send_header('Upgrade','tcp');self.end_headers();self.wfile.flush()
                value=self.rfile.read(4);self.wfile.write(value);self.wfile.flush();self.close_connection=True;return
            if self.command=='PUT' and '/archive' in self.path:
                uploads.append(body);response=gateway.Response.json({'copied':len(body)})
            else:response=engine.request(self.command,self.path,body)
            self.send_response(response.status)
            for key,value in response.headers.items():self.send_header(key,value)
            self.send_header('Content-Length',str(len(response.body)));self.send_header('Connection','close');self.end_headers()
            if self.command!='HEAD':self.wfile.write(response.body)
            self.close_connection=True
        do_GET=do_HEAD=do_POST=do_PUT=do_DELETE=respond
    # Keep the fake Engine endpoint within AF_UNIX's pathname limit even when
    # pytest artifacts live below a long checkout or --basetemp path.
    socket_home=tempfile.TemporaryDirectory(prefix='ctxpress-fake-daemon-')
    daemon=Path(socket_home.name)/'daemon.sock';server=socket_bridge.unix_server(daemon,Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    registry=SimpleNamespace(project=PROJECT,label='fixture-label',daemon=engine.daemon,folder=tmp_path)
    service=gateway.Gateway(registry,{'database':{'id':IMAGE,'reference':'redis:7'}},gateway.Backend(daemon))
    try:
        root=service.open()
        yield service,engine,root,uploads
    finally:
        stop.set();service.cleanup();server.shutdown();server.server_close();thread.join(3)
        socket_home.cleanup()
        assert not thread.is_alive()


def request(root,method,path,body=b'',headers=None):
    connection=gateway.Connection(root/'docker.sock',timeout=3)
    try:
        connection.request(method,path,body,headers or {'Content-Type':'application/json'})
        response=connection.getresponse();return response.status,response.read(),dict(response.getheaders())
    finally:connection.close()


def create(root):
    status,body,_=request(root,'POST','/v1.47/containers/create',b'{"Image":"redis:7","HostConfig":{"PublishAllPorts":true},"ExposedPorts":{"6379/tcp":{}}}')
    assert status==201;return json.loads(body)['Id']


def test_real_unix_http_handles_sdk_requests_and_hides_foreign_resources(tmp_path):
    with running(tmp_path) as (service,engine,root,_):
        assert request(root,'GET','/_ping')[1]==b'OK'
        identifier=create(root)
        status,body,_=request(root,'GET','/v1.47/containers/json?all=1')
        assert status==200 and len(json.loads(body))==1
        assert request(root,'POST','/containers/'+identifier+'/start')[0]==200
        assert request(root,'GET','/containers/'+'b'*64+'/json')[0]==404
        assert request(root,'POST','/build',b'{}')[0]==403
        assert not any(path=='/build' for _,path,_,_ in engine.calls)
        assert service.record['rejections']==2
    assert not root.exists() and all(not rows for rows in engine.objects.values())


def test_chunked_archive_upload_and_fragmented_body_are_preserved(tmp_path):
    with running(tmp_path) as (_,_,root,uploads):
        identifier=create(root);connection=gateway.Connection(root/'docker.sock',timeout=3)
        try:
            connection.request('PUT','/containers/'+identifier+'/archive?path=%2Fdata',
                iter([b'tar-first',b'-second']),{'Content-Type':'application/x-tar'},encode_chunked=True)
            response=connection.getresponse();assert response.status==200;response.read()
        finally:connection.close()
        assert uploads==[b'tar-first-second']
        # Unbuffered SocketIO.read may return a short piece even with a larger
        # Content-Length; the transport must keep reading until the body ends.
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(3);client.connect(str(root/'docker.sock'))
            body=b'{"Image":"redis:7"}'
            client.sendall(b'POST /containers/create HTTP/1.1\r\nHost: localhost\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body[:4])
            client.sendall(body[4:]);assert b'201' in client.recv(4096)


def test_following_logs_allows_other_api_calls_before_the_stream_closes(tmp_path):
    with running(tmp_path) as (_,_,root,_):
        identifier=create(root);connection=gateway.Connection(root/'docker.sock',timeout=3)
        try:
            connection.request('GET','/containers/'+identifier+'/logs?follow=1')
            response=connection.getresponse();assert response.status==200 and response.read(6)==b'ready\n'
            assert request(root,'GET','/version')[0]==200
        finally:connection.close()


def test_exec_upgrade_keeps_raw_bytes_after_the_http_header(tmp_path):
    with running(tmp_path) as (_,_,root,_):
        identifier=create(root)
        status,body,_=request(root,'POST','/containers/'+identifier+'/exec',b'{"Cmd":["echo","fixture"],"AttachStdout":true}')
        assert status==201;execution=json.loads(body)['Id']
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(3);client.connect(str(root/'docker.sock'))
            target='/exec/'+execution+'/start'
            client.sendall(('POST '+target+' HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: tcp\r\nContent-Length: 2\r\n\r\n{}').encode())
            header=bytearray()
            while not header.endswith(b'\r\n\r\n'):header.extend(client.recv(1))
            assert b'101' in header
            client.sendall(b'\x00abc');assert client.recv(4)==b'\x00abc'


def test_ambiguous_request_framing_is_rejected_without_daemon_mutation(tmp_path):
    with running(tmp_path) as (_,engine,root,_):
        before=len(engine.calls)
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(3);client.connect(str(root/'docker.sock'))
            client.sendall(b'POST /containers/create HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}')
            assert b'400' in client.recv(4096)
        assert not any(method=='POST' for method,_,_,_ in engine.calls[before:])


@pytest.mark.skipif(shutil.which('docker') is None,reason='existing Docker CLI is unavailable')
def test_existing_real_docker_cli_against_fake_engine_only(tmp_path):
    config=tmp_path/'cli-config';config.mkdir()
    with running(tmp_path) as (_,engine,root,_):
        command=[shutil.which('docker'),'--config',str(config),'--host','unix://'+str(root/'docker.sock')]
        env={'PATH':os.environ.get('PATH',''),'HOME':str(config)}
        ping=subprocess.run(command+['version','--format','{{.Server.APIVersion}}'],env=env,capture_output=True,text=True,timeout=10)
        assert ping.returncode==0,ping.stderr
        assert ping.stdout.strip()=='1.47'
        created=subprocess.run(command+['create','--pull=never','--name','cli-fixture','--publish','6379','redis:7'],
            env=env,capture_output=True,text=True,timeout=10)
        assert created.returncode==0,created.stderr
        assert len(engine.objects['containers'])==1 and len(created.stdout.strip())==64
