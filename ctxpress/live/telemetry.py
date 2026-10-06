"""What a session through the proxy did: requests, history sizes, API usage and method activity, from its
request log (the end-of-session line of `ctxpress codex` / `ctxpress claude`, and run results)."""
from __future__ import annotations
import json, os


def summary(log, since=0.0):
    rows = []
    if os.path.exists(log):
        with open(log, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:                     # a terminated request may leave a partial row
                    continue
    rows = [r for r in rows if r.get("t", 0) >= since]
    from ctxpress.live import usage as eval_usage
    from ctxpress.live.control_receipts import route_failures
    observed = rows
    summaries = [r for r in rows if r.get("type") == "summary"]
    compactions = [r for r in rows if r.get("type") == "native_compaction"]
    passthrough = [r for r in rows if r.get("type") == "passthrough"]
    rows = [r for r in rows if "request" in r]
    accounting = eval_usage.analyze(dict(requests=len(rows), rewrites=observed,
                                        usage=dict(summary_calls=len(summaries), native_compaction_calls=len(compactions),
                                                   passthrough_calls=len(passthrough))))
    return dict(method_tool_route_failures=len(route_failures(dict(rewrites=observed))),
                requests=len(rows), history_tokens_before=sum(r["tokens_before"] for r in rows),
                history_tokens_after=sum(r["tokens_after"] for r in rows), outputs_changed_last=rows[-1]["changed"] if rows else 0,
                api_input_tokens=accounting['api_input_tokens'], api_cached_tokens=accounting['api_cached_tokens'],
                api_cache_write_tokens=accounting['api_cache_write_tokens'],
                missing_api_cache_write_usage=accounting['missing_api_cache_write_usage'], log=log, summary_calls=len(summaries),
                native_compaction_calls=len(compactions), passthrough_calls=len(passthrough),
                native_compaction_blocked=sum(r.get('type') == 'native_compaction_blocked' for r in observed),
                api_output_tokens=accounting['api_output_tokens'],
                pruner_calls=sum(r.get("pruner_calls", 0) for r in rows),
                pruner_input_tokens=sum(r.get("pruner_input_tokens", 0) for r in rows),
                pruner_usage_unknown=sum(r.get("pruner_usage_unknown", 0) for r in rows),
                method_overhead_estimate=sum(r.get("method_overhead_estimate", 0) for r in rows),
                summary_input_tokens=sum((r.get("usage") or {}).get("input_tokens") or 0 for r in summaries))
