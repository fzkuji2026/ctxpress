import pytest

from ctxpress.harness.results.paired_statistics import summarize


def pair(task, repeat=0, a=10, b=9, valid=True):
    return dict(task_id=task, repeat=repeat, reference_cost=a, candidate_cost=b, cost_comparable=valid,
                quality_comparable=valid, metrics=dict(resolved=dict(reference=0, candidate=1, comparable=valid)))


def test_one_itinerary_with_many_repeats_is_not_independent_tasks():
    report = summarize([pair('navidrome', repeat=i) for i in range(20)])
    assert report['mean_delta'] == -1 and report['complete_tasks'] == 1
    assert report['confidence_interval'] is report['sign_flip_p'] is None


def test_exact_paired_test_and_constant_bootstrap():
    rows = [pair(str(i)) for i in range(4)]
    report = summarize(rows)
    assert report['sign_flip_p'] == 2 / 16
    assert report['confidence_interval']['lower'] == report['confidence_interval']['upper'] == -1
    assert report == summarize(list(reversed(rows)))
    quality = summarize(rows, 'resolved')
    assert quality['mean_delta'] == 1


def test_missing_pair_excludes_entire_task_not_just_bad_repeat():
    rows = [pair('a'), pair('a', 1, valid=False), pair('b'), pair('c', a=None)]
    report = summarize(rows)
    assert report['complete_tasks'] == report['complete_pairs'] == 1
    assert report['excluded_incomplete_tasks'] == 2
    assert report['mean_delta'] == -1


def test_tasks_have_equal_weight_regardless_of_repeat_count():
    rows = [pair('a', i, b=9) for i in range(3)] + [pair('b', b=7)]
    report = summarize(rows)
    assert report['mean_delta'] == -2


def test_duplicate_and_nonfinite_measurements_are_not_accepted():
    with pytest.raises(ValueError, match='duplicate'):
        summarize([pair('a'), pair('a')])
    for value in (True, float('inf'), float('nan')):
        assert summarize([pair('a', b=value)])['complete_tasks'] == 0


def test_monte_carlo_pvalue_is_reproducible_and_never_zero():
    rows = [pair(str(i)) for i in range(17)]
    report = summarize(rows, samples=100)
    assert report['sign_flip_p'] > 0 and report == summarize(rows, samples=100)
