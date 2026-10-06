"""Returned identity coverage is separate from requested-model pricing."""
import copy
import pytest
from ctxpress.live import usage as eval_usage
from test_model_accounting import result, rates


PRICES = {'models':{'main':rates(),'reflect':rates(1,.1,2)}}


def test_complete_bill_can_have_missing_returned_identity():
    usage=eval_usage.analyze(result())
    bill=eval_usage.bill(usage,PRICES)
    assert usage['model_usage_complete'] and bill['api_cost_complete']
    assert bill['observed_response_model_requests']==1 and bill['missing_response_model_requests']==2
    assert not bill['response_model_observation_complete']
    assert bill['usage_by_model']['main']['missing_response_model_requests']==2
    assert bill['usage_by_model']['reflect']['missing_response_model_requests']==0


def test_returned_snapshots_are_observed_without_changing_requested_model_rates():
    original=eval_usage.bill(eval_usage.analyze(result()),PRICES)
    sample=result()
    sample['rewrites'][0]['response_model']='main-snapshot-A'
    sample['rewrites'][2]['response_model']='main-snapshot-B'
    bill=eval_usage.bill(eval_usage.analyze(sample),PRICES)
    assert bill['response_model_observation_complete'] and bill['missing_response_model_requests']==0
    assert bill['observed_response_model_requests']==3
    assert bill['api_cost_at_declared_rates_usd']==original['api_cost_at_declared_rates_usd']
    assert bill['usage_by_model']['main']['response_models']=={'main-snapshot-A':1,'main-snapshot-B':1}


@pytest.mark.parametrize('returned',[None,'', '  ', 3, {}, []])
def test_missing_or_invalid_returned_ids_stay_missing(returned):
    sample=result();sample['rewrites'][1]['response_model']=returned
    bill=eval_usage.bill(eval_usage.analyze(sample),PRICES)
    assert bill['api_cost_complete'] and bill['missing_response_model_requests']==3
    assert not bill['response_model_observation_complete']


def test_missing_wire_records_count_as_missing_identity_and_are_not_imputed():
    sample=result();sample['requests']=4
    usage=eval_usage.analyze(sample)
    assert usage['missing_response_model_requests']==5
    assert usage['observed_response_model_requests']==1
    assert not usage['response_model_observation_complete']


def test_aggregation_preserves_unknown_legacy_identity_and_empty_is_not_complete():
    usage=eval_usage.analyze(result())
    combined=eval_usage.combine([usage,usage],'main')
    assert combined['missing_response_model_requests']==4
    assert combined['observed_response_model_requests']==2
    assert combined['usage_by_model']['main']['missing_response_model_requests']==4
    legacy=copy.deepcopy(usage)
    for key in ('missing_response_model_requests','observed_response_model_requests','response_model_observation_complete'):
        legacy.pop(key)
    for bucket in [*legacy['usage_by_model'].values(),legacy['unattributed_usage']]:
        bucket.pop('missing_response_model_requests')
    mixed=eval_usage.bill(eval_usage.combine([usage,legacy],'main'),PRICES)
    assert mixed['api_cost_complete']
    assert mixed['missing_response_model_requests'] is None and not mixed['response_model_observation_complete']
    assert not eval_usage.combine([])['response_model_observation_complete']


def test_local_compaction_stop_is_not_a_missing_upstream_identity():
    sample=result();sample['rewrites'].append({'type':'native_compaction_blocked','upstream_sent':False})
    usage=eval_usage.analyze(sample)
    assert usage['missing_response_model_requests']==2
