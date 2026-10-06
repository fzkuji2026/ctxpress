"""Distinguish boolean issue outcomes from numeric verifier metrics."""
from __future__ import annotations
import math


def observe(grade, result=None):
    grade = grade if isinstance(grade, dict) else {}
    from ctxpress.harness.runtime import execution_health
    execution_invalid = result is not None and execution_health.observe(result)['execution_invalid']
    milestone = grade.get('quality_kind') == 'milestone' or 'official_metrics' in grade
    official = grade.get('official_metrics')
    milestone_declared_valid = (milestone and grade.get('official_metrics_valid') is True and
                               grade.get('infra_invalid') is False and isinstance(official, dict) and
                               official.get('error') is False)
    infrastructure = bool(execution_invalid or grade.get('infra_invalid') or (grade.get('error') and 'failure_kind' not in grade))
    grading_error = bool(not infrastructure and
                         (grade.get('error') or grade.get('failure_kind') or
                          (grade.get('scoring_complete') is False and not milestone_declared_valid) or
                          (milestone and not milestone_declared_valid)))
    invalid = infrastructure or grading_error
    boolean = not invalid and not milestone and type(grade.get('resolved')) is bool and grade.get('infra_invalid') is False
    code = (not invalid and not milestone and grade.get('quality_kind')=='code_sample' and grade.get('infra_invalid') is False and
            type(grade.get('sample_passed')) is bool)
    rewards = grade.get('rewards')
    numeric = (not invalid and not milestone and grade.get('infra_invalid') is False and grade.get('valid_rewards') is True and
               isinstance(rewards, dict) and bool(rewards) and
               all(isinstance(key, str) and type(value) in (int, float) and math.isfinite(value) for key,value in rewards.items()))
    return dict(infra_invalid=infrastructure, grading_error=grading_error,
                boolean_valid=boolean, rewards_valid=bool(numeric),
                resolved=grade['resolved'] if boolean else None, rewards=rewards if numeric else {},
                code_sample_valid=code,sample_passed=grade.get('sample_passed') if code else None,
                milestone_metrics_valid=bool(milestone_declared_valid and not invalid),
                official_metrics=official if milestone_declared_valid and not invalid else None,
                ungraded=not invalid and not boolean and not numeric and not code and not milestone_declared_valid)


def add_rewards(metrics, rewards):
    for name, value in rewards.items():
        metric = metrics.setdefault(name, dict(count=0, sum=0.0, mean=None, minimum=None, maximum=None))
        metric['count'] += 1
        metric['sum'] += value
        metric['mean'] = metric['sum'] / metric['count']
        metric['minimum'] = value if metric['minimum'] is None else min(metric['minimum'], value)
        metric['maximum'] = value if metric['maximum'] is None else max(metric['maximum'], value)
