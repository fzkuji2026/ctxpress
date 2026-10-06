"""Numeric rewards are valid measurements but do not imply a boolean solved task."""
import json
import pytest
from ctxpress.harness.results import outcomes as eval_outcomes
from ctxpress.harness.jobs import plan as eval_plan, queue as evaluation
from ctxpress.harness.results.report import report, write_report
from test_task_start_plan import config


@pytest.mark.parametrize('grade',[
    {}, {'resolved':1,'infra_invalid':False}, {'resolved':True,'infra_invalid':True},
    {'valid_rewards':True,'rewards':{'reward':float('nan')},'infra_invalid':False},
    {'valid_rewards':True,'rewards':{'reward':True},'infra_invalid':False},
    {'valid_rewards':True,'rewards':{'reward':1},'infra_invalid':None},
    {'valid_rewards':True,'rewards':{'reward':1},'infra_invalid':False,'error':'trial exception'}])
def test_incomplete_or_invalid_outcomes_do_not_produce_valid_quality(grade):
    observed=eval_outcomes.observe(grade)
    assert not observed['boolean_valid'] and not observed['rewards_valid'] and observed['resolved'] is None


def test_boolean_and_numeric_scores_remain_separate():
    assert eval_outcomes.observe({'resolved':False,'infra_invalid':False})['boolean_valid']
    result=eval_outcomes.observe({'valid_rewards':True,'rewards':{'reward':0.75},'infra_invalid':False})
    assert result['rewards_valid'] and not result['boolean_valid'] and not result['ungraded']


def test_reports_show_each_reward_metric_without_inventing_a_resolve_rate(tmp_path):
    cfg=config(tmp_path);cfg['repeats']=3
    plan=eval_plan.compile_plan(cfg);directory=evaluation.prepare(plan,tmp_path/'run')
    values=[{'reward':0.5,'another':2},{'reward':1.0},None]
    with evaluation.database(directory) as connection:
        for job,rewards in zip(plan['jobs'],values):
            grade={'infra_invalid':False,'resolved':None,'valid_rewards':True,'rewards':rewards} if rewards else {'infra_invalid':True,'error':'fixture infrastructure failure'}
            result=dict(test_only=True,requests=1,rewrites=[{'request':1,'status':200}],grade=grade)
            connection.execute("UPDATE jobs SET status='completed',result=? WHERE id=?",(json.dumps(result),job['id']))
    result=report(directory);method=result['methods'][0]
    assert method['valid_rewards']==2 and method['valid_grades']==0 and method['resolve_rate_valid_grades'] is None
    assert method['infra_invalid']==1 and method['ungraded']==0
    assert method['reward_metrics']['reward']==dict(count=2,sum=1.5,mean=0.75,minimum=0.5,maximum=1.0)
    assert method['reward_metrics']['another']['count']==1
    assert result['benchmark_release']=='fixture-v1' and result['evidence'].startswith('synthetic')
    write_report(directory)
    assert '官方 verifier 数值指标' in (directory/'report.html').read_text(encoding='utf-8')


def test_missing_verifier_or_agent_exception_is_not_proof_of_an_infrastructure_failure():
    observed=eval_outcomes.observe({'resolved':None,'infra_invalid':None,'failure_kind':'grading_error','error':'trial timeout'})
    assert observed['grading_error'] and not observed['infra_invalid']
    assert not observed['boolean_valid'] and not observed['rewards_valid']


@pytest.mark.parametrize('failure', [
    {'failure_kind': 'grading_error'},
    {'scoring_complete': False},
    {'failure_kind': 'grading_error', 'scoring_complete': False},
])
def test_incomplete_official_grading_cannot_be_counted_as_quality_or_ungraded(failure):
    # Collectors can retain partial metrics without an error message at the top level.
    grade = dict(infra_invalid=False, resolved=True, valid_rewards=True, rewards={'score': 1.0},
                 quality_kind='code_sample', sample_passed=True, **failure)
    outcome = eval_outcomes.observe(grade)
    assert outcome['grading_error'] and not outcome['infra_invalid']
    assert not outcome['ungraded']
    assert not any(outcome[k] for k in ('boolean_valid', 'rewards_valid', 'code_sample_valid'))
    assert outcome['resolved'] is None and outcome['sample_passed'] is None and outcome['rewards'] == {}


def test_infrastructure_error_takes_precedence_without_double_counting():
    outcome = eval_outcomes.observe(dict(infra_invalid=True, failure_kind='grading_error', scoring_complete=False))
    assert outcome['infra_invalid'] and not outcome['grading_error'] and not outcome['ungraded']


def test_complete_grading_and_absent_grading_stay_distinct():
    complete = eval_outcomes.observe(dict(infra_invalid=False, resolved=False, failure_kind=None, scoring_complete=True))
    assert complete['boolean_valid'] and complete['resolved'] is False and not complete['grading_error']
    absent = eval_outcomes.observe({})
    assert absent['ungraded'] and not absent['grading_error']
