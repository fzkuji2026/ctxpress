"""Agent time with failed transport removed: what network and provider errors cost, kept apart from the method.

A failed model request (connection dropped, stream cut, 5xx, failed summary call) gives the host nothing to act
on, so the time until its next model request is a retry wait, not tool or method work. Both are subtracted:

    effective_seconds = agent_seconds - failed_request_seconds - retry_wait_seconds

Waits are capped so a long idle gap after a failure is never attributed to it silently; the cap is reported.
"""
from __future__ import annotations

RETRY_WAIT_CAP = 600.0
REQUEST_TYPES = (None, 'native_compaction', 'passthrough')


def failed(row):
    if row.get('upstream_error') or row.get('stream_error') or row.get('error'):
        return True
    status = row.get('status')
    return status is not None and status != 200


def observe(result):
    agent = result.get('seconds')
    rows = [row for row in result.get('rewrites') or [] if isinstance(row, dict)
            and type(row.get('seconds')) in (int, float) and type(row.get('t')) in (int, float)]
    rows.sort(key=lambda row: row['t'])
    requests = [row for row in rows if row.get('type') in REQUEST_TYPES]
    lost = wait = 0.0
    count = capped = 0
    for row in rows:
        if not failed(row):
            continue
        count += 1
        lost += row['seconds']
        if row.get('type') not in REQUEST_TYPES:
            continue                       # a failed summary call runs inside the next request's rewrite
        end = row['t'] + row['seconds']
        later = next((r['t'] for r in requests if r['t'] >= end), None)
        if later is not None:
            gap = later - end
            capped += gap > RETRY_WAIT_CAP
            wait += min(gap, RETRY_WAIT_CAP)
    effective = None if type(agent) not in (int, float) else max(0.0, agent - lost - wait)
    return dict(agent_seconds=agent, failed_requests=count, failed_request_seconds=round(lost, 1),
                retry_wait_seconds=round(wait, 1), retry_waits_capped=capped,
                effective_seconds=None if effective is None else round(effective, 1),
                scope='agent time minus failed model requests and the wait before the next request; '
                      'grading, container setup and harness recovery outside requests are not included')
