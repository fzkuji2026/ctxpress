"""Network-free profile checks; synthetic text is not benchmark evidence."""
import copy
import json
import http.client
import threading

import pytest

from ctxpress.methods import build
from ctxpress.methods.paper_catalog import method_entries
from ctxpress.methods.experiment_context import PROFILE, ContextLimitError, check_body
from ctxpress.live.rewrite import Rewriter


def body(text='task', **extra):
    return dict(model='test-model', input=[dict(type='message', role='user', content=text)], **extra)


def factory(name='AgentFoldSource', **args):
    return lambda: build(
        {'class': name, 'args': dict(args, context_profile=PROFILE)})


def test_all_eighteen_profiles_reach_runtime_and_clone_without_losing_limits():
    entries = method_entries('http://127.0.0.1:1/prune')
    assert len(entries) == 18
    for entry in entries:
        m = copy.deepcopy(build(entry))
        assert m.context_profile == PROFILE
        assert m.codex_config['model_context_window'] == 128000
        assert m.unbounded and not m.allow_native_compaction  # no hidden fallback
        assert m.requires_host_view
    # Old author configs have no experimental layer, and no defaults were overwritten.
    assert build({'class': 'ACONSource'}).t_hist == 4096
    assert build({'class': 'FoldActSource'}).max_obs_length == 4096
    assert not hasattr(build({'class': 'CWL'}), 'context_profile')


def test_maximum_is_decimal_and_counts_fixed_prompt_and_tools():
    request = body(instructions='x' * 20000, tools=[{'name': 'exec', 'description': 'x' * 30000}])
    row = check_body(request, PROFILE)
    assert row['input_tokens_estimate'] > 12500
    result, info = Rewriter(factory()).rewrite_body(request, 'session')
    assert '128000' in json.dumps(result) and '131072' not in json.dumps(result)
    assert info['experiment_context']['max_context_tokens'] == 128000
    with pytest.raises(ContextLimitError):
        Rewriter(factory()).rewrite_body(body(instructions='x' * (128000 * 4)), 'session')


def test_final_render_is_checked_after_the_method_can_compress():
    request = body()
    for i in range(3):
        request['input'] += [dict(type='function_call', call_id=str(i), name='exec', arguments='{}'),
                             dict(type='function_call_output', call_id=str(i), output='x' * 180000)]
    with pytest.raises(ContextLimitError):
        check_body(request, PROFILE)
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
    with pytest.raises(ContextLimitError):
        rewrite(manager, request, 'session')


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


def test_explicit_context_limit_error_cannot_fall_back_to_forwarding(tmp_path):
    from ctxpress.live.proxy import ThreadingHTTPServer, make_handler
    log = tmp_path / 'requests.jsonl'
    server = ThreadingHTTPServer(('127.0.0.1', 0),
        make_handler(Rewriter(factory()), 'http://127.0.0.1:1', None, str(log)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        connection.request('POST', '/responses', json.dumps(body('x' * 520000)),
                           {'Content-Type': 'application/json'})
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())['error']['code'] == 'ctxpress_experiment_context_limit'
        connection.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    row = json.loads(log.read_text().splitlines()[-1])
    assert row['type'] == 'experiment_context_limit' and row['upstream_sent'] is False


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
