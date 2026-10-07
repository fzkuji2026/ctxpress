"""Exploratory paired inference on reviewed, complete task blocks only.

Repeated runs of one task are averaged first. Resampling is over tasks, never
over repeats or requests. Does not establish non-inferiority, equal seeds,
random assignment or family-wide generalization.
"""
from __future__ import annotations

from collections import defaultdict
import itertools
import math
import random
import statistics


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def quantile(values, q):
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lo = int(position)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def summarize(pairs, metric="cost_usd", *, samples=2000, seed=0):
    if type(samples) is not int or samples < 100:
        raise ValueError("at least 100 bootstrap samples required")
    grouped = defaultdict(list)
    seen = set()
    for pair in pairs:
        key = (pair['task_id'], pair['repeat'])
        if key in seen:
            raise ValueError("duplicate task/repeat pair")
        seen.add(key)
        if metric == "cost_usd":
            a, b = pair.get('reference_cost'), pair.get('candidate_cost')
            valid = pair.get('cost_comparable') is True and finite(a) and finite(b)
            delta = b - a if valid else None
        else:
            entry = pair.get('metrics', {}).get(metric, {})
            a, b = entry.get('reference'), entry.get('candidate')
            valid = pair.get('quality_comparable') is True and entry.get('comparable') is True and finite(a) and finite(b)
            delta = b - a if valid else None
        if delta is not None and not math.isfinite(delta):
            delta = None
        grouped[pair['task_id']].append(delta)
    effects = [statistics.mean(values) for task, values in sorted(grouped.items()) if all(v is not None for v in values)]
    n = len(effects)
    report = dict(metric=metric, estimand='equal-task mean of candidate-minus-reference paired differences',
        planned_tasks=len(grouped), complete_tasks=n, excluded_incomplete_tasks=len(grouped) - n,
        complete_pairs=sum(len(v) for v in grouped.values() if all(x is not None for x in v)),
        mean_delta=statistics.mean(effects) if effects else None,
        confidence_interval=None, sign_flip_p=None, inference_available=n >= 2, seed=seed,
        limitations=['Only tasks with every planned repeat comparable are included; no imputation.',
            'Tasks are the sampling units; repeated runs of one itinerary do not add independent tasks.',
            'Exploratory percentile bootstrap and unadjusted sign-flip test; multiple comparisons are not corrected.',
            'Sign-flip inference assumes exchangeable signs under the null; pairing does not prove matched random seeds.',
            'No claim of quality non-inferiority or representativeness of the full benchmark.'])
    if n < 2:
        report['unavailable_reason'] = 'fewer than two complete independent task blocks'
        return report
    rng = random.Random(seed)
    boot = [statistics.mean(rng.choices(effects, k=n)) for _ in range(samples)]
    report['confidence_interval'] = dict(level=.95, method='task-cluster percentile bootstrap', samples=samples,
                                       lower=quantile(boot, .025), upper=quantile(boot, .975))
    # Scaling does not change a sign-flip test; it avoids overflowing sums.
    scale = max(abs(v) for v in effects) or 1
    scaled = [v / scale for v in effects]
    observed = abs(sum(scaled))
    exact = n <= 16
    signs = itertools.product((-1, 1), repeat=n) if exact else (
        [rng.choice((-1, 1)) for _ in range(n)] for _ in range(samples))
    exceed, total = 0, 0
    for assignment in signs:
        total += 1
        exceed += abs(sum(s * d for s, d in zip(assignment, scaled))) >= observed - 1e-12
    report['sign_flip_p'] = exceed / total if exact else (exceed + 1) / (total + 1)
    report['sign_flip_method'] = 'exact two-sided' if exact else 'Monte Carlo two-sided (+1 correction)'
    report['small_sample'] = n < 10
    return report


def candidate_statistics(candidate):
    pairs = candidate['pairs']
    metrics = sorted({key for pair in pairs for key in pair.get('metrics', {})})
    return dict(cost=summarize(pairs), quality={m: summarize(pairs, m) for m in metrics})
