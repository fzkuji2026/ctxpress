"""Native compaction is forwarded intact and billed separately; all API data is synthetic."""
import http.client, json, threading, urllib.error, urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest
from ctxpress.live.telemetry import summary
from ctxpress.live.usage import analyze
from ctxpress.live.proxy import serve
from ctxpress.methods import CliffCompaction


@contextmanager
def fixture(log,compact_status=200,compact_usage=True,bad_length=False,method=None):
    got=[]
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            raw=self.rfile.read(int(self.headers['Content-Length']))
            got.append((self.path,raw,self.headers.get('Authorization')))
            compact='/responses/compact' in self.path
            value=dict(object='response.compaction' if compact else 'response',
                       output=[dict(type='compaction',encrypted_content='private-encrypted-context')])
            if not compact or compact_usage:
                value['usage']=dict(input_tokens=30 if compact else 10,output_tokens=7 if compact else 2,
                                    input_tokens_details=dict(cached_tokens=20 if compact else 4,
                                                              cache_write_tokens=5 if compact else 6))
            response=json.dumps(value).encode()
            self.send_response(compact_status if compact else 200)
            self.send_header('Content-Length',str(len(response)+(10 if compact and bad_length else 0)));self.end_headers();self.wfile.write(response)
    upstream=HTTPServer(('127.0.0.1',0),Upstream)
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    proxy,rw=serve(method or (lambda:CliffCompaction(t=200000)),0,f'http://127.0.0.1:{upstream.server_port}/v1',
                   host='127.0.0.1',log=str(log))
    threading.Thread(target=proxy.serve_forever,daemon=True).start()
    def send(path,raw,headers=None):
        request=urllib.request.Request(f'http://127.0.0.1:{proxy.server_port}'+path,data=raw,
            headers={'Content-Type':'application/json','Authorization':'Bearer private-fixture-token', **(headers or {})})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request,timeout=5) as response:
                return response.status,response.read()
        except urllib.error.HTTPError as response:
            return response.code,response.read()
    try:
        yield send,got,rw
    finally:
        proxy.shutdown();proxy.server_close();upstream.shutdown();upstream.server_close()


def test_native_payload_and_response_are_not_rewritten_and_usage_is_included(tmp_path):
    log=tmp_path/'requests.jsonl'
    raw=b'{ "model": "fixture", "input": [{"role":"user","content":"private task"}] }'
    with fixture(log) as (send,got,rw):
        status,response=send('/responses/compact?fixture=true',raw)
        assert status==200 and json.loads(response)['output'][0]['encrypted_content']=='private-encrypted-context'
        assert got[0]==('/v1/responses/compact?fixture=true',raw,'Bearer private-fixture-token') and rw.sessions=={}
        send('/responses',json.dumps(dict(model='fixture',input=[dict(role='user',content='next turn')])).encode())
    rows=[json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
    assert rows[0]['type']=='native_compaction' and rows[0]['completed'] is True and 'request' not in rows[0]
    assert len(got)==2 and rows[1]['request']==1
    telemetry=summary(str(log))
    assert telemetry['requests']==telemetry['native_compaction_calls']==1 and telemetry['summary_calls']==0
    assert (telemetry['api_input_tokens'],telemetry['api_cached_tokens'],telemetry['api_output_tokens'])==(40,24,9)
    assert [r['usage']['cache_write_tokens'] for r in rows] == [5,6]
    assert telemetry['api_cache_write_tokens']==11 and telemetry['missing_api_cache_write_usage']==0
    accounting=analyze(dict(requests=1,usage=telemetry,rewrites=rows))
    assert accounting['complete'] and accounting['native_compaction_calls']==1
    assert 'private-fixture-token' not in log.read_text(encoding='utf-8') and 'private task' not in log.read_text(encoding='utf-8')
    assert 'private-encrypted-context' not in log.read_text(encoding='utf-8')


@pytest.mark.parametrize('status,usage',[(200,False),(400,False)])
def test_missing_native_usage_stays_unknown_and_errors_do_not_enter_overflow_retry(tmp_path,status,usage):
    log=tmp_path/'requests.jsonl'
    with fixture(log,status,usage) as (send,got,rw):
        code,_=send('/responses/compact',b'{"input":[]}')
        assert code==status and len(got)==1 and rw.sessions=={}
        send('/responses',b'{"model":"fixture","input":[]}')
    rows=[json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
    assert rows[0]['completed']==(status==200) and rows[0]['usage'] is None
    accounting=analyze(dict(requests=1,usage=summary(str(log)),rewrites=rows))
    assert not accounting['complete'] and accounting['missing_api_usage']==1


def test_native_log_and_telemetry_must_agree():
    result=dict(requests=1,rewrites=[dict(request=1,usage=dict(input_tokens=1,cached_tokens=0,output_tokens=1)),
        dict(type='native_compaction',usage=dict(input_tokens=2,cached_tokens=0,output_tokens=1))],
        usage=dict(summary_calls=0,native_compaction_calls=0))
    accounting=analyze(result)
    assert not accounting['complete'] and 'native_compaction_calls count disagrees with request records' in accounting['usage_conflicts']


def test_interrupted_native_http_body_is_not_a_completed_compaction(tmp_path):
    log=tmp_path/'requests.jsonl'
    with fixture(log,bad_length=True) as (send,got,rw):
        with pytest.raises(http.client.IncompleteRead):
            send('/responses/compact',b'{"input":[]}')
    row=json.loads(log.read_text(encoding='utf-8'))
    assert row['status']==200 and row['completed'] is False and row['stream_error']=='IncompleteRead'
    assert row['usage'] is None
