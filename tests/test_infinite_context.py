import copy
import json

import pytest

from ctxpress.live.context import LiveContext
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods.infinite_rules import InfiniteContextRules as InfiniteContext
from ctxpress.methods import build


class Predictor:
    def __init__(self, invalid=None, loss=0, extra=0):
        self.calls, self.invalid, self.loss, self.extra = [], invalid, loss, extra

    def summarize_prompt(self, system, user, purpose, model):
        data = json.loads(user)
        self.calls.append((purpose, model, data))
        if self.invalid is not None:
            return self.invalid
        return json.dumps(dict(horizon=64, predictions=[dict(id=c['id'],
            extra_requests=self.extra, quality_loss=self.loss) for c in data['candidates']]))


def context(method, predictor=None):
    ctx = LiveContext(method, summarizer=predictor)
    ctx.add_message('user', 'Implement the required API exactly')
    ctx.add_call('spec', 'cat docs/SPEC.md')
    ctx.add_output('spec', 'EXACT_API_NAME\n' * 100)
    for i in range(12):
        ctx.add_message('assistant', f'Working on file {i}')
        ctx.add_call(f'c{i}', f'cat src/file{i}.py')
        ctx.add_output(f'c{i}', f'SOURCE_{i}\n' * 500)
    return ctx


def method(**kwargs):
    return InfiniteContext(t=5000, target=2500, emergency=100000, **kwargs)


def test_spec_pair_user_and_recent_work_survive_and_shrink():
    m = method()
    ctx = context(m)
    before = ctx.request_chars()
    view = ctx.before_request()
    assert ctx.request_chars() < before
    assert any(s.get('call_id') == 'spec' and s['seg'] == 'call' for s in view)
    assert any('EXACT_API_NAME' in s['text'] for s in view)
    assert any('SOURCE_11' in s['text'] for s in view)
    assert ctx.M['infinite_context_compaction'] == 1
    assert not ctx.M['summary_skipped']


def test_cooldown_leaves_prefix_unchanged():
    ctx = context(method(cooldown=8))
    ctx.before_request()
    first = copy.deepcopy(ctx.sent())
    ctx.before_request()
    assert ctx.sent() == first
    assert ctx.M['infinite_context_compaction'] == 1


def test_temporary_protection_expires_but_external_protection_remains():
    m = method(work_turns=1, cooldown=0)
    ctx = context(m)
    old = next(s for s in ctx.ctx if s.get('call_id') == 'c11' and s['seg'] == 'out')
    external = next(s for s in ctx.ctx if s.get('call_id') == 'c0')
    ctx.protect([external])
    ctx.before_request()
    assert not old.get('protected') and external['protected']
    for i in range(12, 25):
        ctx.add_message('assistant', 'next')
        ctx.add_call(f'c{i}', f'cat src/file{i}.py')
        ctx.add_output(f'c{i}', 'x' * 3000)
    ctx.before_request()
    assert old not in ctx.ctx and external in ctx.ctx


@pytest.mark.parametrize('invalid', ['not json', '{}', '[]',
    '{"horizon":true,"predictions":[]}', '{"horizon":8,"predictions":[{"id":"invented"}]}'])
def test_invalid_predictor_keeps_history_without_repeated_calls(invalid):
    p = Predictor(invalid=invalid)
    m = method(predictor_mode='control', predictor_model='explicit-test-model')
    ctx = context(m, p)
    before = [s['id'] for s in ctx.sent()]
    ctx.before_request(); ctx.before_request()
    assert [s['id'] for s in ctx.sent()] == before
    assert len(p.calls) == 1 and ctx.M['infinite_context_prediction_invalid'] == 1


def test_high_risk_prediction_rejects_compaction():
    p = Predictor(loss=1)
    m = method(predictor_mode='control', predictor_model='explicit-test-model')
    ctx = context(m, p); ctx.before_request()
    assert m.last_decision['selected'] == 'keep'
    assert not ctx.M['infinite_context_compaction']


@pytest.mark.parametrize('loss', [float('nan'), float('inf'), -0.1, True])
def test_invalid_numeric_prediction_cannot_commit(loss):
    ctx = context(method(predictor_mode='control', predictor_model='test'), Predictor(loss=loss))
    before = ctx.request_chars()
    ctx.before_request()
    assert ctx.request_chars() == before
    assert ctx.M['infinite_context_prediction_invalid'] == 1


def test_predictor_timeout_keeps_history_and_respects_cooldown():
    class Unavailable:
        calls = 0

        def summarize_prompt(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError('test service timeout')

    predictor = Unavailable()
    ctx = context(method(predictor_mode='control', predictor_model='test'), predictor)
    before = ctx.request_chars()
    ctx.before_request()
    ctx.before_request()
    assert ctx.request_chars() == before
    assert predictor.calls == 1 and ctx.M['summary_failed'] == 1


def test_valid_prediction_uses_separate_model_and_bounded_evidence():
    p = Predictor()
    m = method(predictor_mode='control', predictor_model='explicit-test-model', evidence_chars=800)
    ctx = context(m, p); ctx.before_request()
    assert ctx.M['infinite_context_compaction'] == 1
    purpose, model_name, payload = p.calls[0]
    assert (purpose, model_name) == ('compaction_prediction', 'explicit-test-model')
    assert sum(len(s['text']) for s in payload['evidence']) <= 800


def test_shadow_prediction_does_not_change_policy():
    a, b = context(method()), context(method(predictor_mode='shadow', predictor_model='test'), Predictor(loss=1))
    assert a.before_request() == b.before_request()


def test_overflow_bypasses_predictor_and_cooldown_but_protects_specs():
    p = Predictor(loss=1)
    m = method(predictor_mode='control', predictor_model='test')
    ctx = context(m, p); ctx.before_request()
    assert m.on_overflow(ctx, {})
    assert len(p.calls) == 1
    assert any(s.get('call_id') == 'spec' and s['seg'] == 'out' for s in ctx.sent())


def test_wire_rewrite_preserves_tool_catalog_pairs_and_append_stability():
    rw = Rewriter(lambda: method())
    inputs = [{'role': 'user', 'content': 'task'}]
    for i in range(12):
        inputs += [dict(type='function_call', call_id=str(i), name='exec', arguments='cat docs/SPEC.md' if i == 0 else f'cat f{i}.py'),
                   dict(type='function_call_output', call_id=str(i), output='data\n' * 1000)]
    request = dict(model='test', input=inputs, tools=[dict(type='function', name='exec', parameters=dict(type='object'))])
    first, info = rw.rewrite_body(copy.deepcopy(request), 's')
    second, later = rw.rewrite_body(copy.deepcopy(request), 's')
    assert info['method_decision']['selected'].startswith('recent_')
    assert 'method_decision' not in later
    assert first == second and first['tools'] == request['tools']
    assert inputs[1] in first['input'] and inputs[2] in first['input']
    calls = {x['call_id'] for x in first['input'] if x.get('type') == 'function_call'}
    outputs = {x['call_id'] for x in first['input'] if x.get('type') == 'function_call_output'}
    assert calls == outputs


def test_configuration_requires_explicit_model_and_reset_clears_state():
    with pytest.raises(ValueError):
        InfiniteContext(predictor_mode='control')
    with pytest.raises(ValueError):
        InfiniteContext(target=100000)
    m = build({'class': 'infinite-context-rules'})
    m.last_attempt = 100
    m.reset(None)
    assert m.last_attempt is None and m.last_decision is None
