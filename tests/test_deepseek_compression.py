import json
from types import SimpleNamespace
import pytest
from ctxpress.live import deepseek
from ctxpress.live.context import LiveContext
from ctxpress.methods.infinite_context import InfiniteContext


def transport(monkeypatch, finish='stop', status=200):
    sent = {}
    reply = dict(model='deepseek-flash', usage=dict(prompt_tokens=100, prompt_cache_hit_tokens=60,
                 completion_tokens=10), choices=[dict(finish_reason=finish, message=dict(content='{"records":[]}'))])
    class Connection:
        def __init__(self, host, timeout):
            sent.update(host=host, timeout=timeout)
        def request(self, method, path, body, headers):
            sent.update(path=path, payload=json.loads(body), headers=headers)
        def getresponse(self):
            return SimpleNamespace(status=status, read=lambda: json.dumps(reply).encode())
        def close(self):
            sent['closed'] = True
    monkeypatch.setattr(deepseek.http.client, 'HTTPSConnection', Connection)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-secret')
    return sent


def test_live_routes_independently_and_records_cache(monkeypatch):
    sent = transport(monkeypatch)
    records = []
    service = SimpleNamespace(record=records.append)  # no host transport required
    ctx = LiveContext(InfiniteContext(), summarizer=service)
    assert ctx.model_text('json', 'data', purpose='infinite_context_compression',
                          model='deepseek-flash') == '{"records":[]}'
    assert sent['host'] == 'api.deepseek.com'
    assert sent['payload']['thinking'] == {'type': 'disabled'}
    assert sent['headers']['Authorization'] == 'Bearer test-secret'
    assert records[0]['usage']['cached_tokens'] == 60
    assert records[0]['usage']['output_tokens'] == 10
    assert 'test-secret' not in json.dumps(records)


@pytest.mark.parametrize('finish,status', [('length', 200), ('stop', 401)])
def test_failure_keeps_context_and_records_usage(monkeypatch, finish, status):
    sent = transport(monkeypatch, finish, status)
    records = []
    ctx = LiveContext(InfiniteContext(), summarizer=SimpleNamespace(record=records.append))
    ctx.add_message('user', 'preserve this', fixed=True)
    before = [dict(x) for x in ctx.ctx]
    assert ctx.model_text('json', 'data', purpose='infinite_context_compression', model='deepseek-flash') is None
    assert ctx.ctx == before and ctx.M['summary_failed'] == 1
    assert records[0]['completed'] is False and sent['closed']
    if status == 200:
        assert records[0]['usage']['output_tokens'] == 10


def test_missing_key_never_contacts_host(monkeypatch, tmp_path):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setattr(deepseek.Path, 'home', lambda: tmp_path)
    with pytest.raises(ValueError, match='DEEPSEEK_API_KEY'):
        deepseek.summarize('json', 'data', record=lambda x: None)


def test_local_credential_fallback(monkeypatch, tmp_path):
    sent = transport(monkeypatch)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setattr(deepseek.Path, 'home', lambda: tmp_path)
    credential = tmp_path/'.config/ctxpress/deepseek-api-key'
    credential.parent.mkdir(parents=True)
    credential.write_text('local-test-secret')
    deepseek.summarize('json', 'data', record=lambda x: None)
    assert sent['headers']['Authorization'] == 'Bearer local-test-secret'


def test_owned_unix_relay_returns_usage_without_credentials(monkeypatch, tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler
    from ctxpress.harness.runtime.socket_bridge import unix_server
    transport(monkeypatch)
    class Quiet(BaseHTTPRequestHandler):
        rbufsize = 0
        def log_message(self, *args):
            pass
    path = tmp_path/'model.sock'
    server = unix_server(path, deepseek.relay_handler(Quiet))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        records = []
        assert deepseek._relay('json', 'data'*20000, records.append, path) == '{"records":[]}'
        assert records[0]['usage']['cached_tokens'] == 60
        assert 'test-secret' not in json.dumps(records)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
