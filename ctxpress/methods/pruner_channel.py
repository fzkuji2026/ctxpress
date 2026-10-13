"""Fixed local SWE-Pruner operation over the owned evaluation channel."""
import http.client
import json
import socket
import urllib.request
from urllib.parse import urlsplit

ROUTE = '/ctxpress/swe-pruner'
LIMIT = 8 * 1024 * 1024


def validate_endpoint(url):
    value = urlsplit(url)
    if (value.scheme != 'http' or value.hostname != '127.0.0.1' or
            value.username is not None or value.password is not None or
            value.path != '/prune' or value.query or value.fragment or
            value.port is None):
        raise ValueError('native SWE-Pruner requires http://127.0.0.1:PORT/prune')
    return url


def relay_handler(base, endpoint):
    validate_endpoint(endpoint)
    class Handler(base):
        def do_POST(self):
            if self.path != ROUTE:
                parent = getattr(super(), 'do_POST', None)
                if parent:
                    return parent()
                self.send_error(404)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= LIMIT:
                    raise ValueError('invalid size')
                raw = self.rfile.read(length)
                data = json.loads(raw)
                if (set(data) != {'query', 'code', 'threshold', 'always_keep_first_frags', 'chunk_overlap_tokens'} or
                        not isinstance(data['query'], str) or not isinstance(data['code'], str)):
                    raise ValueError('invalid pruning request')
                request = urllib.request.Request(endpoint, raw, {'Content-Type': 'application/json'})
                # Ignore ambient HTTP proxies; this endpoint is loopback-only.
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=60) as response:
                    raw = response.read(LIMIT + 1)
                if len(raw) > LIMIT:
                    raise ValueError('oversized response')
                json.loads(raw)
                status = 200
            except Exception:
                status, raw = 502, b'{"error":"local SWE-Pruner request failed"}'
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    return Handler


def request(body, timeout, channel):
    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(str(channel))
    connection = Connection('localhost', timeout=timeout)
    try:
        connection.request('POST', ROUTE, body, {'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(LIMIT + 1)
        if response.status != 200 or len(raw) > LIMIT:
            raise ValueError('SWE-Pruner channel request failed')
        return json.loads(raw)
    finally:
        connection.close()
