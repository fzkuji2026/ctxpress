import pytest
from ctxpress.core.context_forecast import ActionForecast


def forecast():
    return ActionForecast(retention_prior=.5,seconds_prior=2,prior_weight=2,quantile=.9,history_limit=3)


def test_forecast_uses_only_past_same_operation_and_reports_priors():
    f=forecast()
    assert f.predict('annotation')['samples']==0
    f.observe('annotation',input_chars=1000,output_chars=100,seconds=5,cost_estimate=.2,usable=True)
    learned=f.predict('annotation')
    assert learned['retained_ratio']==pytest.approx(1.1/3)
    assert learned['seconds']==3
    assert f.predict('compact')['samples']==0


def test_failures_reduce_predicted_saving_and_increase_latency():
    f=forecast()
    before=f.predict('annotation')
    for _ in range(3):
        f.observe('annotation',input_chars=1000,output_chars=0,seconds=10,cost_estimate=.1,usable=False)
    after=f.predict('annotation')
    assert after['retained_ratio']>before['retained_ratio']
    assert after['seconds']>before['seconds']
    assert after['failures']==3


def test_history_is_bounded_and_oversized_summary_has_no_predicted_saving():
    f=forecast()
    for _ in range(10):
        f.observe('compact',input_chars=100,output_chars=200,seconds=1,cost_estimate=.1,usable=True)
    assert f.predict('compact')['samples']==3
    assert f.predict('compact')['retained_ratio']==pytest.approx(.8)


def test_invalid_observations_do_not_pollute_history():
    f=forecast()
    with pytest.raises(ValueError):
        f.observe('annotation',input_chars=1,output_chars=1,seconds=float('nan'),cost_estimate=0,usable=True)
    assert f.predict('annotation')['samples']==0
