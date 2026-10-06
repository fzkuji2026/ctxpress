"""Local socket transport for model traffic in containers with no IP network.

The host owns a restricted CONNECT proxy. Its handler can listen on a Unix
socket; a container forwards loopback TCP to that socket. This module does not
resolve destinations, inspect HTTP headers, or record traffic.
"""
from __future__ import annotations
import argparse, socket, socketserver, threading
from pathlib import Path
from ctxpress.harness.runtime.connect_proxy import relay
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.core import processes


class UnixServer(socketserver.ThreadingMixIn, getattr(socketserver, 'UnixStreamServer', socketserver.TCPServer)):
    daemon_threads = True


class TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = False


def unix_server(path, handler):
    """Bind an unused socket in an owned directory; never unlink another run."""
    if not hasattr(socketserver, 'UnixStreamServer'):
        raise ValueError('Unix socket transport requires a supported Linux/macOS host')
    path = Path(path)
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise ValueError('provide an unused absolute Unix socket path')
    return UnixServer(str(path), handler)


def loopback_server(path, port=0, idle_timeout=600, max_connections=32):
    if not Path(path).is_absolute() or idle_timeout <= 0 or type(max_connections) is not int or max_connections < 1:
        raise ValueError('declare an absolute socket, positive timeout and connection limit')
    capacity = threading.BoundedSemaphore(max_connections)

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            if not capacity.acquire(blocking=False):
                return
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
                    upstream.settimeout(min(idle_timeout, 15))
                    upstream.connect(str(path))
                    upstream.settimeout(idle_timeout)
                    self.request.settimeout(idle_timeout)
                    relay(self.request, upstream, idle_timeout)
            except OSError:
                pass
            finally:
                capacity.release()

    return TCPServer(('127.0.0.1', port), Handler)


def main(argv=None):
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--ready-file', required=True)
    args = parser.parse_args(argv)
    with loopback_server(args.socket) as server:
        eval_plan.atomic_json(args.ready_file, dict(pid=os.getpid(), identity=processes.identity(os.getpid()),
            url=f'http://127.0.0.1:{server.server_address[1]}'))
        server.serve_forever()


if __name__ == '__main__':
    main()
