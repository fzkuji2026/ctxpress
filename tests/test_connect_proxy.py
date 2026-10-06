"""Real local sockets verify destination confinement and transparent tunneling."""
import socket, socketserver, threading
from contextlib import contextmanager
import pytest
from ctxpress.harness.runtime.connect_proxy import authority, make_server


@contextmanager
def fixture():
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            while True:
                data = self.request.recv(65536)
                if not data:
                    self.request.sendall(b'end')
                    return
                self.request.sendall(data)
    upstream = socketserver.ThreadingTCPServer(('127.0.0.1',0),Echo)
    upstream.daemon_threads = True
    target = f'127.0.0.1:{upstream.server_address[1]}'
    proxy = make_server('127.0.0.1',0,[target],idle_timeout=3)
    for server in (upstream,proxy):
        threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        yield proxy.server_port, target
    finally:
        for server in (proxy,upstream):
            server.shutdown();server.server_close()


def header(connection):
    value = b''
    while not value.endswith(b'\r\n\r\n'):
        value += connection.recv(1)
    return value


def test_tunnel_keeps_pipelined_bytes_and_allows_half_close():
    with fixture() as (port,target), socket.create_connection(('127.0.0.1',port),timeout=5) as client:
        payload = b'\x16\x03\x01opaque TLS-like bytes\x00'
        client.sendall(f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n'.encode()+payload)
        assert b' 200 ' in header(client)
        received = b''
        while len(received) < len(payload):
            received += client.recv(65536)
        assert received == payload
        client.shutdown(socket.SHUT_WR)
        assert client.recv(3) == b'end'


@pytest.mark.parametrize('target,status',[('undeclared.invalid:443',403),
    ('127.0.0.1:1',403),('user:password@host:443',400),('host:443/path',400),('host:bad',400)])
def test_only_declared_destinations_can_connect(target,status):
    with fixture() as (port,_), socket.create_connection(('127.0.0.1',port),timeout=5) as client:
        client.sendall(f'CONNECT {target} HTTP/1.1\r\nHost: fixture\r\n\r\n'.encode())
        assert f' {status} '.encode() in header(client)


def test_plain_http_cannot_use_the_relay_as_an_open_proxy():
    with fixture() as (port,_), socket.create_connection(('127.0.0.1',port),timeout=5) as client:
        client.sendall(b'GET http://undeclared.invalid/ HTTP/1.1\r\nHost: fixture\r\n\r\n')
        assert b' 501 ' in header(client)


def test_empty_destination_list_is_rejected_before_binding():
    with pytest.raises(ValueError):
        make_server('127.0.0.1',0,[])


def test_authority_normalizes_only_host_case_and_trailing_dot():
    assert authority('Example.COM.:443') == ('example.com',443)
    assert authority('[::1]:443') == ('::1',443)


def test_declared_via_proxy_preserves_pipelined_bytes_and_half_close():
    with fixture() as (port,target):
        proxy=make_server('127.0.0.1',0,[target],idle_timeout=3,via=f'http://127.0.0.1:{port}')
        threading.Thread(target=proxy.serve_forever,daemon=True).start()
        try:
            with socket.create_connection(('127.0.0.1',proxy.server_port),timeout=5) as client:
                payload=b'\x16\x03\x01opaque two-hop bytes\x00'
                client.sendall(f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n'.encode()+payload)
                assert b' 200 ' in header(client)
                received=b''
                while len(received)<len(payload):received+=client.recv(65536)
                assert received==payload
                client.shutdown(socket.SHUT_WR);assert client.recv(3)==b'end'
        finally:
            proxy.shutdown();proxy.server_close()


@pytest.mark.parametrize('via',['https://proxy.invalid','http://user:password@proxy.invalid',
    'http://proxy.invalid/path','http://proxy.invalid?key=secret'])
def test_invalid_via_is_rejected_before_binding(via):
    with pytest.raises(ValueError,match='HTTP proxy address'):
        make_server('127.0.0.1',0,['provider.invalid:443'],via=via)
