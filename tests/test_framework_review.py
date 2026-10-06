import copy
import json
from pathlib import Path

import pytest

from ctxpress.harness import eval_review as review
from ctxpress.harness import evidence_status


def detail():
    jobs = [dict(job_id=name, method=method, task_id='t', repeat=0, attempt=1, status='completed',
                 quality_admissible=True, api_cost_complete=True, api_cost_usd=cost, metrics={'resolved': False},
                 mechanism_observation=dict(operation_logging_available=True, operations_on_successful_requests={'placeholder': n}))
            for name, method, cost, n in [('a', 'base', 0., 0), ('b', 'compressed', 2., 1)]]
    pair = dict(task_id='t', repeat=0, reference=jobs[0], candidate=jobs[1], quality_admissible=True,
                api_cost_comparable=True, reference_api_cost_usd=0., candidate_api_cost_usd=2.,
                metrics={'resolved': dict(reference=False, candidate=False, comparable=True, delta=0)})
    return dict(benchmark='swe-bench-verified', reference='base', plan_sha256='a'*64, code_sha256='b'*64,
                observed_runtime_code_sha256='b'*64, task_resource_manifest_sha256='c'*64,
                jobs=jobs, candidates=[dict(method='compressed', pairs=[pair])])


def clean(d):
    return dict(jobs=[dict(job_id=j['job_id'], verified=True) for j in d['jobs']])


def test_unreviewed_and_excluded_never_become_comparison_pairs():
    d = detail()
    jobs, pairs = review.assess(d, {}, False, clean(d))
    assert all(j['official_quality_valid'] for j in jobs)  # A valid unsolved task is retained.
    assert not any(j['comparison_eligible'] for j in jobs)
    assert pairs[0]['quality_pairs'] == pairs[0]['cost_pairs'] == 0
    jobs, pairs = review.assess(d, {'b': {'reason': 'host defect'}}, True, clean(d))
    assert jobs[1]['excluded'] and jobs[1]['api_cost_usd'] == 2
    assert pairs[0]['paired_candidate_cost_subtotal'] is None


def test_quality_and_cost_coverage_are_independent_and_zero_is_known():
    d = detail()
    jobs, pairs = review.assess(d, {}, True, clean(d))
    assert pairs[0]['paired_reference_cost_subtotal'] == 0
    d['jobs'][1].update(api_cost_complete=False, api_cost_usd=None)
    d['candidates'][0]['pairs'][0].update(api_cost_comparable=False, candidate_api_cost_usd=None)
    jobs, pairs = review.assess(d, {}, True, clean(d))
    assert pairs[0]['quality_pairs'] == 1 and pairs[0]['cost_pairs'] == 0
    assert jobs[0]['operations_observed'] is False and jobs[1]['operations_observed'] is True
    assert jobs[0]['native_compaction_observed'] is None


def test_exclusions_are_plan_source_bound_with_hashed_evidence(tmp_path):
    d = detail()
    proof = tmp_path/'proof.json'; review.write(proof, {'reproduced': True})
    _, proof_ref = review.read(proof)
    manifest = dict(schema='ctxpress.eval.exclusions', version=1, plan_sha256=d['plan_sha256'],
                    code_sha256=d['code_sha256'], review_note='Reviewed known runtime defect',
                    excluded=[dict(job_id='b', reason='reproduced', evidence=[proof_ref])])
    path = tmp_path/'exclude.json'; review.write(path, manifest)
    assert set(review.exclusions(d, path)[0]) == {'b'}
    proof.write_text('{}')
    with pytest.raises(ValueError, match='evidence changed'):
        review.exclusions(d, path)
    manifest['plan_sha256'] = 'd'*64; path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='matching plan/source'):
        review.exclusions(d, path)


def test_docker_failure_is_unknown_not_clean(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('unavailable')
    monkeypatch.setattr(review.subprocess, 'run', fail)
    assert review.inventory('containers')['complete'] is False


def test_review_bundle_and_scoped_catalog_evidence(tmp_path, monkeypatch):
    from ctxpress.harness import evaluation
    d = detail()
    monkeypatch.setattr(review.eval_plan, 'load', lambda *a, **kw: {})
    monkeypatch.setattr(evaluation, 'compare_results', lambda *a, **kw: d)
    monkeypatch.setattr(review, 'cleanup', lambda *a, **kw: clean(d))
    out = tmp_path/'review'
    result = review.review(tmp_path/'run', 'base', out, analysis=False)
    assert result['counts']['eligible'] == 0
    rows = evidence_status.describe('task_start', [out/'review.json'])
    state = next(x for x in rows if x['name'] == d['benchmark'])['verification_evidence'][0]
    assert state['real_tasks_verified'] == 2 and state['resources_bound'] is True
    assert state['task_ids'] == ['t']
    (out/'comparison.json').write_text('{}')
    with pytest.raises(ValueError, match='artifact changed'):
        evidence_status.describe('task_start', [out/'review.json'])
    with pytest.raises(ValueError, match='new directory'):
        review.review(tmp_path/'run', 'base', out, analysis=False)


def test_secret_and_symlink_inputs_refused(tmp_path):
    with pytest.raises(ValueError):
        review.read(tmp_path/'auth.json')
    proof = tmp_path/'proof.json'; proof.write_text('{}')
    alias = tmp_path/'alias'
    try:
        alias.symlink_to(proof)
    except OSError:
        pytest.skip('symlink creation not available')
    with pytest.raises(ValueError):
        review.read(alias)


def test_process_self_check_failure_disqualifies_comparison_without_erasing_raw_bill():
    d = detail()
    jobs, pairs = review.assess(d, {}, True, clean(d), {'b': ['missing request number']})
    assert jobs[1]['cost_complete'] and jobs[1]['api_cost_usd'] == 2
    assert not jobs[1]['comparison_eligible'] and jobs[1]['process_problems']
    assert pairs[0]['cost_pairs'] == pairs[0]['quality_pairs'] == 0
