"""Real loopback transport tests; all upstream data and usage are synthetic."""
import copy
import http.client
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest
from ctxpress.live.telemetry import summary
from ctxpress.live.usage import analyze
from ctxpress.live.proxy import context_length_error, serve
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import CliffCompaction, NoCompaction, WithMemory, Composed, PinRequirements, Trigger
from ctxpress.methods.base import Method


def request(turns=8):
    items = [dict(type='message', role='user', content='task')]
    for i in range(turns):
        items += [dict(type='message', role='assistant', content='reasoned explanation\n' * 100),
                  dict(type='function_call', name='shell', call_id=str(i), arguments=f'cat file{i}.py'),
                  dict(type='function_call_output', call_id=str(i), output='full source line\n' * 300)]
    return dict(model='synthetic-model', input=items, stream=False, tools=[])


@contextmanager
def fixture(method, path, reject=1, status=400, error=None, disconnect_after=None, bad_length=False):
    got=[]
    error = error or dict(error=dict(code='context_length_exceeded',message='input exceeds context window'))
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            if len(got)==disconnect_after:
                self.close_connection=True
                return
            rejected = len(got) <= reject
            value=error if rejected else dict(status='completed', output=[], usage=dict(input_tokens=20, output_tokens=2,
                     input_tokens_details=dict(cached_tokens=0)))
            raw=json.dumps(value).encode()
            self.send_response(status if rejected else 200)
            self.send_header('Content-Length',str(len(raw)+(10 if bad_length else 0)))
            self.end_headers();self.wfile.write(raw)
    upstream=HTTPServer(('127.0.0.1',0),Upstream)
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    proxy,rw=serve(lambda:method,0,f'http://127.0.0.1:{upstream.server_port}',host='127.0.0.1',log=str(path))
    threading.Thread(target=proxy.serve_forever,daemon=True).start()
    def send(body):
        req=urllib.request.Request(f'http://127.0.0.1:{proxy.server_port}/responses',data=json.dumps(body).encode(),
            headers={'Content-Type':'application/json','Authorization':'Bearer fixture-private'})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req,timeout=5) as response:
                return response.status,json.loads(response.read())
        except urllib.error.HTTPError as response:
            raw=response.read()
            try:
                value=json.loads(raw)
            except ValueError:
                value=raw.decode('utf-8','replace')
            return response.code,value
    try:
        yield send,got,rw
    finally:
        proxy.shutdown();proxy.server_close();upstream.shutdown();upstream.server_close()


@pytest.mark.parametrize('method', [CliffCompaction(t=100000),WithMemory(CliffCompaction(t=100000)),
    Composed([PinRequirements(NoCompaction()),CliffCompaction(t=100000)])])
def test_explicit_length_rejection_retries_current_context_without_ingesting_generated_summary(tmp_path,method):
    log=tmp_path/'usage.jsonl'
    body=request();original=copy.deepcopy(body)
    with fixture(method,log) as (send,got,rw):
        status,_=send(body)
        assert status==200 and len(got)==2 and len(got[1]['input'])<len(got[0]['input'])
        session=rw.sessions['default']
        assert session.n==session.ctx.r==1 and session.history_epoch==0
        assert len(session.history)==len(original['input'])
        assert all('fixture-private' not in json.dumps(x) for x in got)
    assert body==original
    rows=[json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
    assert [row['status'] for row in rows]==[400,200]
    assert [row['http_attempt'] for row in rows]==[0,1]
    assert rows[0]['operations']=={} and rows[1]['operations'].get('mechanical_summary')==1
    assert all(row['request']==1 for row in rows)
    telemetry=summary(str(log))
    usage=analyze(dict(requests=telemetry['requests'],usage=telemetry,rewrites=rows))
    assert telemetry['requests']==2 and usage['missing_api_usage']==1 and not usage['complete']
    assert 'fixture-private' not in log.read_text(encoding='utf-8')


@pytest.mark.parametrize('status,error', [(401,dict(error=dict(code='context_length_exceeded'))),
    (429,dict(error=dict(message='too many tokens'))),(500,dict(error=dict(message='maximum context length'))),
    (400,dict(error=dict(code='invalid_tools',message='tools schema is invalid'))),
    (400,dict(error=dict(message='billing unavailable'),input='context_length_exceeded'))])
def test_unrelated_errors_are_returned_without_retry(tmp_path,status,error):
    with fixture(CliffCompaction(t=100000),tmp_path/'log',status=status,error=error) as (send,got,_):
        received,value=send(request())
        assert received==status and value==error and len(got)==1


def test_method_without_overflow_rule_forwards_length_error_unchanged(tmp_path):
    with fixture(NoCompaction(),tmp_path/'log') as (send,got,_):
        status,_=send(request())
        assert status==400 and len(got)==1


def test_trigger_threshold_also_gates_the_inner_overflow_rule(tmp_path):
    with fixture(Trigger(CliffCompaction(t=100000),threshold=1000000),tmp_path/'log') as (send,got,_):
        status,_=send(request())
        assert status==400 and len(got)==1


def test_no_change_hook_cannot_cause_duplicate_upstream_request(tmp_path):
    class NoChange(Method):
        max_overflow_retries=4
        def on_overflow(self,sim,body):
            return True
    with fixture(NoChange(),tmp_path/'log') as (send,got,_):
        status,_=send(request())
        assert status==400 and len(got)==1


def test_transport_retry_cap_stops_even_a_method_that_keeps_changing(tmp_path):
    class AlwaysChange(Method):
        max_overflow_retries=4
        def on_overflow(self,sim,body):
            item=sim.outputs()[0]
            sim.keep_text(item,sim.current_text(item)+'x')
            return True
    log=tmp_path/'log'
    with fixture(AlwaysChange(),log,reject=99) as (send,got,rw):
        status,_=send(request())
        assert status==400 and len(got)==5 and rw.sessions['default'].n==1
    assert len(log.read_text(encoding='utf-8').splitlines())==5


def test_retry_ticket_cannot_change_a_newer_request_in_the_same_session():
    rw=Rewriter(lambda:CliffCompaction(t=100000))
    body=request()
    rejected,info=rw.rewrite_body(body,'session')
    rw.rewrite_body(dict(body,input=body['input']+[dict(type='message',role='user',content='new constraint')]),'session')
    before=copy.deepcopy(rw.sessions['session'].ctx.ctx)
    assert rw.retry_body(body,rejected,info) is None
    assert rw.sessions['session'].ctx.ctx==before


def test_retry_does_not_repeat_entry_model_usage_or_initial_decision_estimate():
    class Metered(Method):
        max_overflow_retries=1
        def reset(self,sim):
            self.pending=0
        def on_ingest(self,sim,item):
            if item['seg']=='out':
                sim.M['pruner_calls']+=1
                sim.M['pruner_input_tokens']+=10
                self.pending+=7
        def overhead(self,sim,r):
            value,self.pending=self.pending,0
            return value
        def on_overflow(self,sim,body):
            sim.to_placeholder(sim.outputs()[:1])
            return True
    rw=Rewriter(Metered)
    body=request()
    rejected,info=rw.rewrite_body(body,'session')
    assert info['pruner_calls']==8 and info['pruner_input_tokens']==80 and info['method_overhead_estimate']==56
    _,retry_info=rw.retry_body(body,rejected,info)
    assert retry_info['pruner_calls']==retry_info['pruner_input_tokens']==retry_info['method_overhead_estimate']==0
    assert rw.sessions['session'].ctx.M['overhead']==56


def test_retry_connection_failure_is_logged_as_an_attempt_with_unknown_usage(tmp_path):
    log=tmp_path/'log'
    with fixture(CliffCompaction(t=100000),log,disconnect_after=2) as (send,got,_):
        status,_=send(request())
        assert status==502 and len(got)==2
    rows=[json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
    assert len(rows)==2 and rows[-1]['http_attempt']==1 and rows[-1]['status'] is None
    assert rows[-1]['usage'] is None and rows[-1]['upstream_error']=='RemoteDisconnected'
    assert summary(str(log))['requests']==2


def test_large_error_body_is_forwarded_intact_without_retry(tmp_path):
    error=dict(error=dict(code='context_length_exceeded'),padding='x'*(1024*1024))
    with fixture(CliffCompaction(t=100000),tmp_path/'log',error=error) as (send,got,_):
        status,value=send(request())
        assert status==400 and value==error and len(got)==1


def test_interrupted_error_body_does_not_trigger_retry_or_claim_known_usage(tmp_path):
    log=tmp_path/'log'
    with fixture(CliffCompaction(t=100000),log,bad_length=True) as (send,got,_):
        with pytest.raises(http.client.IncompleteRead):
            send(request())
        assert len(got)==1
    rows=[json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
    assert len(rows)==1 and rows[0]['status']==400 and rows[0]['stream_error']=='IncompleteRead' and rows[0]['usage'] is None


def test_classifier_uses_only_explicit_error_information():
    assert context_length_error(b'{"error":{"code":"context_length_exceeded"}}')
    assert context_length_error(b'Your prompt is too long')
    assert not context_length_error(b'{"input":"context_length_exceeded","error":{"message":"bad tools"}}')
    assert not context_length_error(b'{"error":{"message":"input_tokens must be positive"}}')


def test_invalid_retry_cap_fails_before_binding():
    class Bad(Method):
        max_overflow_retries=True
    with pytest.raises(ValueError,match='max_overflow_retries'):
        serve(Bad,0,'http://unused.invalid',host='127.0.0.1')
