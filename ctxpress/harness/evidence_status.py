"""Capability is catalog metadata; verification always names its run and scope."""
from pathlib import Path
from ctxpress import benchmarks
from ctxpress.harness.eval_review import read


def describe(start_mode=None, evidence=()):
    rows = benchmarks.describe(start_mode)
    by_name = {row['name']: row for row in rows}
    for row in rows:
        row['verification_evidence'] = []
        row['verification_scope'] = 'Static capability is not a local environment or real-run check.'
    for path in evidence:
        review, ref = read(path)
        if review.get('schema') != 'ctxpress.eval.review' or review.get('version') != 1:
            raise ValueError('evidence must be a ctxpress eval review bundle')
        bound = {}
        for artifact in review['artifacts']:
            value, observed = read(artifact['path'])
            if observed['sha256'] != artifact['sha256']:
                raise ValueError('review artifact changed')
            bound[Path(artifact['path']).name] = value
        detail = bound.get('comparison.json')
        if not detail or any(review[k] != detail[k] for k in ('benchmark', 'code_sha256', 'plan_sha256')):
            raise ValueError('review comparison binding differs')
        if start_mode is not None and detail.get('start_mode', 'task_start') != start_mode:
            raise ValueError('review start mode differs from requested catalog scope')
        if detail['benchmark'] not in by_name:
            raise ValueError('review benchmark is outside requested catalog scope')
        verified = [job for job in detail['jobs'] if job['status'] == 'completed' and job['quality_admissible']]
        by_name[detail['benchmark']]['verification_evidence'].append(dict(
            artifact=ref, plan_sha256=detail['plan_sha256'], code_sha256=detail['code_sha256'],
            benchmark_release=detail.get('benchmark_release'), start_mode=detail.get('start_mode', 'task_start'),
            resources_bound=bool(detail.get('task_resource_manifest_sha256') and
                                 detail.get('observed_runtime_code_sha256') == detail['code_sha256']),
            real_tasks_verified=len(verified), planned_jobs=len(detail['jobs']),
            task_ids=sorted({j['task_id'] for j in verified}),
            methods=[dict(method=label,
                verified_jobs=sum(j['method'] == label for j in verified),
                operation_observed_jobs=sum(j['method'] == label and
                    (j.get('mechanism_observation') or {}).get('operation_logging_available') is True and
                    any(n > 0 for n in ((j.get('mechanism_observation') or {}).get('operations_on_successful_requests') or {}).values())
                    for j in verified)) for label in dict.fromkeys(j['method'] for j in detail['jobs'])],
            scope='Evidence for these frozen tasks/methods only; not current machine readiness or full benchmark coverage.'))
    return rows
