from ctxpress.harness.results import timing


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
