"""Native channel ownership and real local sockets; no models or Docker daemon."""
import json,os,socket,socketserver,subprocess,sys,threading
from pathlib import Path
from types import SimpleNamespace
import pytest
from ctxpress.benchmarks.milestone import transport
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.harness.runtime import agent_process
from ctxpress.core import processes
from test_connect_proxy import header

PROJECT='ctxp-ms-'+'3'*24


def fixture(tmp_path,monkeypatch):
    state={'daemon':'fixture-daemon','container':False};calls=[]
    def docker(*args):
        calls.append(args)
        if args[0]=='info':return state['daemon']
        if args[0]=='ps':return 'retained' if state['container'] else ''
        pytest.fail('unexpected Docker operation')
    monkeypatch.setattr(transport,'docker',docker)
    def verify():
        if state['daemon']!='fixture-daemon':raise ValueError('daemon changed')
    registry=SimpleNamespace(project=PROJECT,label='fixture',daemon='fixture-daemon',folder=tmp_path,verify_daemon=verify)
    return registry,state,calls


@pytest.mark.skipif(not hasattr(socketserver,'UnixStreamServer'),reason='native Unix channel requires Linux/macOS')
def test_real_owned_channel_allows_only_declared_model_destination_and_cleans_socket(tmp_path,monkeypatch):
    registry,state,calls=fixture(tmp_path,monkeypatch);channel=transport.Channel(registry,'https://fixture.invalid/responses')
    path=channel.open()
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(5);client.connect(str(path/'model.sock'))
            client.sendall(b'CONNECT other.invalid:443 HTTP/1.1\r\nHost: fixture\r\n\r\n')
            assert b' 403 ' in header(client)
        assert channel.record['phase']=='serving' and (path/'model.sock').is_socket()
    finally:channel.cleanup()
    assert not path.exists() and json.loads(channel.path.read_text(encoding='utf-8'))['cleaned']


def test_channel_directory_is_preserved_while_an_agent_or_credentials_are_retained(tmp_path,monkeypatch):
    registry,state,_=fixture(tmp_path,monkeypatch);channel=transport.Channel(registry,'https://fixture.invalid/responses')
    state['container']=True
    with pytest.raises(ValueError,match='Agent container'):channel.cleanup()
    assert not channel.record['cleaned']
    state['container']=False
    path=tmp_path/('resources-milestone-'+PROJECT+'-agent.json')
    record=dict(schema=transport.CONTAINER_SCHEMA,project=PROJECT,label='fixture',daemon_id=registry.daemon,
        container=PROJECT+'-agent',cleaned=True,credentials_may_exist=True)
    eval_plan.atomic_json(path,record)
    with pytest.raises(ValueError,match='checked and removed'):channel.cleanup()
    record['credentials_may_exist']=False;eval_plan.atomic_json(path,record);channel.cleanup()
    assert channel.record['cleaned']


def test_channel_recovery_refuses_live_worker_foreign_daemon_and_foreign_label(tmp_path,monkeypatch):
    registry,state,_=fixture(tmp_path,monkeypatch);channel=transport.Channel(registry,'https://fixture.invalid/responses')
    monkeypatch.setattr(transport.processes,'alive',lambda *args:True)
    with pytest.raises(ValueError,match='still alive'):transport.recover(channel.path,'fixture')
    monkeypatch.setattr(transport.processes,'alive',lambda *args:False)
    state['daemon']='foreign'
    with pytest.raises(ValueError,match='daemon'):transport.recover(channel.path,'fixture')
    with pytest.raises(ValueError,match='journal'):transport.recover(channel.path,'foreign')
    state['daemon']='fixture-daemon';transport.recover(channel.path,'fixture')
    assert json.loads(channel.path.read_text(encoding='utf-8'))['cleaned']


@pytest.mark.parametrize('url',['http://fixture.invalid','https://key:secret@fixture.invalid','https://fixture.invalid:0','https://fixture.invalid:99999'])
def test_invalid_model_destination_is_rejected_before_any_resource_journal(tmp_path,monkeypatch,url):
    registry,state,calls=fixture(tmp_path,monkeypatch)
    with pytest.raises(ValueError):transport.Channel(registry,url)
    assert not list(tmp_path.iterdir()) and not calls


def test_native_relay_start_checks_identity_before_upload_and_returns_only_loopback(tmp_path,monkeypatch):
    events=[]
    owner=SimpleNamespace(record=dict(role='agent',private_initialized=True,container=PROJECT+'-agent'),
        inspect=lambda:{'HostConfig':{'NetworkMode':'none'}},credentials=lambda source:events.append(('auth',source)))
    cls=SimpleNamespace(ctxpress_private_runtime=True,set_model_relay=lambda url:events.append(('relay',url)))
    def docker(*args):
        events.append(('exec',args));return json.dumps(dict(pid=20,identity='proc:fixture',url='http://127.0.0.1:32123'))
    monkeypatch.setattr(transport,'docker',docker)
    assert transport.initialize(owner,cls,tmp_path/'explicit-auth')=='http://127.0.0.1:32123'
    assert [value[0] for value in events]==['exec','relay','auth']
    source=events[0][1][-1]
    assert 'relay-process.json' in source and 'processes.alive' in source and 'auth.json' not in source


@pytest.mark.skipif(sys.platform!='linux',reason='native owned Linux process/socket lifecycle')
def test_actual_relay_start_and_owned_process_stop_with_local_opaque_traffic(tmp_path,monkeypatch):
    registry,state,_=fixture(tmp_path,monkeypatch)
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(3)
            try:
                while True:
                    data=self.request.recv(4096)
                    if not data:return
                    self.request.sendall(data)
            except OSError:pass
    upstream=socketserver.ThreadingTCPServer(('127.0.0.1',0),Echo);upstream.daemon_threads=True
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    target='127.0.0.1:'+str(upstream.server_address[1])
    channel=transport.Channel(registry,'https://'+target+'/responses');private=tmp_path/'private';private.mkdir()
    record=None
    try:
        mounted=channel.open()
        # Execute the real initialization source in local owned directories.
        # Only its container paths are relocated; there is no Docker substitute
        # or model API behind this loopback echo endpoint.
        source=transport.RELAY_START.replace('/ctxpress-private',str(private)).replace('/ctxpress-channel',str(mounted))
        package=Path(__file__).resolve().parents[1]
        result=subprocess.run([sys.executable,'-c',source],env=dict(os.environ,PYTHONPATH=str(package)),
            capture_output=True,text=True,check=True,timeout=15,cwd=package)
        record=json.loads(result.stdout);assert processes.alive(record['pid'],record['identity'])
        address=('127.0.0.1',int(record['url'].rsplit(':',1)[1]))
        with socket.create_connection(address,timeout=5) as client:
            client.sendall(('CONNECT '+target+' HTTP/1.1\r\nHost: fixture\r\n\r\n').encode())
            assert b' 200 ' in header(client)
            payload=b'\x16opaque synthetic bytes\x00';client.sendall(payload)
            received=b''
            while len(received)<len(payload):received+=client.recv(4096)
            assert received==payload
        agent_process.stop(private/'relay-process.json')
        assert not processes.alive(record['pid'],record['identity']) and not (private/'relay-process.json').exists()
    finally:
        agent_process.stop(private/'relay-process.json')
        channel.cleanup();upstream.shutdown();upstream.server_close()


@pytest.mark.skipif(not hasattr(socketserver,'UnixStreamServer'),reason='native Unix socket lifecycle')
def test_failed_bind_recovery_preserves_foreign_files_then_removes_only_owned_channel(tmp_path,monkeypatch):
    registry,state,_=fixture(tmp_path,monkeypatch);channel=transport.Channel(registry,'https://fixture.invalid/responses')
    def fail(*args):raise RuntimeError('synthetic bind failure')
    monkeypatch.setattr(transport.socket_bridge,'unix_server',fail)
    with pytest.raises(RuntimeError,match='bind failure'):channel.open()
    path=Path(channel.record['channel']);extra=path/'unowned.txt';extra.write_text('preserve', encoding='utf-8')
    monkeypatch.setattr(transport.processes,'alive',lambda *args:False)
    with pytest.raises(ValueError,match='undeclared'):transport.recover(channel.path,'fixture')
    assert extra.read_text(encoding='utf-8')=='preserve' and not json.loads(channel.path.read_text(encoding='utf-8'))['cleaned']
    extra.unlink();transport.recover(channel.path,'fixture');assert not path.exists()
