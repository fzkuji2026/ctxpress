"""Milestone validity and author metric coverage stay independent of issue scores."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from ctxpress.harness.results import outcomes as eval_outcomes, report as eval_report
from ctxpress.harness.jobs import plan as eval_plan, queue as evaluation
from test_task_start_plan import config


def grade(*, complete=True, submitted=2, valid=True, infra=False):
    return dict(quality_kind='milestone', official_metrics_valid=valid, infra_invalid=infra,
                failure_kind=None if valid else 'grading_error', scoring_complete=complete,
                submission_complete=submitted == 2, resolved=True if complete else None,
                official_metrics=dict(error=False, graded=2, submitted=submitted, evaluated=submitted,
                                      resolved=submitted, resolve_pct=submitted * 50,
                                      score_1000=submitted * 50, score_full=submitted * 50,
                                      score_reliable=submitted * 50, precision=submitted * 50,
                                      recall=submitted * 50, infra_invalid=int(infra)),
                coverage=dict(graded=2, submitted=submitted, served_graded=submitted,
                              unsubmitted=['M3'] if submitted == 1 else [], grading_errors={}))


@pytest.mark.parametrize('submitted', [0, 1, 2])
def test_valid_author_metrics_do_not_require_full_submission_or_become_issue_or_reward_scores(submitted):
    current = grade(complete=submitted == 2, submitted=submitted)
    # Even redundant legacy boolean/reward fields must not mix score families.
    current.update(resolved=True, valid_rewards=True, rewards={'score': 1.0})
    before = copy.deepcopy(current)
    observed = eval_outcomes.observe(current)
    assert observed['milestone_metrics_valid'] and observed['official_metrics'] == before['official_metrics']
    assert not any(observed[key] for key in ('grading_error', 'ungraded', 'boolean_valid', 'rewards_valid', 'code_sample_valid'))
    assert observed['resolved'] is None and observed['rewards'] == {}
    assert current == before


@pytest.mark.parametrize('change', ['failure_kind', 'error', 'infra_invalid', 'metrics_error', 'metrics_missing', 'valid_missing', 'valid_false'])
def test_explicit_metric_validity_cannot_override_failure_evidence(change):
    current = grade(complete=False, submitted=1)
    if change == 'failure_kind':
        current['failure_kind'] = 'grading_error'
    elif change == 'error':
        current['error'] = 'newer retry failed'
    elif change == 'infra_invalid':
        current['infra_invalid'] = True
    elif change == 'metrics_error':
        current['official_metrics']['error'] = True
    elif change == 'metrics_missing':
        current['official_metrics'] = None
    elif change == 'valid_missing':
        current.pop('official_metrics_valid')
    else:
        current['official_metrics_valid'] = False
    observed = eval_outcomes.observe(current)
    assert not observed['milestone_metrics_valid'] and observed['official_metrics'] is None
    assert observed['infra_invalid'] or observed['grading_error']
    assert not observed['ungraded'] and not observed['boolean_valid'] and not observed['rewards_valid']


def test_old_failed_native_grade_is_not_reinterpreted_as_a_valid_zero():
    current = grade(complete=False, submitted=1, valid=False)
    current.pop('quality_kind')
    current.pop('official_metrics_valid')
    current.pop('infra_invalid')
    observed = eval_outcomes.observe(current)
    assert observed['grading_error'] and not observed['infra_invalid']
    assert not observed['milestone_metrics_valid'] and not observed['ungraded']


def test_report_keeps_per_itinerary_author_values_and_invalid_diagnostics_separate(tmp_path):
    cfg = config(tmp_path)
    cfg['repeats'] = 5
    plan = eval_plan.compile_plan(cfg)
    directory = evaluation.prepare(plan, tmp_path / 'run')
    complete = grade()
    partial = grade(complete=False, submitted=1)
    failed_retry = grade(complete=False, valid=False)
    failed_retry['coverage']['grading_errors'] = {'M3': 'evaluation_error'}
    infra = grade(complete=False, valid=False, infra=True)
    compile_failure = grade()
    compile_failure.update(resolved=False)
    compile_failure['official_metrics'].update(resolved=1, resolve_pct=50, score_1000=50)
    grades = [complete, partial, failed_retry, infra, compile_failure]
    with evaluation.database(directory) as connection:
        for job, current in zip(plan['jobs'], grades):
            result = dict(test_only=True, requests=1, grade=current)
            connection.execute("UPDATE jobs SET status='completed',result=? WHERE id=?", (json.dumps(result), job['id']))
    result = eval_report.report(directory)
    method = result['methods'][0]
    assert method['valid_milestone_metrics'] == 3
    assert method['valid_grades'] == method['resolved'] == method['valid_rewards'] == 0
    assert method['reward_metrics'] == {} and method['resolve_rate_valid_grades'] is None
    assert method['grading_errors'] == method['infra_invalid'] == 1 and method['ungraded'] == 0
    rows = method['milestone_metrics']
    assert len(rows) == 5
    for row, current, job in zip(rows, grades, plan['jobs']):
        assert row['official_metrics'] == current['official_metrics']
        assert row['coverage'] == current['coverage']
        assert row['job_id'] == job['id'] and row['task_id'] == job['task']['id']
    assert rows[1]['valid'] and not rows[1]['submission_complete'] and not rows[1]['scoring_complete']
    assert not rows[2]['valid'] and not rows[3]['valid']
    eval_report.write_report(directory)
    markup = (directory / 'report.html').read_text(encoding='utf-8')
    assert 'SWE-Milestone 官方指标' in markup and '无效结果的数值仅作诊断' in markup
    assert 'score_1000' in markup and '完整提交' in markup and '完整评分报告' in markup
    assert '官方 verifier 数值指标' not in markup
    saved = json.loads((directory / 'report.json').read_text(encoding='utf-8'))
    assert saved['methods'][0]['milestone_metrics'] == rows


def test_real_author_collector_only_isolated_entry(tmp_path):
    code = Path(os.environ.get('CTXPRESS_MILESTONE_AUTHOR_CODE', '~/swe/SWE-Milestone')).expanduser()
    if not (code / 'harness/e2e/collect_results.py').is_file():
        pytest.skip('set CTXPRESS_MILESTONE_AUTHOR_CODE to run the optional real author fixture check')
    output = tmp_path / 'author-collector.json'
    child = subprocess.run([sys.executable, '-I', '-S', '-B', str(Path(__file__).with_name('check_milestone_author.py')),
                            '--code', str(code), '--collector-only', '--output', str(output)],
                           env={}, capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stdout + child.stderr
    actual = json.loads(output.read_text(encoding='utf-8'))
    assert actual['model_calls'] == actual['containers_started'] == 0
    assert actual['test_only'] and not actual['real_run_verified']
    assert actual['cases']['budget_unsubmitted']['valid']
    assert not actual['cases']['newer_retry_error']['valid']
    assert actual['cases']['compile_failure']['valid']
