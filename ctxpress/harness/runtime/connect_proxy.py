"""Host-side CONNECT relay for DNS-isolated experiment containers.

The host resolves only explicitly declared destinations. TLS stays end-to-end;
request headers and tunneled bytes are never written to logs.
"""
from __future__ import annotations
import argparse, os, selectors, socket, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ctxpress.core.artifacts import atomic_json
from ctxpress.core import processes


def authority(value):
    parsed = urllib.parse.urlsplit('//'+value)
    if (parsed.username is not None or parsed.password is not None or parsed.path or
            parsed.query or parsed.fragment or not parsed.hostname or parsed.port is None):
        raise ValueError('target must be HOST:PORT without credentials or a path')
    return parsed.hostname.lower().rstrip('.'), parsed.port


def relay(client, upstream, idle_timeout):
    last = time.monotonic()
    with selectors.DefaultSelector() as poll:
        poll.register(client, selectors.EVENT_READ, upstream)
        poll.register(upstream, selectors.EVENT_READ, client)
        while poll.get_map():
            remaining = idle_timeout - (time.monotonic()-last)
            if remaining <= 0:
                return
            for event, _ in poll.select(min(remaining, 5)):
                data = event.fileobj.recv(65536)
                if not data:
                    poll.unregister(event.fileobj)
                    try:
                        event.data.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                else:
                    event.data.sendall(data)
                    last = time.monotonic()


def proxy_address(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'http' or not parsed.hostname or parsed.username is not None or
            parsed.password is not None or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('relay via must be an HTTP proxy address without credentials')
    return parsed.hostname, parsed.port or 80


def connect(target, timeout, via=None):
    upstream = socket.create_connection(proxy_address(via) if via else target, timeout=timeout)
    try:
        if via:
            host, port = target
            name = '[' + host + ']' if ':' in host else host
            destination = f'{name}:{port}'
            upstream.sendall(f'CONNECT {destination} HTTP/1.1\r\nHost: {destination}\r\n\r\n'.encode('ascii'))
            # No read-ahead: the bytes following these headers belong to TLS.
            header = bytearray()
            while not header.endswith(b'\r\n\r\n'):
                value = upstream.recv(1)
                if not value or len(header) >= 16384:
                    raise OSError('relay proxy returned invalid headers')
                header.extend(value)
            first = bytes(header).split(b'\r\n', 1)[0].split()
            if len(first) < 2 or first[1] != b'200':
                raise OSError('relay proxy refused CONNECT')
        return upstream
    except BaseException:
        upstream.close()
        raise


def make_server(bind, port, targets, idle_timeout=600, max_connections=32, via=None):
    allowed = frozenset(authority(value) for value in targets)
    if not allowed or idle_timeout <= 0 or type(max_connections) is not int or max_connections < 1:
        raise ValueError('declare targets, a positive idle timeout and connection limit')
    if via:
        proxy_address(via)
    capacity = threading.BoundedSemaphore(max_connections)

    class Handler(BaseHTTPRequestHandler):
        # Do not read tunnel bytes ahead while parsing CONNECT headers.
        rbufsize = 0
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def setup(self):
            self.request.settimeout(idle_timeout)
            super().setup()

        def do_CONNECT(self):
            try:
                target = authority(self.path)
            except ValueError:
                self.send_error(400, 'invalid CONNECT destination')
                return
            if target not in allowed:
                self.send_error(403, 'destination is not declared')
                return
            if not capacity.acquire(blocking=False):
                self.send_error(503, 'connection limit reached')
                return
            connected = False
            try:
                with connect(target, min(idle_timeout, 15), via) as upstream:
                    upstream.settimeout(idle_timeout)
                    self.send_response(200, 'Connection established')
                    self.end_headers(); self.wfile.flush()
                    connected = True
                    relay(self.connection, upstream, idle_timeout)
            except OSError:
                if not connected:
                    self.send_error(502, 'upstream connection failed')
            finally:
                capacity.release()
                self.close_connection = True

    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--target', action='append', required=True, help='allowed HOST:PORT; repeat for each destination')
    parser.add_argument('--idle-timeout', type=float, default=600)
    parser.add_argument('--ready-file', required=True, help='JSON process identity and selected local port')
    args = parser.parse_args(argv)
    with make_server(args.bind, args.port, args.target, args.idle_timeout) as server:
        atomic_json(args.ready_file, dict(pid=os.getpid(), identity=processes.identity(os.getpid()),
            url=f'http://{args.bind}:{server.server_port}', targets=sorted(args.target), started=time.time()))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
