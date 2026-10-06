"""Real local transports check transparent traffic and restricted CONNECT targets."""
import socket, socketserver, tempfile, threading
from contextlib import contextmanager
from pathlib import Path
import pytest
from ctxpress.harness import socket_bridge
from ctxpress.harness.connect_proxy import make_server
from test_connect_proxy import header


@contextmanager
def channel():
    if not hasattr(socketserver, 'UnixStreamServer'):
        pytest.skip('Unix transport requires Linux/macOS')
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            while True:
                data = self.request.recv(65536)
                if not data:
                    self.request.sendall(b'end')
                    return
                self.request.sendall(data)
    with tempfile.TemporaryDirectory() as folder:
        upstream = socketserver.ThreadingTCPServer(('127.0.0.1',0), Echo)
        upstream.daemon_threads = True
        target = '127.0.0.1:' + str(upstream.server_address[1])
        template = make_server('127.0.0.1', 0, [target], idle_timeout=3)
        try:
            unix = socket_bridge.unix_server(Path(folder) / 'model.sock', template.RequestHandlerClass)
        finally:
            template.server_close()
        local = socket_bridge.loopback_server(Path(folder) / 'model.sock', idle_timeout=3)
        servers = [upstream, unix, local]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield local.server_address, target
        finally:
            for server in reversed(servers):
                server.shutdown(); server.server_close()
            (Path(folder) / 'model.sock').unlink()


def test_connect_and_opaque_bytes_survive_tcp_unix_tcp_and_half_close():
    with channel() as (address, target), socket.create_connection(address, timeout=5) as client:
        payload = b'\x16opaque TLS-like bytes\x00'
        client.sendall(('CONNECT ' + target + ' HTTP/1.1\r\nHost: fixture\r\n\r\n').encode() + payload)
        assert b' 200 ' in header(client)
        received = b''
        while len(received) < len(payload):
            received += client.recv(65536)
        assert received == payload
        client.shutdown(socket.SHUT_WR)
        assert client.recv(3) == b'end'


def test_unix_transport_cannot_bypass_the_host_connect_allowlist():
    with channel() as (address, _), socket.create_connection(address, timeout=5) as client:
        client.sendall(b'CONNECT undeclared.invalid:443 HTTP/1.1\r\nHost: fixture\r\n\r\n')
        assert b' 403 ' in header(client)


def test_existing_socket_path_is_never_unlinked_or_replaced(tmp_path):
    path = tmp_path / 'owned-by-another-run'; path.write_bytes(b'preserve')
    with pytest.raises(ValueError):
        socket_bridge.unix_server(path, socketserver.BaseRequestHandler)
    assert path.read_bytes() == b'preserve'
