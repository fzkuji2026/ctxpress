import pytest
from ctxpress.core.hostsize import billable_chars
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import NoCompaction


def request(writer, model='model-a', session='s'):
    body = dict(model=model, max_output_tokens=1234, instructions='Retain this requirement',
                input=[dict(role='user', content='Read the source')])
    rewritten, info = writer.rewrite_body(body, session)
    info.update(status=200, usage=dict(input_tokens=100), response_model=model)
    return rewritten, info


def test_full_wire_prompt_and_request_output_limit_reach_context():
    writer = Rewriter(NoCompaction)
    body, info = request(writer)
    assert info['rendered_prompt_tokens'] == billable_chars(body)/4
    assert writer.sessions['s'].ctx.request_output_limit == 1234
    assert writer.observe_response(info)
    assert not writer.observe_response(info)
    assert writer.sessions['s'].ctx.token_calibrations['model-a'].estimate(100)['samples'] == 1


def test_missing_usage_and_failed_stream_never_calibrate():
    writer = Rewriter(NoCompaction)
    _, info = request(writer)
    for change in [dict(usage=None), dict(usage={'input_tokens': None}),
                   dict(status=400), dict(stream_error='broken'), dict(usage={'input_tokens': True})]:
        assert not writer.observe_response(dict(info, **change))
    assert not hasattr(writer.sessions['s'].ctx, 'token_calibrations')


def test_sessions_and_served_models_have_independent_calibration():
    writer = Rewriter(NoCompaction)
    _, first = request(writer)
    assert writer.observe_response(first)
    _, second = request(writer, model='model-b')
    second['usage']['input_tokens'] = 500
    assert writer.observe_response(second)
    _, other = request(writer, session='other')
    assert writer.observe_response(other)
    one = writer.sessions['s'].ctx.token_calibrations
    two = writer.sessions['other'].ctx.token_calibrations
    assert set(one) == {'model-a', 'model-b'}
    assert set(two) == {'model-a'}
    assert one['model-a'] is not two['model-a']
    assert one['model-b'].estimate(100)['tokens'] > one['model-a'].estimate(100)['tokens']


def test_late_response_uses_its_original_sized_prompt():
    writer = Rewriter(NoCompaction)
    _, old = request(writer)
    newer = dict(model='model-a', input=[dict(role='user', content='longer '*1000)])
    writer.rewrite_body(newer, 's')
    assert writer.observe_response(old)
    ratio = writer.sessions['s'].ctx.token_calibrations['model-a'].estimate(100)['ratio']
    assert ratio == 100/old['rendered_prompt_tokens']


def test_real_proxy_retry_calibrates_successful_retry_body_only(tmp_path):
    from test_overflow import fixture, request as large_request
    from ctxpress.methods import CliffCompaction
    with fixture(CliffCompaction(t=100000), tmp_path/'http.jsonl') as (send, got, writer):
        status, _ = send(large_request())
        assert status == 200 and len(got) == 2
    # Closing fixture waits for the handler to finish delivering usage feedback.
    calibration = writer.sessions['default'].ctx.token_calibrations['synthetic-model']
    estimate = calibration.estimate(100)
    assert estimate['samples'] == 1
    assert estimate['ratio'] == pytest.approx(20/(billable_chars(got[1])/4))
    assert billable_chars(got[1]) < billable_chars(got[0])
