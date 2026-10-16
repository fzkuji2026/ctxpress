"""Network-free profile checks; synthetic text is not benchmark evidence."""
import copy
import json
import http.client
import threading
from pathlib import Path

import pytest

from ctxpress.methods import build
from ctxpress.methods.paper_catalog import method_entries
from ctxpress.methods.experiment_context import PROFILE, ContextLimitError, check_body, instructions, role
from ctxpress.live.rewrite import Rewriter


def body(text='task', **extra):
    return dict(model='test-model', input=[dict(type='message', role='user', content=text)], **extra)


def factory(name='AgentFoldSource', **args):
    return lambda: build(
        {'class': name, 'args': dict(args, context_profile=PROFILE)})


LOCAL = {'HostDefault', 'ComplexityTrap', 'AgentDiet', 'SWEPruner', 'TokenPilotLifecycle', 'TACO', 'DTOC'}
EXTRA_MODEL = {'SWEPruner'}


def test_all_profiles_reach_runtime_and_clone_without_losing_limits():
    entries = method_entries()
    assert len(entries) == 18 and entries[0]['class'] == 'HostDefault'
    assert not EXTRA_MODEL & {e['class'] for e in entries}  # left out unless their service is given
    with_service = method_entries('http://127.0.0.1:1/prune')
    assert EXTRA_MODEL <= {e['class'] for e in with_service} and len(with_service) == 19
    for entry in with_service:
        m = copy.deepcopy(build(entry))
        assert m.context_profile == PROFILE
        assert m.codex_config['model_context_window'] == 128000
        assert m.unbounded and m.requires_host_view  # no ctxpress summary fallback
        assert role(m) == ('local' if entry['class'] in LOCAL else 'full')
        # Every method keeps the host's own compaction at its own threshold: the only fallback.
        assert m.allow_native_compaction is True
        assert 'model_auto_compact_token_limit' not in m.codex_config
        assert 'native compaction remains active' in instructions(m)
    # Old author configs have no experimental layer, and no defaults were overwritten.
    assert build({'class': 'ACONSource'}).t_hist == 4096
    assert build({'class': 'FoldActSource'}).max_obs_length == 4096
    assert not hasattr(build({'class': 'CWL'}), 'context_profile')


def history(outputs, size=16000):
    request = body()
    for i in range(outputs):
        request['input'] += [dict(type='function_call', call_id=str(i), name='exec', arguments='{}'),
                             dict(type='function_call_output', call_id=str(i), output='x' * size)]
    return request


def test_over_window_request_is_sent_unchanged_and_recorded():
    request = history(40)
    result, info = Rewriter(factory('HostDefault')).rewrite_body(copy.deepcopy(request), 'session')
    outputs = lambda b: [i for i in b['input'] if i.get('type') == 'function_call_output']
    assert outputs(result) == outputs(request)  # only the method's instructions are added
    row = info['experiment_context']
    assert row['over_limit'] and row['input_tokens_estimate'] > 128000 and 'overflow_fallback' not in row
    assert not check_body(history(20), PROFILE)['over_limit']


def test_maximum_is_decimal_and_counts_fixed_prompt_and_tools():
    request = body(instructions='x' * 20000, tools=[{'name': 'exec', 'description': 'x' * 30000}])
    row = check_body(request, PROFILE)
    assert row['input_tokens_estimate'] > 12500
    result, info = Rewriter(factory()).rewrite_body(request, 'session')
    assert '128000' in json.dumps(result) and '131072' not in json.dumps(result)
    assert info['experiment_context']['max_context_tokens'] == 128000
    _, info = Rewriter(factory()).rewrite_body(body(instructions='x' * (128000 * 4)), 'session')
    assert info['experiment_context']['over_limit']


def test_final_render_is_checked_after_the_method_can_compress():
    request = body()
    for i in range(3):
        request['input'] += [dict(type='function_call', call_id=str(i), name='exec', arguments='{}'),
                             dict(type='function_call_output', call_id=str(i), output='x' * 180000)]
    assert check_body(request, PROFILE)['over_limit']
    _, info = Rewriter(factory('ComplexityTrap', n=1)).rewrite_body(request, 'session')
    assert info['experiment_context']['input_tokens_estimate'] < 102400
    assert info['host_contract']['valid']


@pytest.mark.parametrize('dialect', ['chat', 'anthropic'])
def test_nonresponses_hosts_check_final_tools_and_system(dialect):
    manager = Rewriter(factory())
    request = dict(model='test-model', messages=[dict(role='user', content='task')],
                   tools=[dict(name='exec', description='x' * 520000)])
    if dialect == 'chat':
        from ctxpress.live.chat import rewrite_chat as rewrite
    else:
        from ctxpress.live.anthropic import rewrite_messages as rewrite
    _, info = rewrite(manager, request, 'session')
    assert info['experiment_context']['over_limit']


def test_acon_uses_whole_input_not_just_selected_history():
    from ctxpress.live.context import LiveContext
    entry = next(e for e in method_entries() if e['class'] == 'ACONSource')
    ctx = LiveContext(build(entry))
    for i in range(4):
        ctx.add_message('assistant', 'inspect')
        ctx.add_call(str(i), 'ls')
        ctx.add_output(str(i), 'short result')
    seen = []
    ctx.model_text = lambda *a, **kw: seen.append(kw['purpose']) or '# History Summary\nsummary'
    ctx.measure_history = lambda _: 102399
    ctx.before_request()
    assert not seen
    ctx.measure_history = lambda _: 102400
    ctx.before_request()
    assert seen == ['history']


@pytest.mark.parametrize('path', ['/responses', '/responses/compact'])
def test_proxy_forwards_over_window_requests_unchanged_and_records_them(tmp_path, path):
    from http.server import BaseHTTPRequestHandler
    from ctxpress.live.proxy import ThreadingHTTPServer, make_handler
    received = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            reply = json.dumps({'output': []}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(reply)))
            self.end_headers(); self.wfile.write(reply)

    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    log = tmp_path / 'requests.jsonl'
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(
        Rewriter(factory('HostDefault')), f'http://127.0.0.1:{upstream.server_port}', None, str(log),
        context_profile=PROFILE))
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (upstream, server)]
    for thread in threads:
        thread.start()
    try:
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        connection.request('POST', path, json.dumps(history(40)), {'Content-Type': 'application/json'})
        assert connection.getresponse().status == 200
        connection.close()
    finally:
        for s in (server, upstream):
            s.shutdown(); s.server_close()
    outputs = lambda b: [i for i in b['input'] if i.get('type') == 'function_call_output']
    assert outputs(received[0]) == outputs(history(40))
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    row = next(r for r in rows if 'experiment_context' in r)
    assert row['experiment_context']['over_limit'] and 'overflow_fallback' not in row['experiment_context']


@pytest.mark.parametrize('profile', [dict(PROFILE, max_context_tokens=True),
                                    dict(PROFILE, compression_trigger_tokens=128000),
                                    dict(PROFILE, typo=128000)])
def test_bad_profile_fails_before_launch(profile):
    with pytest.raises(ValueError):
        build({'class': 'DTOC', 'args': {'context_profile': profile}})


@pytest.mark.parametrize('dialect', ['responses', 'chat', 'anthropic'])
def test_auxiliary_calls_cannot_use_a_larger_window(dialect):
    if dialect == 'responses':
        from ctxpress.live.summarize import ResponsesSummarizer as Client
    elif dialect == 'chat':
        from ctxpress.live.chat import ChatSummarizer as Client
    else:
        from ctxpress.live.anthropic import MessagesSummarizer as Client
    def no_connect():
        raise AssertionError('oversized auxiliary input must not reach upstream')
    rows = []
    client = Client(no_connect, '/test', {}, {'model': 'test'}, rows.append)
    client.context_profile = PROFILE
    with pytest.raises(ContextLimitError):
        client.summarize_prompt('', 'x' * 520000)
    assert rows[-1]['caller'] == 'summary' and rows[-1]['upstream_sent'] is False


def test_provider_conformance_is_separate_from_estimates():
    manager = Rewriter(factory())
    _, info = manager.rewrite_body(body(), 's')
    info.update(status=200, usage=dict(input_tokens=128001), response_model='test-model')
    manager.observe_response(info)
    assert info['experiment_context_provider_check']['within_limit'] is False
    assert info['experiment_context']['input_tokens_estimate'] < 128000


def test_profile_rewrite_failure_does_not_forward_original_request(tmp_path):
    from ctxpress.methods.base import Method
    from ctxpress.methods.experiment_context import attach
    from ctxpress.live.proxy import ThreadingHTTPServer, make_handler
    class Broken(Method):
        def step(self, sim, r):
            raise ValueError('synthetic method failure')
    log = tmp_path / 'requests.jsonl'
    server = ThreadingHTTPServer(('127.0.0.1', 0),
        make_handler(Rewriter(lambda: attach(Broken(), PROFILE)), 'http://127.0.0.1:1', None, str(log)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        connection.request('POST', '/responses', json.dumps(body()), {'Content-Type': 'application/json'})
        response = connection.getresponse()
        assert response.status == 422
        response.read(); connection.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    row = json.loads(log.read_text().splitlines()[-1])
    assert row['type'] == 'experiment_context_rewrite_failed' and row['upstream_sent'] is False


def test_published_config_matches_generated_profile():
    from pathlib import Path
    from ctxpress.methods.paper_profile import arguments
    document = json.loads((Path(__file__).resolve().parents[1] / 'configs/paper-context.protocol.json').read_text())
    for entry in document['comparison']['methods']:
        expected = arguments(entry['class'])
        if entry['class'] == 'AgentDiet':
            expected['reflect_model'] = document['models']['agentdiet_reflection']
        assert entry['args'] == expected
    assert document['run']['max_calls'] is None and document['run']['timeout'] is None


@pytest.mark.parametrize('window', [32000, 64000, 128000, 256000])
def test_every_token_setting_follows_the_window(window):
    from ctxpress.methods.paper_profile import arguments
    trigger, headroom = round(window * 0.8), window - round(window * 0.8)
    entries = {e['class']: e for e in method_entries(window=window)}
    for entry in entries.values():
        m = build(entry)
        assert m.context_profile == dict(version=1, max_context_tokens=window, compression_trigger_tokens=trigger)
        assert m.codex_config['model_context_window'] == window
    args = lambda name: entries[name]['args']
    assert args('ACONSource')['t_hist'] == args('ACONSource')['trigger_tokens'] == trigger
    assert args('ACONSource')['t_obs'] == args('TokenPilotLifecycle')['entry_budget'] == headroom
    assert args('ContextFoldingTools')['branch_tokens'] == headroom
    assert args('FoldActSource')['max_model_len'] == window and args('FoldActSource')['selection_tokens'] == trigger
    assert args('ReSumSource')['context_k'] == window // 1024 and args('CWL')['budget'] == trigger
    for name in ('ComplexityTrap', 'ComplexityTrapSummary', 'ComplexityTrapHybrid', 'CoMem', 'ReSumSource'):
        assert args(name)['trigger_tokens'] == trigger
    published = json.loads((Path(__file__).resolve().parents[1] / f'configs/paper-context-{window // 1000}k.methods.json').read_text())
    assert published['methods'] == method_entries(window=window)
    assert arguments('ACONSource', window) == args('ACONSource')


@pytest.mark.parametrize('outputs,masked', [(3, False), (12, True)])
def test_window_complexity_trap_masks_only_once_the_input_reaches_the_trigger(outputs, masked):
    request = history(outputs, size=40000)   # 10k tokens each
    result, _ = Rewriter(factory('ComplexityTrap', n=1, trigger_tokens=102400)).rewrite_body(copy.deepcopy(request), 's')
    kept = [i['output'] for i in result['input'] if i.get('type') == 'function_call_output']
    assert (sum('omitted' in text for text in kept) == outputs - 1) is masked
    assert masked or kept == [i['output'] for i in request['input'] if i.get('type') == 'function_call_output']


def test_window_complexity_trap_summary_checkpoints_all_but_latest_m_at_the_trigger():
    from ctxpress.live.context import LiveContext
    calls = []

    class Service:
        def summarize_prompt(self, system, user, purpose, **kw):
            calls.append(purpose)
            return 'checkpoint'

    ctx = LiveContext(build({'class': 'ComplexityTrapSummary', 'args': dict(n=21, m=2, trigger_tokens=102400)}),
                      summarizer=Service())
    for i in range(5):
        ctx.add_message('assistant', 'inspect')
        ctx.add_call(str(i), 'ls')
        ctx.add_output(str(i), 'result')
    ctx.measure_history = lambda _: 102399
    ctx.before_request()
    assert not calls
    ctx.measure_history = lambda _: 102400
    ctx.before_request()
    assert len(calls) == 1
    assert len(ctx.method.turns(ctx)) == 2 and any(s['seg'] == 'summary' for s in ctx.ctx)


def test_author_turn_schedule_is_unchanged_without_a_trigger():
    m = build({'class': 'ComplexityTrapSummary', 'args': dict(n=21, m=10)})
    assert m.trigger_tokens is None and build({'class': 'ComplexityTrap', 'args': dict(n=10)}).trigger_tokens is None


def test_anthropic_capacity_trigger_counts_the_system_prompt():
    from ctxpress.live.anthropic import rewrite_messages
    request = dict(model='test-model', system='s' * 80000, tools=[dict(name='exec', description='run')],
                   messages=[dict(role='user', content=[dict(type='text', text='task')])])
    result, info = rewrite_messages(Rewriter(factory()), request, 'session')
    assert info['rendered_prompt_tokens'] >= 20000  # the measured body includes the 20k-token system prompt
    assert result['system'][0]['text'] == 's' * 80000 and len(result['system']) <= 2  # not duplicated


def test_comparison_reports_window_conformance_beside_the_grade():
    from ctxpress.harness.results.task_compare import _context_window
    row = lambda tokens, **extra: dict(request=1, experiment_context=check_body(body('x' * tokens * 4), PROFILE), **extra)
    clean = _context_window(dict(rewrites=[row(1000), row(2000, experiment_context_provider_check=dict(
        input_tokens=2100, max_context_tokens=128000, within_limit=True))]))
    assert clean['conformant'] and clean['over_limit_requests'] == 0 and clean['provider_checked_requests'] == 1
    over = _context_window(dict(rewrites=[row(1000), dict(row(140000), type='native_compaction')]))
    assert not over['conformant'] and over['over_limit_requests'] == 1 and over['over_limit_native_compactions'] == 1
    assert _context_window(dict(rewrites=[dict(request=1)])) is None  # no declared window
