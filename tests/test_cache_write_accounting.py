"""Offline financial regressions using declared public API Standard rates.

Sources verified 2026-10-04 (fixtures, never inferred by production code):
https://developers.openai.com/api/docs/guides/prompt-caching
https://developers.openai.com/api/docs/models/gpt-6.1-sol
https://developers.openai.com/api/docs/models/gpt-6-luna
"""
import json

import pytest

from ctxpress.live.telemetry import summary
from ctxpress.live import usage as eval_usage
from ctxpress.harness.jobs import plan as eval_plan, queue as evaluation
from ctxpress.harness.results.report import write_report
from ctxpress.live.proxy import find_usage
from ctxpress.live.summarize import ResponsesSummarizer
from test_evaluation import config


SOL = 'gpt-6.1-sol'
LUNA = 'gpt-6-luna'


def rates(model=SOL, long_context=True):
    input, cached, write, output = (2, .1, 2.5, 10) if model == SOL else (.1, .01, .125, .5)
    result = dict(input=input, cached=cached, cache_write=write, output=output,
                  unit='USD_per_million_tokens', source=f'https://developers.openai.com/api/docs/models/{model}',
                  as_of='2026-10-04')
    if long_context:
        result['long_context'] = dict(input_tokens_threshold=272000, input=input*2,
                                      cached=cached*2, cache_write=write*2, output=output*1.5)
    return result


def row(input=15000, cached=12000, write=3000, output=0, model=SOL, kind='main'):
    record = dict(model=model, response_model=model+'-snapshot',
                  usage=dict(input_tokens=input, cached_tokens=cached, cache_write_tokens=write, output_tokens=output))
    if kind == 'main':
        record.update(request=1, tokens_before=input, tokens_after=input, changed=0)
    else:
        record['type'] = kind
    return record


def run(*rows):
    return dict(model=SOL, requests=sum('request' in r for r in rows), rewrites=list(rows),
                usage=dict(summary_calls=sum(r.get('type') == 'summary' for r in rows),
                           native_compaction_calls=sum(r.get('type') == 'native_compaction' for r in rows)))


def priced(sample, prices=None):
    return eval_usage.bill(eval_usage.analyze(sample), prices or {'models': {SOL: rates(), LUNA: rates(LUNA)}})


@pytest.mark.parametrize('stream', [False, True])
def test_find_usage_preserves_official_writes_and_missing_writes(stream):
    response = dict(usage=dict(input_tokens=15000, input_tokens_details=dict(cached_tokens=12000, cache_write_tokens=3000),
                               output_tokens=0, output_tokens_details=dict(reasoning_tokens=0)))
    def encode(value):
        if stream:
            return ('data: {"type":"response.output_text.delta","delta":"hi"}\n\n' +
                    'data: '+json.dumps(dict(type='response.completed', response=value))+'\n\ndata: [DONE]\n\n').encode()
        return json.dumps(value).encode()
    normalized = find_usage(encode(response))
    assert normalized == dict(input_tokens=15000, cached_tokens=12000, cache_write_tokens=3000,
                              output_tokens=0, reasoning_tokens=0)
    assert priced(run(dict(row(), usage=normalized)))['api_cost_at_declared_rates_usd'] == pytest.approx(.0087)
    del response['usage']['input_tokens_details']['cache_write_tokens']
    assert find_usage(encode(response))['cache_write_tokens'] is None


def test_official_input_partitions_replace_input_rate_instead_of_adding_fee():
    bill = priced(run(row()))
    assert bill['api_cost_complete'] and bill['api_cache_write_tokens'] == 3000
    assert bill['api_cost_at_declared_rates_usd'] == pytest.approx(.0087)
    assert bill['missing_api_cache_write_usage'] == 0
    assert bill['usage_by_model'][SOL]['response_models'] == {SOL+'-snapshot': 1}


def test_main_summary_reflection_and_native_compaction_are_priced_once(tmp_path):
    records = [row(18000, output=100), row(kind='summary'),
               row(18000, output=100, model=LUNA, kind='summary'),
               row(2000, cached=0, write=2000, output=10, kind='native_compaction')]
    bill = priced(run(*records))
    assert bill['api_cost_at_declared_rates_usd'] == pytest.approx(.0157+.0087+.000845+.0051)
    sol, luna = bill['usage_by_model'][SOL], bill['usage_by_model'][LUNA]
    assert (sol['requests'], sol['main_requests'], sol['summary_calls'], sol['native_compaction_calls']) == (3, 1, 1, 1)
    assert sol['api_cost_at_declared_rates_usd'] == pytest.approx(.0295)
    assert luna['summary_calls'] == 1 and luna['api_cost_at_declared_rates_usd'] == pytest.approx(.000845)
    log = tmp_path/'usage.jsonl'
    log.write_text('\n'.join(json.dumps(r) for r in records), encoding='utf-8')
    telemetry = summary(str(log))
    assert (telemetry['api_input_tokens'], telemetry['api_cached_tokens'], telemetry['api_cache_write_tokens'],
            telemetry['api_output_tokens']) == (53000, 36000, 11000, 210)
    assert telemetry['missing_api_cache_write_usage'] == 0
    sample = run(*records)
    sample['usage'] = telemetry
    assert priced(sample)['api_cost_at_declared_rates_usd'] == bill['api_cost_at_declared_rates_usd']


@pytest.mark.parametrize('model', [SOL, LUNA])
@pytest.mark.parametrize('input,multiplier', [(272000, 1), (272001, 2)])
def test_long_context_boundary_prices_entire_request_including_reads_writes_and_output(model, input, multiplier):
    bill = priced(run(row(input, cached=12000, write=3000, output=100, model=model)))
    rate = rates(model)
    expected = ((input-15000)*rate['input']*multiplier + 12000*rate['cached']*multiplier +
                3000*rate['cache_write']*multiplier + 100*rate['output']*(1.5 if multiplier == 2 else 1)) / 1e6
    assert bill['api_cost_at_declared_rates_usd'] == pytest.approx(expected)
    assert bill['usage_by_model'][model]['long_context_requests'] == int(multiplier == 2)


def test_many_short_requests_and_combined_runs_never_trigger_long_tier():
    samples = [eval_usage.analyze(run(row(150000, cached=0, write=0, output=100))) for _ in range(3)]
    combined = eval_usage.combine(samples, SOL)
    bill = eval_usage.bill(combined, {'models': {SOL: rates()}})
    assert combined['api_input_tokens'] == 450000
    assert bill['api_cost_at_declared_rates_usd'] == pytest.approx(.903)
    assert bill['usage_by_model'][SOL]['long_context_requests'] == 0
    assert priced(run(row(150000, cached=0, write=0, output=100),
                      row(150000, cached=0, write=0, output=100)))['api_cost_at_declared_rates_usd'] == pytest.approx(.602)


def test_old_aggregate_can_use_static_rates_but_cannot_guess_request_tiers():
    accounting = eval_usage.analyze(run(row(150000, cached=0, write=0), row(150000, cached=0, write=0)))
    for bucket in [accounting['usage_by_model'][SOL], accounting['unattributed_usage']]:
        for field in ('_request_usage', 'api_cache_write_tokens', 'missing_api_cache_write_usage'):
            bucket.pop(field)
    accounting.pop('api_cache_write_tokens')
    accounting.pop('missing_api_cache_write_usage')
    legacy = rates(long_context=False)
    legacy.pop('cache_write')
    bill = eval_usage.bill(accounting, legacy)
    assert bill['api_cost_at_declared_rates_usd'] == pytest.approx(.6)
    assert bill['api_cache_write_tokens'] is None and bill['missing_api_cache_write_usage'] == 2
    extended = eval_usage.bill(accounting, rates())
    assert extended['api_cost_at_declared_rates_usd'] is None
    assert extended['missing_model_rate_fields'] == {SOL: ['per_request_usage']}


def test_combined_models_and_mixed_tiers_preserve_each_request():
    samples = [eval_usage.analyze(run(row(272000, cached=0, write=0, output=100),
                                    row(272001, output=100, model=LUNA, kind='summary'))),
               eval_usage.analyze(run(row(272001, output=100),
                                      row(1000, cached=0, write=1000, output=10, model=LUNA, kind='summary')))]
    bill = eval_usage.bill(eval_usage.combine(samples, SOL), {'models': {SOL: rates(), LUNA: rates(LUNA)}})
    # Sol: 0.545 + (257001*4 + 12000*.2 + 3000*5 + 100*15)/1M.
    # Luna: (257001*.2 + 12000*.02 + 3000*.25 + 100*.75)/1M + 0.000130.
    assert bill['api_cost_at_declared_rates_usd'] == pytest.approx(.545+1.046904+.0524652+.000130)
    assert bill['usage_by_model'][SOL]['long_context_requests'] == 1
    assert bill['usage_by_model'][LUNA]['long_context_requests'] == 1


@pytest.mark.parametrize('missing', ['absent', 'null'])
def test_missing_writer_stays_unknown_with_explicit_write_rates_but_legacy_scope_survives(tmp_path, missing):
    record = row()
    if missing == 'absent':
        del record['usage']['cache_write_tokens']
    else:
        record['usage']['cache_write_tokens'] = None
    sample = run(row(), record)
    accounting = eval_usage.analyze(sample)
    assert accounting['complete']  # Base token telemetry is still complete.
    assert accounting['api_cache_write_tokens'] is None and accounting['missing_api_cache_write_usage'] == 1
    bill = priced(sample)
    assert bill['api_cost_at_declared_rates_usd'] is None and not bill['api_cost_complete']
    assert bill['usage_by_model'][SOL]['api_cache_write_tokens'] is None
    legacy = rates(long_context=False)
    legacy.pop('cache_write')
    # A known positive writer can never silently use the ordinary input rate.
    assert priced(sample, legacy)['missing_model_rate_fields'] == {SOL: ['cache_write']}
    old = eval_usage.bill(eval_usage.analyze(run(record)), legacy)
    assert old['api_cost_complete'] and old['api_cost_at_declared_rates_usd'] == pytest.approx(.0072)
    assert old['api_cache_write_tokens'] is None and old['missing_api_cache_write_usage'] == 1
    log = tmp_path/'log.jsonl'
    log.write_text('\n'.join(json.dumps(r) for r in sample['rewrites']), encoding='utf-8')
    assert summary(str(log))['api_cache_write_tokens'] is None
    assert summary(str(log))['missing_api_cache_write_usage'] == 1


def test_missing_write_rate_is_unknown_when_writes_are_positive_but_explicit_zero_is_priceable():
    legacy = rates(long_context=False)
    legacy.pop('cache_write')
    assert priced(run(row()), legacy)['api_cost_at_declared_rates_usd'] is None
    assert priced(run(row(write=0)), legacy)['api_cost_complete']
    free_write = dict(legacy, cache_write=0)
    assert priced(run(row()), free_write)['api_cost_at_declared_rates_usd'] == pytest.approx(.0012)
    assert priced(run(row()), dict(legacy, cache_write=2))['api_cost_at_declared_rates_usd'] == pytest.approx(.0072)


@pytest.mark.parametrize('bad', [-1, True, False, 1.0, .5, float('nan'), float('inf'), '3000', 3001])
def test_invalid_or_overlapping_write_counts_never_produce_a_bill(bad):
    sample = run(row(write=bad))
    accounting = eval_usage.analyze(sample)
    assert not accounting['complete'] and accounting['missing_api_usage'] == 1
    assert accounting['api_cache_write_tokens'] is None and accounting['missing_api_cache_write_usage'] == 1
    assert priced(sample)['api_cost_at_declared_rates_usd'] is None
    legacy = rates(long_context=False)
    legacy.pop('cache_write')
    assert priced(sample, legacy)['api_cost_at_declared_rates_usd'] is None


@pytest.mark.parametrize('field', ['input_tokens', 'cached_tokens', 'output_tokens'])
@pytest.mark.parametrize('bad', [-1, True, 1.0, None])
def test_invalid_base_counts_cannot_be_hidden_by_valid_writer(field, bad):
    record = row()
    record['usage'][field] = bad
    assert priced(run(record))['api_cost_at_declared_rates_usd'] is None


@pytest.mark.parametrize('bad', [-1, True, float('nan'), float('inf'), None, '2.5'])
@pytest.mark.parametrize('tier', [False, True])
def test_invalid_declared_write_rates_are_rejected(bad, tier):
    price = rates()
    (price['long_context'] if tier else price)['cache_write'] = bad
    with pytest.raises(ValueError, match='invalid declared price'):
        eval_usage.validate_prices({'models': {SOL: price}})
    with pytest.raises(ValueError):
        priced(run(row()), price)


@pytest.mark.parametrize('bad', [-1, True, 272000.0, None, float('nan')])
def test_long_context_requires_explicit_integer_threshold(bad):
    price = rates()
    price['long_context']['input_tokens_threshold'] = bad
    with pytest.raises(ValueError, match='long_context'):
        eval_usage.validate_prices(price)


def test_long_context_requires_complete_four_rate_schema_and_no_hidden_defaults():
    for field in ('input', 'cached', 'cache_write', 'output', 'input_tokens_threshold'):
        price = rates()
        del price['long_context'][field]
        with pytest.raises(ValueError):
            eval_usage.validate_prices(price)
    price = rates()
    price.pop('cache_write')
    with pytest.raises(ValueError):
        eval_usage.validate_prices(price)
    # A model name and Standard-looking rates alone never infer a long tier.
    assert priced(run(row(272001, cached=0, write=0)), rates(long_context=False))['api_cost_at_declared_rates_usd'] == pytest.approx(.544002)


def test_unknown_writer_is_preserved_across_models_runs_and_unobserved_calls():
    known = eval_usage.analyze(run(row(), row(model=LUNA, kind='summary')))
    old_record = row()
    del old_record['usage']['cache_write_tokens']
    unknown = eval_usage.analyze(run(old_record))
    combined = eval_usage.combine([known, unknown], SOL)
    bill = eval_usage.bill(combined, {'models': {SOL: rates(), LUNA: rates(LUNA)}})
    assert bill['api_cache_write_tokens'] is None and bill['missing_api_cache_write_usage'] == 1
    assert bill['usage_by_model'][LUNA]['api_cache_write_tokens'] == 3000
    assert bill['usage_by_model'][LUNA]['api_cost_at_declared_rates_usd'] == pytest.approx(.000495)
    assert bill['api_cost_at_declared_rates_usd'] is None
    sample = run(row())
    sample['usage']['summary_calls'] = 1
    accounting = eval_usage.analyze(sample)
    assert accounting['api_cache_write_tokens'] is None and accounting['missing_api_cache_write_usage'] == 1
    assert not priced(sample)['api_cost_complete']


@pytest.mark.parametrize('wrong', [0, 3001, True, 3000.0])
def test_writer_telemetry_mismatch_prevents_complete_cost(wrong):
    sample = run(row())
    sample['usage']['api_cache_write_tokens'] = wrong
    assert not priced(sample)['api_cost_complete']


def test_reflection_callback_retains_real_writer_usage():
    records = []
    class Response:
        status = 200
        def read(self):
            return json.dumps(dict(model=LUNA+'-snapshot', status='completed',
                                   output=[dict(type='message', content=[dict(type='output_text', text='note')])],
                                   usage=dict(input_tokens=15000, output_tokens=10,
                                              input_tokens_details=dict(cached_tokens=12000, cache_write_tokens=3000)))).encode()
    class Connection:
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            pass
    callback = ResponsesSummarizer(Connection, '/responses', {}, {'model': SOL}, records.append)
    assert callback.summarize_prompt('reflect', 'step', purpose='reflect', model=LUNA) == 'note'
    assert records[0]['usage']['cache_write_tokens'] == 3000
    assert records[0]['model'] == LUNA and records[0]['response_model'] == LUNA+'-snapshot'
    assert priced(run(row(), records[0]))['usage_by_model'][LUNA]['api_cost_at_declared_rates_usd'] == pytest.approx(.0005)


@pytest.mark.parametrize('failure', [None, 'missing-write', 'missing-rate'])
def test_saved_reports_expose_writer_and_missing_data_without_private_request_lists(tmp_path, failure):
    cfg = config()
    prices = {'models': {SOL: rates(), LUNA: rates(LUNA)}}
    if failure == 'missing-rate':
        prices['models'][LUNA].pop('long_context')
        prices['models'][LUNA].pop('cache_write')
    cfg.update(model=SOL, methods=[{'class': 'NoCompaction'}], repeats=1, prices=prices)
    directory = evaluation.prepare(eval_plan.compile_plan(cfg), tmp_path/'run')
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET status='running',attempt=1")
    job = evaluation.state(directory)['jobs'][0]['id']
    sample = run(row(), row(model=LUNA, kind='summary'))
    if failure == 'missing-write':
        del sample['rewrites'][1]['usage']['cache_write_tokens']
    evaluation.set_result(directory, job, 1, result=sample)
    write_report(directory)
    raw = (directory/'report.json').read_text(encoding='utf-8')
    saved = json.loads(raw)
    group = saved['methods'][0]
    assert group['api_cost_complete'] == (failure is None)
    assert group['api_cache_write_tokens'] == (None if failure == 'missing-write' else 6000)
    assert group['missing_api_cache_write_usage'] == int(failure == 'missing-write')
    assert group['api_cost_at_declared_rates_usd'] == (pytest.approx(.009195) if failure is None else None)
    if failure == 'missing-rate':
        assert group['missing_model_rate_fields'] == {LUNA: ['cache_write']}
    assert '_request_usage' not in raw and '_accounting' not in raw
    markup = (directory/'report.html').read_text(encoding='utf-8')
    assert '缓存写入' in markup and '缺失写入用量请求' in markup
    assert '不代表 ChatGPT 实际账单' in markup and 'counterfactual API estimate' in saved['cost_scope']
    assert ('cache_write' in markup) if failure == 'missing-rate' else ('未知' in markup if failure else '0.009195' in markup)


def test_comparison_cannot_claim_savings_when_cache_rewrites_cost_more(tmp_path):
    from test_eval_compare import cohort, change, analyzed
    plan, jobs = cohort(tmp_path)
    plan['config']['prices'] = {'models': {'fixture-model': rates()}}
    for job in jobs[:4]:
        change(job, lambda result: result['rewrites'][0]['usage'].update(cache_write_tokens=0))
    for job in jobs[4:]:
        # Cached-prefix invalidation saves 10 input tokens but writes 80 tokens:
        # the old 90*2 ordinary-input cost becomes 80*2.5 in write cost.
        def rewrite(result):
            result['rewrites'][0]['usage'].update(input_tokens=90, cache_write_tokens=80)
            result['usage']['api_input_tokens'] = 90
        change(job, rewrite)
    comparison = analyzed(plan, jobs)
    assert comparison['quality_satisfied'] and comparison['api_cost_complete']
    assert comparison['api_saving_usd'] == pytest.approx(-4*20/1e6)
    assert comparison['verdict'] == 'observed_quality_constraint_met_api_cost_not_lower'
    change(jobs[-1], lambda result: result['rewrites'][0]['usage'].pop('cache_write_tokens'))
    comparison = analyzed(plan, jobs)
    assert comparison['quality_satisfied'] and not comparison['api_cost_complete']
    assert comparison['api_saving_usd'] is None


def test_failed_summary_calls_record_the_exception():
    import http.client
    import pytest
    from ctxpress.live.summarize import ResponsesSummarizer

    class Dropped:
        def request(self, *args, **kwargs):
            raise http.client.RemoteDisconnected("Remote end closed connection without response")

        def close(self):
            pass

    records = []
    summarizer = ResponsesSummarizer(lambda: Dropped(), "/v1/responses", {"Authorization": "Bearer secret"},
                                     {"model": "m", "input": []}, record=records.append)
    with pytest.raises(http.client.RemoteDisconnected):
        summarizer.summarize_prompt("system", "user", purpose="history")
    assert records[0]["completed"] is False and "status" not in records[0]
    assert records[0]["error"] == "RemoteDisconnected: Remote end closed connection without response"
    assert "secret" not in str(records[0])
