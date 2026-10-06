"""Separate reflection billing, unknown rates, legacy records, and saved reports."""
import copy
import json

import pytest

from ctxpress.live import usage as eval_usage
from ctxpress.harness.jobs import plan as eval_plan, queue as evaluation
from ctxpress.harness.results.report import write_report
from ctxpress.live.summarize import ResponsesSummarizer
from ctxpress.live.proxy import find_model
from test_evaluation import config


def rates(input=10, cached=1, output=20):
    return dict(input=input, cached=cached, output=output, unit='USD_per_million_tokens',
                source='offline fixture rates', as_of='2000-01-01')


def result():
    return dict(model='main', requests=1, usage=dict(summary_calls=1, native_compaction_calls=1), rewrites=[
        dict(request=1, status=200, model='main', usage=dict(input_tokens=100, cached_tokens=40, output_tokens=10)),
        dict(type='summary', purpose='reflect', model='reflect', response_model='reflect-snapshot',
             usage=dict(input_tokens=200, cached_tokens=0, output_tokens=30)),
        dict(type='native_compaction', model='main', usage=dict(input_tokens=50, cached_tokens=0, output_tokens=5))])


def test_main_reflection_and_native_usage_are_priced_separately():
    usage = eval_usage.analyze(result())
    priced = eval_usage.bill(usage, {'models': {'main': rates(), 'reflect': rates(1, .1, 2)}})
    assert priced['api_cost_complete']
    assert priced['api_cost_at_declared_rates_usd'] == pytest.approx((110*10 + 40 + 15*20 + 200 + 30*2)/1e6)
    assert priced['usage_by_model']['main']['main_requests'] == 1
    assert priced['usage_by_model']['main']['native_compaction_calls'] == 1
    assert priced['usage_by_model']['reflect']['summary_calls'] == 1
    assert priced['usage_by_model']['reflect']['response_models'] == {'reflect-snapshot': 1}


@pytest.mark.parametrize('prices', [None, rates(), {'models': {'main': rates()}}])
def test_missing_reflection_rate_keeps_total_unknown(prices):
    priced = eval_usage.bill(eval_usage.analyze(result()), prices)
    assert priced['api_cost_at_declared_rates_usd'] is None and not priced['api_cost_complete']
    assert 'reflect' in priced['missing_model_rates']


def test_legacy_reflection_is_never_assigned_main_model_rates():
    sample = result()
    for row in sample['rewrites']:
        row.pop('model')
    usage = eval_usage.analyze(sample)
    assert usage['complete'] and not usage['model_usage_complete']
    assert usage['legacy_model_assignments'] == 2 and usage['missing_model_requests'] == 1
    assert usage['unattributed_usage']['api_input_tokens'] == 200
    assert eval_usage.price(usage, rates()) is None


def test_shared_main_model_summary_can_use_legacy_main_rates():
    sample = result()
    sample['rewrites'][1]['model'] = 'main'
    priced = eval_usage.bill(eval_usage.analyze(sample), rates())
    assert priced['api_cost_complete'] and set(priced['usage_by_model']) == {'main'}


def test_bad_or_missing_usage_cannot_produce_a_complete_bill():
    sample = result()
    del sample['rewrites'][1]['usage']['cached_tokens']
    usage = eval_usage.analyze(sample)
    priced = eval_usage.bill(usage, {'models': {'main': rates(), 'reflect': rates(1, .1, 2)}})
    assert priced['api_cost_at_declared_rates_usd'] is None
    assert priced['usage_by_model']['reflect']['api_cost_at_declared_rates_usd'] is None


def test_aggregate_preserves_each_model_and_unknown_usage():
    complete = eval_usage.analyze(result())
    incomplete = result()
    del incomplete['rewrites'][1]['model']
    usage = eval_usage.combine([complete, eval_usage.analyze(incomplete)], 'main')
    assert usage['usage_by_model']['main']['api_input_tokens'] == 300
    assert usage['usage_by_model']['reflect']['api_input_tokens'] == 200
    assert usage['unattributed_usage']['api_input_tokens'] == 200
    assert eval_usage.price(usage, {'models': {'main': rates(), 'reflect': rates()}}) is None


@pytest.mark.parametrize('prices', [{'models': {}}, {'models': {'main': None}}, {'models': {'': rates()}},
                                   {'models': {'main': dict(rates(), input=float('nan'))}},
                                   dict(rates(), extra='x'), dict(rates(), cached=True)])
def test_invalid_rates_are_rejected(prices):
    with pytest.raises(ValueError):
        eval_usage.validate_prices(prices)


def test_reflection_logger_records_requested_and_returned_model():
    records, requests = [], []
    class Response:
        status = 200
        def read(self):
            return json.dumps(dict(model='reflect-snapshot', status='completed', output=[dict(type='message', content=[
                dict(type='output_text', text='short step')])], usage=dict(input_tokens=20, output_tokens=3,
                input_tokens_details=dict(cached_tokens=0)))).encode()
    class Connection:
        def request(self, *args, **kwargs):
            requests.append(json.loads(kwargs['body']))
        def getresponse(self):
            return Response()
        def close(self):
            pass
    callback = ResponsesSummarizer(Connection, '/responses', {}, {'model': 'main'}, records.append)
    assert callback.summarize_prompt('reflect', 'step', purpose='reflect', model='reflect') == 'short step'
    assert requests[0]['model'] == records[0]['model'] == 'reflect'
    assert records[0]['response_model'] == 'reflect-snapshot'
    assert records[0]['usage']['input_tokens'] == 20
    assert find_model(b'data: {"type":"response.completed","response":{"model":"main-snapshot"}}\n\n') == 'main-snapshot'


def test_saved_json_and_html_reports_show_model_costs_and_unknown_rates(tmp_path):
    cfg = config()
    cfg.update(model='main', methods=[{'class': 'NoCompaction'}], repeats=1,
               prices={'models': {'main': rates(), 'reflect': rates(1, .1, 2)}})
    directory = evaluation.prepare(eval_plan.compile_plan(cfg), tmp_path/'run')
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET status='running',attempt=1")
    job = evaluation.state(directory)['jobs'][0]['id']
    evaluation.set_result(directory, job, 1, result=result())
    write_report(directory)
    report = json.loads((directory/'report.json').read_text(encoding='utf-8'))
    group = report['methods'][0]
    assert group['api_cost_complete'] and group['usage_by_model']['reflect']['summary_calls'] == 1
    saved = json.loads((directory/'report.json').read_text(encoding='utf-8'))
    assert saved['methods'][0]['api_cost_at_declared_rates_usd'] == group['api_cost_at_declared_rates_usd']
    html = (directory/'report.html').read_text(encoding='utf-8')
    assert 'reflect' in html and '0.000260' in html
    assert 'reflect-snapshot: 1' in html and '缺失返回身份' in html
    assert group['api_cost_complete'] and not group['response_model_observation_complete']
    assert group['missing_response_model_requests'] == 2
    assert '_accounting' not in group
