"""Summary failure is atomic, and real requests use and account for the same upstream model."""
import copy
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from ctxpress.live.context import LiveContext
from ctxpress.live.rewrite import Rewriter
from ctxpress.live.proxy import serve, find_usage
from ctxpress.live.summarize import response_text
from ctxpress.methods import CodexAutoCompact, ReSum, ACON, AgentFold, WithMemory, Composed, SlidingWindow
from ctxpress.methods.summaries import RESUM_GUIDANCE
from ctxpress.live.telemetry import summary


@pytest.mark.parametrize("segment", [False, True])
def test_summary_failure_keeps_history_and_members(segment):
    def broken(items):
        items[0]["text"] = "mutated callback input"
        raise RuntimeError("model failed")
    ctx = LiveContext(CodexAutoCompact(100), summarizer=broken)
    ctx.add_message("user", "task")
    ctx.add_call("a", "cat a.py")
    ctx.add_output("a", "original source" * 100)
    before = copy.deepcopy((ctx.ctx, ctx.members))
    if segment:
        ctx.summarize_segment(list(ctx.ctx))
    else:
        ctx.summarize()
    assert (ctx.ctx, ctx.members) == before and ctx.M["summary_failed"] == 1


def test_summary_input_uses_current_representation():
    seen = []
    ctx = LiveContext(CodexAutoCompact(100), summarizer=lambda items: seen.extend(items) or "note")
    ctx.add_call("a", "cat a.py")
    ctx.add_output("a", "SECRET ORIGINAL" * 100)
    ctx.keep_text(ctx.outputs()[0], "current kept text")
    ctx.summarize()
    assert seen[-1]["text"] == "current kept text"


def test_media_and_call_pair_survive_history_summary():
    inputs = [{"type": "message", "role": "user", "content": "task"},
              {"type": "function_call", "call_id": "a", "name": "exec", "arguments": "screenshot"},
              {"type": "function_call_output", "call_id": "a", "output": [
                  {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]},
              {"type": "message", "role": "assistant", "content": "analysis" * 200}]
    body, _ = Rewriter(lambda: CodexAutoCompact(10), summarizer=lambda items: "note").rewrite_body({"input": inputs}, "s")
    assert inputs[1] in body["input"] and inputs[2] in body["input"]


@pytest.mark.parametrize('method',[ACON(t_hist=1), SlidingWindow(t=1)])
def test_opaque_native_compaction_survives_summary_and_deletion(method):
    opaque=dict(type='compaction',id='native-state',encrypted_content='opaque-host-context')
    inputs=[dict(role='user',content='task'),opaque,dict(role='assistant',content='earlier work'*100)]
    body,_=Rewriter(lambda:copy.deepcopy(method),summarizer=lambda items:'note').rewrite_body(dict(input=inputs),'s')
    assert opaque in body['input']


def test_acons_observation_summary_uses_callback():
    ctx = LiveContext(ACON(t_obs=20), summarizer=lambda items: "selected relevant lines")
    ctx.add_call("a", "cat a.py")
    ctx.add_output("a", "whole source\n" * 100)
    assert ctx.render(ctx.outputs()[0]) == "selected relevant lines"


@pytest.mark.parametrize("segment", [False, True])
def test_instructions_added_after_first_turn_survive_summary(segment):
    ctx = LiveContext(ReSum(1), summarizer=lambda items: "note")
    ctx.add_message("user", "task")
    ctx.add_message("assistant", "earlier work")
    ctx.add_message("developer", "new constraint", fixed=True)
    if segment:
        ctx.summarize_segment(list(ctx.ctx))
    else:
        ctx.summarize()
    assert any(s.get("text") == "new constraint" and s.get("role") == "developer" for s in ctx.ctx)


def test_usage_counts_summary_even_if_main_request_never_completes(tmp_path):
    usage = dict(input_tokens=30, output_tokens=7, input_tokens_details=dict(cached_tokens=20))
    normalized = find_usage(json.dumps(dict(usage=usage)).encode())
    assert normalized["cached_tokens"] == 20
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps(dict(type="summary", t=1, usage=normalized)) + '\n{"partial":', encoding="utf-8")
    report = summary(str(log))
    assert report["requests"] == 0 and report["summary_calls"] == 1
    assert (report["api_input_tokens"], report["api_cached_tokens"], report["api_output_tokens"]) == (30, 20, 7)


def test_incomplete_summary_response_is_rejected():
    with pytest.raises(ValueError):
        response_text(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n')
    with pytest.raises(ValueError):
        response_text(b'{"status":"incomplete","output":[]}')


@pytest.mark.parametrize('status',['in_progress','queued',None])
def test_nonterminal_summary_json_cannot_replace_history(status):
    raw=json.dumps(dict(status=status,output=[dict(type='message',content=[dict(type='output_text',text='partial note')])])).encode()
    with pytest.raises(ValueError,match='did not complete'):
        response_text(raw)


def test_terminal_summary_event_without_redundant_status_remains_supported():
    raw=b'data: {"type":"response.completed","response":{"output":[{"type":"message","content":[{"type":"output_text","text":"finished"}]}]}}\n\n'
    assert response_text(raw)=='finished'


def test_proxy_summaries_use_upstream_model_and_usage(tmp_path):
    got = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            got.append((request, self.headers.get("Authorization")))
            is_summary = "Scope:" in request.get("instructions", "")
            response = dict(status="completed", output=[dict(type="message", content=[dict(type="output_text", text="continuation note")])],
                            usage=dict(input_tokens=10 if is_summary else 100, output_tokens=5,
                                       input_tokens_details=dict(cached_tokens=0, cache_write_tokens=2 if is_summary else 30)))
            payload = ('data: ' + json.dumps(dict(type="response.completed", response=response)) + '\n\n').encode()
            self.send_response(200); self.send_header("Content-Length", str(len(payload))); self.end_headers()
            self.wfile.write(payload)
    upstream = HTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    log = tmp_path / "log.jsonl"
    proxy, _ = serve(lambda: ReSum(k=1), 0, f"http://127.0.0.1:{upstream.server_port}", log=str(log), host="127.0.0.1")
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        for i in range(2):
            data = dict(model="same-model", input=[{"role": "user", "content": "task"},
                                                   {"role": "assistant", "content": "history"}], prompt_cache_key="s")
            req = urllib.request.Request(f"http://127.0.0.1:{proxy.server_port}/responses", data=json.dumps(data).encode(),
                                         headers={"Authorization": "Bearer private-token", "Content-Type": "application/json"})
            with opener.open(req, timeout=10) as response:
                response.read()
    finally:
        proxy.shutdown(); proxy.server_close(); upstream.shutdown(); upstream.server_close()
    assert len(got) == 3 and all(body["model"] == "same-model" and token == "Bearer private-token" for body, token in got)
    assert got[1][0]["tools"] == []
    context = json.loads(got[1][0]["input"][0]["content"][0]["text"])
    assert context["task"] == "task" and context["history"]
    report = summary(str(log))
    assert report["requests"] == 2 and report["summary_calls"] == 1 and report["api_input_tokens"] == 210
    assert report['api_cache_write_tokens'] == 62 and report['missing_api_cache_write_usage'] == 0
    assert "private-token" not in log.read_text(encoding='utf-8')


def test_each_composed_component_supplies_its_own_guidance_and_current_text():
    seen = []
    class Guided:
        def summarize_with_guidance(self, items, purpose, budget, guidance):
            seen.append((purpose, guidance, [item['text'] for item in items]))
            return 'observation note' if purpose == 'observation' else 'history note'
    method = Composed([WithMemory(ACON(t_obs=20, t_hist=100000,
        observation_guidance='select observation evidence')), ReSum(k=1, summary_guidance='select history evidence')])
    ctx = LiveContext(method, summarizer=Guided())
    ctx.add_message('user', 'task')
    ctx.add_call('a', 'cat a.py')
    ctx.add_output('a', 'whole original source\n'*100)
    ctx.before_request(); ctx.before_request()
    assert [row[:2] for row in seen] == [('observation', 'select observation evidence'),
        ('history', 'select history evidence')]
    assert 'whole original source' not in ' '.join(seen[-1][2])
    assert any('observation note' in text for text in seen[-1][2])
    assert ctx.M['summary_guidance_skipped'] == 0


def test_legacy_summary_callback_remains_usable_and_reports_unhandled_guidance():
    rw = Rewriter(lambda: ReSum(k=1), summarizer=lambda items: 'note')
    body = dict(input=[dict(role='user',content='task'),dict(role='assistant',content='history')])
    rw.rewrite_body(body, 's')
    rewritten, info = rw.rewrite_body(body, 's')
    assert info['summary_guidance_skipped'] == 1 and info['summary_failed'] == 0
    assert any('note' in str(item) for item in rewritten['input'])


def test_guided_service_failure_keeps_history_and_never_duplicates_with_legacy_call():
    class Guided:
        def summarize_with_guidance(self, items, **kwargs):
            raise RuntimeError('model failed')
        def summarize(self, *args, **kwargs):
            pytest.fail('failed guided call retried through another interface')
    ctx = LiveContext(ReSum(k=1), summarizer=Guided())
    ctx.add_message('user', 'task'); ctx.add_message('assistant', 'history')
    before = copy.deepcopy(ctx.ctx)
    ctx.before_request(); ctx.before_request()
    assert ctx.ctx == before and ctx.M['summary_failed'] == 1


def test_agentfold_deep_summary_keeps_its_guidance_and_reads_existing_folds():
    seen = []
    class Guided:
        def summarize_with_guidance(self, items, **kwargs):
            seen.append((kwargs, [item['text'] for item in items]))
            return 'combined fold'
    ctx = LiveContext(AgentFold(deep=1, summary_guidance='retain folded evidence'), summarizer=Guided())
    for index in range(2):
        ctx.add_call(str(index), f'cat file{index}.py')
        ctx.add_output(str(index), 'source text'*100)
        ctx.summarize_segment(ctx.ctx[-2:], text=f'existing fold {index}')
    ctx.before_request()
    assert len(seen) == 1 and seen[0][0]['guidance'] == 'retain folded evidence'
    assert seen[0][0]['purpose'] == 'segment' and seen[0][1] == ['existing fold 0', 'existing fold 1']
    assert ctx.ctx[0]['text'] == 'combined fold'


def test_acon_history_uses_its_guidance_and_keeps_user_constraints():
    seen = []
    class Guided:
        def summarize_with_guidance(self, items, **kwargs):
            seen.append(kwargs)
            return 'historical evidence'
    ctx = LiveContext(ACON(t_hist=1, history_guidance='keep essential state'), summarizer=Guided())
    ctx.add_message('user', 'new user constraint')
    ctx.add_message('assistant', 'earlier verified work'*20)
    ctx.before_request()
    assert len(seen) == 1 and seen[0]['purpose'] == 'history' and seen[0]['guidance'] == 'keep essential state'
    assert any(item.get('text') == 'new user constraint' for item in ctx.ctx + ctx.pinned)


@pytest.mark.parametrize('method,kwargs', [(ReSum, {'summary_guidance': ''}),
    (ACON, {'history_guidance': 123}), (ACON, {'observation_guidance': ' '}),
    (AgentFold, {'summary_guidance': []})])
def test_invalid_method_guidance_is_rejected_at_construction(method, kwargs):
    with pytest.raises(ValueError, match='guidance'):
        method(**kwargs)


def test_resum_adapted_prompt_reaches_the_same_model_without_generic_plan_requirements():
    from ctxpress.live.summarize import ResponsesSummarizer
    sent, logs = [], []
    class Response:
        status = 200
        def read(self):
            return json.dumps(dict(status='completed', output=[dict(type='message', content=[
                dict(type='output_text',text='<summary>supported fact</summary>')])],
                usage=dict(input_tokens=20,output_tokens=3))).encode()
    class Connection:
        def request(self, method, path, body, headers):
            sent.append(json.loads(body))
        def getresponse(self):
            return Response()
        def close(self):
            pass
    original = dict(model='same-agent-model', reasoning=dict(effort='medium'),
        instructions='host constraint', input=[dict(role='user',content='task question'),
            dict(role='developer',content='added developer constraint'),dict(role='user',content='later correction')])
    service = ResponsesSummarizer(Connection, '/responses', {}, original, record=logs.append)
    ctx = LiveContext(ReSum(k=1), summarizer=service)
    ctx.add_message('user','task question'); ctx.add_message('assistant','supported fact')
    ctx.before_request(); ctx.before_request()
    assert len(sent) == len(logs) == 1
    prompt = sent[0]['instructions']
    assert RESUM_GUIDANCE in prompt and 'unresolved work' not in prompt
    assert 'Treat the supplied history' in prompt
    assert sent[0]['model'] == original['model'] and sent[0]['reasoning'] == original['reasoning']
    assert sent[0]['tools'] == [] and sent[0]['store'] is False
    payload = json.loads(sent[0]['input'][0]['content'][0]['text'])
    assert payload['task'] == 'task question' and payload['instructions'] == 'host constraint'
    assert payload['constraints'] == ['added developer constraint'] and payload['user_updates']==['later correction']
    assert logs[0]['completed'] is True and logs[0]['usage']['input_tokens'] == 20
    assert 'supported fact' not in json.dumps(logs) and RESUM_GUIDANCE not in json.dumps(logs)
