"""Observed request usage and model-specific rates; incomplete bills stay unknown."""
from __future__ import annotations
import math

TOKEN_FIELDS = ('api_input_tokens', 'api_cached_tokens', 'api_output_tokens')
COUNT_FIELDS = ('requests', 'main_requests', 'summary_calls', 'native_compaction_calls', 'passthrough_calls',
                'missing_api_usage', 'missing_response_model_requests')
# Billed model calls by role: the agent's rewritten requests, method summaries/reflections, the host's native
# compaction, and model calls the proxy forwarded unchanged (host side calls such as titles or helper requests).
ROW_TYPES = ('summary', 'native_compaction', 'passthrough')
ROLES = ('main', 'summary', 'native_compaction', 'passthrough')


def role(row):
    return 'main' if 'request' in row else row.get('type')
CACHE_WRITE_FIELD = 'api_cache_write_tokens'
CACHE_WRITE_MISSING = 'missing_api_cache_write_usage'
RATE_FIELDS = ('input', 'cached', 'cache_write', 'output')


def _count(value):
    return type(value) is int and value >= 0


def validate_prices(prices):
    """Legacy rates apply only to the main model; a models map declares each model."""
    if prices is None:
        return
    if not isinstance(prices, dict):
        raise ValueError('prices must be an object')
    if set(prices) == {'models'}:
        models = prices['models']
        if not isinstance(models, dict) or not models or any(not isinstance(m, str) or not m.strip() for m in models):
            raise ValueError('prices.models requires nonempty model IDs and rate objects')
        for rates in models.values():
            _validate_rates(rates)
    else:
        _validate_rates(prices)


def _validate_rates(rates):
    """Optional cache_write is USD/1M; long_context declares full-request rates.

    long_context = {input_tokens_threshold: int >= 0, input: number,
                    cached: number, cache_write: number, output: number}
    Its four rates use the parent's unit/source/as_of and apply only when a
    request's input_tokens is strictly greater than the threshold. No model ID
    implies prices or tiers. Declaring long_context also requires cache_write
    in the base rates. Legacy six-key static rates retain their limited scope.
    """
    required = {'input', 'cached', 'output', 'unit', 'source', 'as_of'}
    if (not isinstance(rates, dict) or not required <= set(rates) or
            set(rates) - required - {'cache_write', 'long_context'} or
            rates['unit'] != 'USD_per_million_tokens'):
        raise ValueError('prices require input/cached/output, USD_per_million_tokens, source and as_of')
    _validate_numbers(rates)
    if any(not isinstance(rates[key], str) or not rates[key].strip() for key in ('source', 'as_of')):
        raise ValueError('declared prices need a source and observation date')
    if 'long_context' in rates:
        tier = rates['long_context']
        if ('cache_write' not in rates or not isinstance(tier, dict) or
                set(tier) != set(RATE_FIELDS) | {'input_tokens_threshold'} or
                not _count(tier['input_tokens_threshold'])):
            raise ValueError('long_context requires input_tokens_threshold and four rates, plus base cache_write')
        _validate_numbers(tier)


def _validate_numbers(rates):
    for key in RATE_FIELDS:
        if key in rates and (isinstance(rates[key], bool) or not isinstance(rates[key], (int, float)) or
                             not math.isfinite(rates[key]) or rates[key] < 0):
            raise ValueError('invalid declared price')


def _tokens(usage):
    usage = usage if isinstance(usage, dict) else {}
    return {key: usage.get(key) for key in ('input_tokens', 'cached_tokens', 'cache_write_tokens', 'output_tokens')}


def _valid_write(values):
    return (all(_count(values[key]) for key in ('input_tokens', 'cached_tokens', 'cache_write_tokens')) and
            values['cached_tokens'] + values['cache_write_tokens'] <= values['input_tokens'])


def _valid_usage(values):
    base = all(_count(values[key]) for key in ('input_tokens', 'cached_tokens', 'output_tokens'))
    return (base and values['cached_tokens'] <= values['input_tokens'] and
            (values['cache_write_tokens'] is None or _valid_write(values)))


def _sum_known(values):
    values = list(values)
    return sum(values) if all(_count(value) for value in values) else None


def _bucket():
    return dict.fromkeys(TOKEN_FIELDS + COUNT_FIELDS + (CACHE_WRITE_FIELD, CACHE_WRITE_MISSING), 0) | {
        'response_models': {}, '_request_usage': []}


def _public_bucket(values):
    result = {key: value for key, value in values.items() if key != '_request_usage'}
    # Old aggregate objects have no writer observation either.
    result.setdefault(CACHE_WRITE_FIELD, None if values['requests'] else 0)
    result.setdefault(CACHE_WRITE_MISSING, values['requests'] if result[CACHE_WRITE_FIELD] is None else 0)
    result.setdefault('missing_response_model_requests', None if values['requests'] else 0)
    return result


def analyze(result, default_model=None):
    rows = [row for row in result.get('rewrites',[]) if 'request' in row or row.get('type') in ROW_TYPES]
    main = sum('request' in row for row in rows)
    summaries = sum(row.get('type')=='summary' for row in rows)
    compactions = sum(row.get('type')=='native_compaction' for row in rows)
    passthrough = sum(row.get('type')=='passthrough' for row in rows)
    telemetry = result.get('usage') or {}
    totals = dict.fromkeys(TOKEN_FIELDS + (CACHE_WRITE_FIELD, CACHE_WRITE_MISSING), 0)
    by_model, unknown = {}, _bucket()
    missing, conflicts, missing_models, legacy = 0, [], 0, 0
    missing_returned, observed_returned = 0, 0
    main_model = default_model or result.get('model')
    for row in rows:
        values = _tokens(row.get('usage'))
        valid = _valid_usage(values)
        missing += not valid
        model = row.get('model')
        # Earlier main/native logs predate per-request IDs. Their run metadata can
        # identify the main model; an unidentified reflection is never assigned it.
        if 'model' not in row and row.get('type') != 'summary':
            model = result.get('model') or main_model
            legacy += bool(model)
        if not isinstance(model, str) or not model.strip():
            model = None
            missing_models += 1
        bucket = by_model.setdefault(model, _bucket()) if model else unknown
        bucket['requests'] += 1
        bucket['main_requests'] += 'request' in row
        bucket['summary_calls'] += row.get('type') == 'summary'
        bucket['native_compaction_calls'] += row.get('type') == 'native_compaction'
        bucket['passthrough_calls'] += row.get('type') == 'passthrough'
        bucket['missing_api_usage'] += not valid
        bucket['_request_usage'].append(dict(values, role=role(row)))
        write = values['cache_write_tokens'] if _valid_write(values) else None
        for target in (totals, bucket):
            target[CACHE_WRITE_FIELD] = _sum_known((target[CACHE_WRITE_FIELD], write))
            target[CACHE_WRITE_MISSING] += write is None
        response_model = row.get('response_model')
        if isinstance(response_model, str) and response_model.strip():
            bucket['response_models'][response_model] = bucket['response_models'].get(response_model, 0) + 1
            observed_returned += 1
        else:
            bucket['missing_response_model_requests'] += 1
            missing_returned += 1
        for key,value in values.items():
            if key in ('cache_write_tokens', 'role'):
                continue
            if _count(value) and (key!='cached_tokens' or not _count(values['input_tokens']) or value<=values['input_tokens']):
                totals['api_'+key] += value
                bucket['api_'+key] += value
    # Logs written before pass-through calls were recorded declare no count; their rows are the observation.
    for name,expected,observed in (('requests',result.get('requests'),main),('summary_calls',telemetry.get('summary_calls'),summaries),
                                   ('native_compaction_calls',telemetry.get('native_compaction_calls',0),compactions),
                                   ('passthrough_calls',telemetry.get('passthrough_calls',passthrough),passthrough)):
        if not _count(expected):
            conflicts.append('missing or invalid '+name)
        elif expected < observed:
            conflicts.append(name+' count disagrees with request records')
        else:
            missing += expected-observed
            missing_returned += expected-observed
            if expected > observed:
                totals[CACHE_WRITE_FIELD] = None
                totals[CACHE_WRITE_MISSING] += expected-observed
    for key,value in totals.items():
        if key in telemetry and (telemetry[key] != value or
                                 (value is not None and not _count(telemetry[key]))):
            conflicts.append(key+' total disagrees with request records')
    return dict(**totals,summary_calls=max(summaries,telemetry.get('summary_calls',0) if _count(telemetry.get('summary_calls')) else 0),
                native_compaction_calls=max(compactions,telemetry.get('native_compaction_calls',0) if _count(telemetry.get('native_compaction_calls',0)) else 0),
                passthrough_calls=max(passthrough,telemetry.get('passthrough_calls',0) if _count(telemetry.get('passthrough_calls',0)) else 0),
                missing_api_usage=missing,usage_unobserved_runs=int(not main),usage_conflicts=conflicts,
                complete=bool(main) and not missing and not conflicts, main_model=main_model,
                usage_by_model=by_model, unattributed_usage=unknown, missing_model_requests=missing_models,
                legacy_model_assignments=legacy, model_usage_complete=not missing_models,
                observed_response_model_requests=observed_returned,
                missing_response_model_requests=missing_returned,
                response_model_observation_complete=bool(rows) and not missing_returned and not conflicts)


def combine(records, main_model=None):
    """Aggregate runs without losing model identities or incomplete observations."""
    records = list(records)
    result = dict(main_model=main_model, usage_by_model={}, unattributed_usage=_bucket(),
                  complete=bool(records) and all(r['complete'] for r in records),
                  model_usage_complete=all(r['model_usage_complete'] for r in records))
    for key in TOKEN_FIELDS + ('missing_model_requests', 'legacy_model_assignments'):
        result[key] = sum(r[key] for r in records)
    result[CACHE_WRITE_FIELD] = _sum_known(r[CACHE_WRITE_FIELD] for r in records)
    result[CACHE_WRITE_MISSING] = sum(r[CACHE_WRITE_MISSING] for r in records)
    for key in ('observed_response_model_requests', 'missing_response_model_requests'):
        result[key] = _sum_known(r.get(key) for r in records)
    result['response_model_observation_complete'] = bool(records) and all(
        r.get('response_model_observation_complete') is True for r in records)
    for record in records:
        for model, source in list(record['usage_by_model'].items()) + [(None, record['unattributed_usage'])]:
            target = result['usage_by_model'].setdefault(model, _bucket()) if model else result['unattributed_usage']
            for key in TOKEN_FIELDS + COUNT_FIELDS + (CACHE_WRITE_MISSING,):
                # Records from before pass-through calls were logged carry no count for them.
                target[key] = _sum_known((target[key], source.get(key, 0 if key == 'passthrough_calls' else None)))
            target[CACHE_WRITE_FIELD] = _sum_known((target[CACHE_WRITE_FIELD], source[CACHE_WRITE_FIELD]))
            target['_request_usage'].extend(source['_request_usage'])
            for returned, count in source['response_models'].items():
                target['response_models'][returned] = target['response_models'].get(returned, 0) + count
    return result


def bill(usage, prices):
    """Counterfactual API estimate per request/model, not a ChatGPT invoice.

    Input partitions into ordinary input, cache reads, and cache writes: writes
    replace the ordinary-input rate rather than adding a surcharge. With an
    explicit write rate, missing writes make the estimate unknown. Legacy
    static rates can price old logs, but cannot price observed positive writes.
    """
    validate_prices(prices)
    if prices is None:
        rates = {}
    elif 'models' in prices:
        rates = prices['models']
    else:
        rates = {usage.get('main_model'): prices}
    models, missing_rates, missing_fields = {}, [], {}
    by_role = dict.fromkeys(ROLES, 0.0)          # None once any request of that role cannot be priced
    def charge(kind, value):
        if kind in by_role:
            by_role[kind] = None if value is None or by_role[kind] is None else by_role[kind] + value
    for model, values in usage.get('usage_by_model', {}).items():
        rate, cost, fields = rates.get(model), None, set()
        long_requests = None
        if rate is None or values['missing_api_usage'] or values.get('_request_usage') is None:
            for tokens in values.get('_request_usage') or [dict(role=kind) for kind in ROLES]:
                charge(tokens.get('role'), None)
        if rate is None:
            missing_rates.append(model)
        elif not values['missing_api_usage']:
            requests = values.get('_request_usage')
            if requests is None:
                # Older aggregate objects are sufficient only for static rates.
                requests = [_tokens({key: values.get('api_' + key) for key in
                                     ('input_tokens', 'cached_tokens', 'cache_write_tokens', 'output_tokens')})]
                if 'long_context' in rate:
                    fields.add('per_request_usage')
            costs, tiers = [], []
            for tokens in requests:
                selected = rate
                tier = rate.get('long_context')
                is_long = bool(tier and _count(tokens['input_tokens']) and
                               tokens['input_tokens'] > tier['input_tokens_threshold'])
                if is_long:
                    selected = tier
                tiers.append(is_long)
                value, absent = _request_cost(tokens, selected)
                costs.append(value)
                fields.update(absent)
                if '_request_usage' in values:
                    charge(tokens.get('role'), None if absent else value)
            long_requests = sum(tiers) if not fields else None
            if not fields and (len(requests) == values['requests'] or
                               ('_request_usage' not in values and 'long_context' not in rate)):
                cost = sum(costs) if all(value is not None for value in costs) else None
        if fields:
            missing_fields[model] = sorted(fields)
        public = _public_bucket(values)
        models[model] = dict(public, prices=rate, api_cost_at_declared_rates_usd=cost,
                             long_context_requests=long_requests, missing_rate_fields=sorted(fields))
    complete = (usage['complete'] and usage.get('model_usage_complete', False) and prices is not None and
                not missing_rates and all(m['api_cost_at_declared_rates_usd'] is not None for m in models.values()))
    unknown = _public_bucket(usage.get('unattributed_usage', _bucket()))
    for tokens in unknown_requests(usage):
        charge(tokens.get('role'), None)
    buckets = list(models.values()) + [unknown]
    return dict(api_cost_at_declared_rates_usd=sum(m['api_cost_at_declared_rates_usd'] for m in models.values()) if complete else None,
                api_cost_complete=complete, usage_by_model=models, missing_model_rates=missing_rates,
                missing_model_rate_fields=missing_fields,
                # The same declared-rate estimate split by who made the call; each part is unknown on its own terms.
                # Missing rows or conflicting counts leave every role unknown; a missing rate only its own roles.
                api_cost_by_role=(by_role if usage['complete'] and usage.get('model_usage_complete', False)
                                  else dict.fromkeys(ROLES)),
                api_cache_write_tokens=usage.get(CACHE_WRITE_FIELD, _sum_known(b[CACHE_WRITE_FIELD] for b in buckets)),
                missing_api_cache_write_usage=usage.get(CACHE_WRITE_MISSING, sum(b[CACHE_WRITE_MISSING] for b in buckets)),
                unattributed_usage=unknown, missing_model_requests=usage.get('missing_model_requests', 0),
                legacy_model_assignments=usage.get('legacy_model_assignments', 0),
                observed_response_model_requests=usage.get('observed_response_model_requests'),
                missing_response_model_requests=usage.get('missing_response_model_requests'),
                response_model_observation_complete=usage.get('response_model_observation_complete') is True)


def unknown_requests(usage):
    return (usage.get('unattributed_usage') or {}).get('_request_usage') or []


def _request_cost(tokens, rates):
    if not _valid_usage(tokens):
        return None, ()
    write = tokens['cache_write_tokens']
    if 'cache_write' in rates and write is None:
        return None, ()
    if write and 'cache_write' not in rates:
        return None, ('cache_write',)
    # This zero is solely the historical, limited-scope static estimate; the
    # observed writer field and its missing count retain None/unknown.
    priced_write = write if write is not None else 0
    ordinary = tokens['input_tokens'] - tokens['cached_tokens'] - priced_write
    return ((ordinary * rates['input'] + tokens['cached_tokens'] * rates['cached'] +
             priced_write * rates.get('cache_write', 0) + tokens['output_tokens'] * rates['output']) / 1_000_000, ())


def price(usage,prices):
    return bill(usage, prices)['api_cost_at_declared_rates_usd']
