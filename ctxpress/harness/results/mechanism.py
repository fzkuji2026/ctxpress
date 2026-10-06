"""Describe observed context operations without declaring method fidelity or quality."""
from __future__ import annotations
from collections import Counter
import math
import statistics


def _distribution(values):
    values = list(values)
    known = sorted(value for value in values if type(value) is int and value >= 0)
    return dict(observed_requests=len(known), missing_requests=len(values)-len(known),
                minimum=known[0] if known else None, maximum=known[-1] if known else None,
                mean=statistics.mean(known) if known else None,
                median=statistics.median(known) if known else None,
                p90=known[math.ceil(.9*len(known))-1] if known else None)


def observe(result):
    rows = result.get('rewrites', [])
    successful = [row for row in rows if 'request' in row and type(row.get('status')) is int
                  and 200 <= row['status'] < 300 and not row.get('stream_error')]
    changed = [row for row in successful if row.get('changed', 0) > 0 or row.get('dropped', 0) > 0]
    operations, purposes = Counter(), Counter()
    for row in successful:
        for key, value in (row.get('operations') or {}).items():
            if type(value) is int and value > 0:
                operations[key] += value
    for row in rows:
        if row.get('type') == 'summary' and row.get('completed') is True and row.get('status') == 200:
            purposes[row.get('purpose') or 'unknown'] += 1
    native = [row for row in rows if row.get('type') == 'native_compaction']
    dispatched = [row for row in rows if 'request' in row or row.get('type') == 'native_compaction']
    return dict(successful_requests=len(successful), changed_requests=len(changed),
                input_exposure=dict(
                    api_input_tokens=_distribution((row.get('usage') or {}).get('input_tokens') for row in successful),
                    proxy_tokens_before=_distribution(row.get('tokens_before') for row in successful),
                    proxy_tokens_after=_distribution(row.get('tokens_after') for row in successful),
                    scope='Successful main requests only; API tokens and proxy estimates are distinct. '
                          'P90 uses nearest rank. Missing values are not imputed; exposure is not a method trigger.'),
                rendered_mutation_observed=bool(changed),
                operation_logging_available=bool(successful) and all('operations' in row for row in successful),
                operations_on_successful_requests=dict(sorted(operations.items())),
                completed_summary_purposes=dict(sorted(purposes.items())),
                failed_summaries=sum(row.get('type') == 'summary' and row.get('completed') is False for row in rows),
                native_compaction_logging_available='native_compaction_calls' in (result.get('usage') or {}),
                native_compaction_metadata_coverage=bool(dispatched) and all(
                    row.get('native_compaction_detection') == 'endpoint_and_turn_metadata_v1' for row in dispatched),
                completed_native_compactions=sum(row.get('completed') is True for row in native),
                host_contract_failures=sum(row.get('type') == 'host_contract_failed' for row in rows),
                passthrough_calls=sum(row.get('type') == 'passthrough' for row in rows),
                blocked_native_compactions=sum(row.get('type') == 'native_compaction_blocked' for row in rows),
                scope='observed transport and context changes; no method-fidelity, complete trigger coverage or task-quality verdict')
