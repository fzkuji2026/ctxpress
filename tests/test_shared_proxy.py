"""Successive host launches of one run share the method's live session; all API data is synthetic."""
import json, os, signal, sys, threading, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='shared proxy uses POSIX locks and sessions')


def upstream():
    got = []
    class API(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            raw = json.dumps(dict(object='response', output=[], usage=dict(input_tokens=10, output_tokens=1))).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    server = HTTPServer(('127.0.0.1', 0), API)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, got


def history(turns):
    items = [dict(type='message', role='user', content=[dict(type='input_text', text='task')])]
    for i in range(turns):
        items += [dict(type='function_call', call_id=f'c{i}', name='shell', arguments='{}'),
                  dict(type='function_call_output', call_id=f'c{i}', output=f'output-{i} ' + 'x' * 2000)]
    return items


def send(port, turns):
    body = json.dumps(dict(model='fixture', prompt_cache_key='s', input=history(turns))).encode()
    request = urllib.request.Request(f'http://127.0.0.1:{port}/responses', data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
        response.read()


def test_resumed_launch_continues_the_same_method_session(tmp_path, monkeypatch):
    from ctxpress.hosts.codex import shared_proxy
    server, got = upstream()
    shared = tmp_path / 'shared'
    base = dict(entry={'class': 'ComplexityTrap', 'args': {'n': 1}}, upstream=f'http://127.0.0.1:{server.server_port}/v1',
                via=None, tools=False)
    first = dict(base, log=str(tmp_path / 'one.jsonl'), store_dir=str(tmp_path / 'store' / ('a' * 32)))
    second = dict(base, log=str(tmp_path / 'two.jsonl'), store_dir=str(tmp_path / 'store' / ('b' * 32)))
    port = shared_proxy.attach(shared, first)
    state = json.loads((shared / 'proxy.json').read_text())
    try:
        send(port, 3)
        assert shared_proxy.detach(port, first)['drained']
        assert shared_proxy.attach(shared, second) == port            # the resume reuses the live proxy
        assert json.loads((shared / 'proxy.json').read_text())['pid'] == state['pid']
        send(port, 4)
        shared_proxy.detach(port, second)
        one = [json.loads(line) for line in open(first['log'])]
        two = [json.loads(line) for line in open(second['log'])]
        assert [row['request'] for row in one] == [1] and [row['request'] for row in two] == [2]
        assert two[0]['history_rebased'] is False                     # the method continued, it did not restart
        masked = [i for i in got[1]['input'] if i.get('type') == 'function_call_output' and not i['output'].startswith('output-')]
        assert len(masked) == 3
        with pytest.raises(RuntimeError, match='different method'):
            shared_proxy.attach(shared, dict(second, entry={'class': 'ComplexityTrap', 'args': {'n': 2}}))
    finally:
        os.kill(state['pid'], signal.SIGTERM)
        server.shutdown(); server.server_close()
