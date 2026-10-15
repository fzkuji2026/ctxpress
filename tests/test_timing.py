from ctxpress.harness.results import timing
from ctxpress.live import usage


def test_failed_requests_without_usage_are_counted_apart_not_missing():
    ok = dict(input_tokens=100, cached_tokens=40, output_tokens=10)
    rows = [dict(request=1, model='m', status=200, usage=ok),
            dict(request=2, model='m', upstream_error='SSLZeroReturnError'),
            dict(request=3, model='m', status=200, stream_error='IncompleteRead'),
            dict(request=4, model='m', status=200, usage=ok)]
    out = usage.analyze(dict(requests=4, usage=dict(summary_calls=0), rewrites=rows), 'm')
    assert out['failed_requests'] == 2 and out['missing_api_usage'] == 0 and out['complete']
    assert out['api_input_tokens'] == 200
    assert out['usage_by_model']['m']['requests'] == 2 and out['usage_by_model']['m']['failed_requests'] == 2
    assert usage.combine([out, out], 'm')['usage_by_model']['m']['failed_requests'] == 4
    rows[1]['usage'] = ok                          # a failed request that did report usage stays billed
    assert usage.analyze(dict(requests=4, usage=dict(summary_calls=0), rewrites=rows), 'm')['api_input_tokens'] == 300


def test_failed_requests_and_retry_waits_are_removed():
    rows = [dict(request=1, t=0.0, seconds=10.0, status=200),
            dict(request=2, t=20.0, seconds=5.0, status=200, stream_error='connection reset'),
            dict(request=3, t=33.0, seconds=8.0, status=200),
            dict(request=4, t=50.0, seconds=2.0, status=503),
            dict(request=5, t=1000.0, seconds=4.0, status=200),
            dict(type='summary', t=60.0, seconds=3.0, status=200, upstream_error='timeout')]
    out = timing.observe(dict(seconds=1100.0, rewrites=rows))
    assert out['failed_requests'] == 3
    assert out['failed_request_seconds'] == 10.0               # 5 + 2 + 3
    assert out['retry_wait_seconds'] == 8.0 + timing.RETRY_WAIT_CAP
    assert out['retry_waits_capped'] == 1
    assert out['effective_seconds'] == 1100.0 - 10.0 - 8.0 - timing.RETRY_WAIT_CAP


def test_clean_run_keeps_agent_time_and_missing_stays_missing():
    rows = [dict(request=1, t=0.0, seconds=10.0, status=200)]
    assert timing.observe(dict(seconds=30.0, rewrites=rows))['effective_seconds'] == 30.0
    assert timing.observe(dict(rewrites=rows))['effective_seconds'] is None
