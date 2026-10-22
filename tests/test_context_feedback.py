import pytest
from ctxpress.core import sizes
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import NoCompaction


def request(writer, model='model-a', session='s'):
    body = dict(model=model, max_output_tokens=1234, instructions='Retain this requirement',
                input=[dict(role='user', content='Read the source')])
    rewritten, info = writer.rewrite_body(body, session)
    info.update(status=200, usage=dict(input_tokens=100), response_model=model)
    return rewritten, info


def counted(body):
    x, n = sizes.features([sizes.parts(i) for i in body['input']])
    return x, n + sizes.fixed_tokens(body)


def test_full_wire_prompt_and_request_output_limit_reach_context():
    writer = Rewriter(NoCompaction)
    body, info = request(writer)
    x, n = counted(body)
    assert info['size_features'] == dict(x=x, counted=n)
    assert info['rendered_prompt_tokens'] == round(sizes.SizeModel().model(x, n))   # cold start: the prior
    assert writer.sessions['s'].ctx.request_output_limit == 1234
    assert writer.observe_response(info)
    assert not writer.observe_response(info)
    assert writer.sessions['s'].ctx.size_models['model-a'].last[2] == 100


def test_missing_usage_and_failed_stream_never_calibrate():
    writer = Rewriter(NoCompaction)
    _, info = request(writer)
    for change in [dict(usage=None), dict(usage={'input_tokens': None}),
                   dict(status=400), dict(stream_error='broken'), dict(usage={'input_tokens': True})]:
        assert not writer.observe_response(dict(info, **change))
    assert all(m.last is None for m in writer.sessions['s'].ctx.size_models.values())


def test_sessions_and_requested_models_have_independent_size_models():
    writer = Rewriter(NoCompaction)
    _, first = request(writer)
    assert writer.observe_response(first)
    _, second = request(writer, model='model-b')
    second['usage']['input_tokens'] = 500
    assert writer.observe_response(second)
    _, other = request(writer, session='other')
    assert writer.observe_response(other)
    one = writer.sessions['s'].ctx.size_models
    two = writer.sessions['other'].ctx.size_models
    assert set(one) == {'model-a', 'model-b'}
    assert set(two) == {'model-a'}
    assert one['model-a'] is not two['model-a']
    _, again = request(writer, model='model-b')                 # unchanged request: what model-b billed
    assert again['rendered_prompt_tokens'] == 500


def test_late_response_uses_its_original_request_features():
    writer = Rewriter(NoCompaction)
    body, old = request(writer)
    newer = dict(model='model-a', input=[dict(role='user', content='longer ' * 1000)])
    writer.rewrite_body(newer, 's')
    assert writer.observe_response(old)
    fit = writer.sessions['s'].ctx.size_models['model-a']
    assert fit.estimate(*counted(body)) == 100


def test_real_proxy_retry_calibrates_successful_retry_body_only(tmp_path):
    from test_overflow import fixture, request as large_request
    from ctxpress.methods import CliffCompaction
    with fixture(CliffCompaction(t=100000), tmp_path/'http.jsonl') as (send, got, writer):
        status, _ = send(large_request())
        assert status == 200 and len(got) == 2
    # Closing fixture waits for the handler to finish delivering usage feedback.
    fit = writer.sessions['default'].ctx.size_models['synthetic-model']
    assert fit.last is not None and fit.last[2] == 20
    assert fit.last[1] == counted(got[1])[1]
    assert counted(got[1])[1] < counted(got[0])[1]
