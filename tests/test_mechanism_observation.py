"""Evidence distinguishes an action, a retained view, and a successful wire response."""
from ctxpress.harness.eval_mechanism import observe
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import ComplexityTrap, ReSum


def history():
    return dict(input=[dict(role='user',content='task'),
        dict(type='function_call',call_id='a',name='shell',arguments='cat a.py'),
        dict(type='function_call_output',call_id='a',output='source'*100),
        dict(type='function_call',call_id='b',name='shell',arguments='cat b.py'),
        dict(type='function_call_output',call_id='b',output='source'*100)])


def test_retained_placeholder_does_not_repeat_operation_counts():
    rw = Rewriter(lambda: ComplexityTrap(budget=1))
    _, first = rw.rewrite_body(history(),'s')
    _, second = rw.rewrite_body(history(),'s')
    assert first['operations'] == {'placeholder':1} and second['operations'] == {}
    assert first['changed'] == second['changed'] == 1


def test_summary_commits_and_failures_have_different_operation_evidence():
    rw = Rewriter(lambda: ReSum(k=1),summarizer=lambda items:'continuation note')
    rw.rewrite_body(history(),'s')
    _, info = rw.rewrite_body(history(),'s')
    assert info['operations'] == {'history_summary':1}
    bad = Rewriter(lambda: ReSum(k=1),summarizer=lambda items:'')
    bad.rewrite_body(history(),'s')
    _, info = bad.rewrite_body(history(),'s')
    assert info['operations'] == {} and info['summary_failed'] == 1


def test_rejected_or_interrupted_changes_do_not_prove_accepted_mutations():
    rows=[dict(request=1,status=400,changed=2,operations=dict(placeholder=2)),
          dict(request=1,status=200,changed=2,operations={},stream_error='IncompleteRead'),
          dict(type='summary',status=200,completed=False,purpose='history')]
    evidence=observe(dict(rewrites=rows))
    assert evidence['changed_requests'] == 0 and not evidence['rendered_mutation_observed']
    assert evidence['operations_on_successful_requests'] == {} and evidence['failed_summaries']==1
    assert not evidence['operation_logging_available']


def test_legacy_changes_are_visible_without_inventing_operation_or_native_evidence():
    evidence=observe(dict(rewrites=[dict(request=1,status=200,changed=2)],usage=dict(summary_calls=0)))
    assert evidence['rendered_mutation_observed'] and evidence['changed_requests']==1
    assert not evidence['operation_logging_available'] and not evidence['native_compaction_logging_available']
    assert evidence['completed_native_compactions']==0


def test_summary_and_native_operations_remain_separate():
    rows=[dict(request=1,status=200,dropped=2,operations=dict(history_summary=1)),
          dict(type='summary',status=200,completed=True,purpose='history'),
          dict(type='native_compaction',status=200,completed=True)]
    evidence=observe(dict(rewrites=rows,usage=dict(native_compaction_calls=1)))
    assert evidence['operation_logging_available'] and evidence['native_compaction_logging_available']
    assert evidence['operations_on_successful_requests']==dict(history_summary=1)
    assert evidence['completed_summary_purposes']==dict(history=1) and evidence['completed_native_compactions']==1


def test_exposure_uses_only_successful_main_api_usage_and_keeps_proxy_estimates_separate():
    rows=[dict(request=i,status=200,usage={'input_tokens':i*10},tokens_before=i*1000,tokens_after=i*500)
          for i in range(1,11)]
    rows += [dict(request=11,status=400,usage={'input_tokens':999999}),
             dict(request=12,status=200,stream_error='incomplete',usage={'input_tokens':999999}),
             dict(type='summary',status=200,completed=True,usage={'input_tokens':999999}),
             dict(type='native_compaction',status=200,completed=True,usage={'input_tokens':999999})]
    evidence=observe(dict(rewrites=rows));exposure=evidence['input_exposure']
    assert exposure['api_input_tokens']==dict(observed_requests=10,missing_requests=0,minimum=10,maximum=100,
                                              mean=55,median=55,p90=90)
    assert exposure['proxy_tokens_before']['p90']==9000 and exposure['proxy_tokens_after']['p90']==4500
    assert not evidence['rendered_mutation_observed']


def test_missing_or_invalid_token_counts_are_not_zero_or_character_estimates():
    rows=[dict(request=i,status=200,usage={'input_tokens':value},tokens_before=12345)
          for i,value in enumerate((None,True,-1,'100',0))]
    exposure=observe(dict(rewrites=rows))['input_exposure']
    assert exposure['api_input_tokens']==dict(observed_requests=1,missing_requests=4,minimum=0,maximum=0,
                                              mean=0,median=0,p90=0)
    assert exposure['proxy_tokens_after']['observed_requests']==0
    assert exposure['proxy_tokens_after']['mean'] is None
    empty=observe({})['input_exposure']['api_input_tokens']
    assert empty['observed_requests']==empty['missing_requests']==0 and empty['maximum'] is None
