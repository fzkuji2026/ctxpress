"""Read-only, evidence-bound acceptance of a frozen task-start evaluation.

No model calls, grading, retries, resource deletion or inferred zero costs.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.core import processes

TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}


def read(path):
    path = Path(path).expanduser().absolute()
    if path.name.lower() == 'auth.json' or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('analysis input must be a regular non-credential file without symlinks')
    raw = path.read_bytes()
    return json.loads(raw), dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())


def write(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')


def exclusions(detail, path=None):
    """Explicit plan-bound review, with hashed evidence for each excluded job."""
    if path is None:
        return {}, None
    document, ref = read(path)
    if (document.get('schema') != 'ctxpress.eval.exclusions' or document.get('version') != 1 or
            document.get('plan_sha256') != detail['plan_sha256'] or
            document.get('code_sha256') != detail['code_sha256'] or
            not isinstance(document.get('review_note'), str) or not document['review_note'].strip()):
        raise ValueError('exclusions need schema/version, matching plan/source and a review_note')
    by_id = {job['job_id']: job for job in detail['jobs']}
    result = {}
    for entry in document['excluded']:
        identity = entry['job_id']
        if identity not in by_id or identity in result or not entry.get('reason') or not entry.get('evidence'):
            raise ValueError('invalid, duplicate or unsubstantiated exclusion')
        for proof in entry['evidence']:
            _, observed = read(proof['path'])
            if observed['sha256'] != proof['sha256']:
                raise ValueError('exclusion evidence changed')
        result[identity] = entry
    return result, ref


def inventory(kind):
    command = ['docker', 'ps', '-a'] if kind == 'containers' else ['docker', 'network', 'ls']
    command += ['--filter', 'label=ctxpress.run', '--format', '{{.ID}}\t{{.Label "ctxpress.run"}}']
    try:
        output = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        items = [line.split('\t') for line in output.stdout.splitlines()]
        if any(len(item) != 2 or not all(item) for item in items):
            raise ValueError('invalid Docker inventory')
        return dict(complete=True, items=items)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return dict(complete=False, items=[], error=type(error).__name__)


def cleanup(directory, detail, inspect=False):
    """Observe current owned resources; receipts alone never prove live cleanup."""
    root = Path(directory).resolve()
    db = root / 'jobs.sqlite'
    if db.is_symlink() or not db.is_file():
        raise ValueError('regular evaluation database required')
    with contextlib.closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN')
        plan = eval_plan.verify(json.loads(connection.execute("SELECT value FROM metadata WHERE key='plan'").fetchone()[0]), check_inputs=False)
        rows = {r['id']: dict(r) for r in connection.execute('SELECT id,status,attempt,pid,identity FROM jobs')}
    if plan['sha256'] != detail['plan_sha256'] or plan['code_sha256'] != detail['code_sha256']:
        raise ValueError('cleanup database belongs to another plan')
    observed = {kind: dict(complete=False, items=[], error='not_requested') for kind in ('containers', 'networks')}
    if inspect:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {kind: pool.submit(inventory, kind) for kind in observed}
            observed = {kind: future.result() for kind, future in futures.items()}
    jobs = []
    for job in detail['jobs']:
        state = rows.get(job['job_id'])
        row = dict(job_id=job['job_id'], verified=False, receipts=[], reasons=[])
        jobs.append(row)
        if (not state or state['status'] != job['status'] or state['attempt'] != job['attempt']):
            row['reasons'].append('state changed since comparison'); continue
        if state['status'] not in TERMINAL or type(state['attempt']) is not int or state['attempt'] < 1:
            row['reasons'].append('no terminal attempt'); continue
        label = plan['sha256'][:12] + '-' + job['job_id']
        row['worker_alive'] = bool(state['pid'] and processes.alive(state['pid'], state['identity']))
        folder = root / 'jobs' / job['job_id'] / f"attempt-{state['attempt']}"
        try:
            for receipt in sorted(folder.glob('resources-*.json')):
                data, ref = read(receipt)
                row['receipts'].append(dict(ref, cleaned=data.get('cleaned') is True))
        except (OSError, ValueError) as error:
            row['reasons'].append('invalid receipt: ' + type(error).__name__)
        for kind, value in observed.items():
            row[kind] = [identity for identity, owner in value['items'] if owner == label] if value['complete'] else None
        if not row['receipts'] or not all(r['cleaned'] for r in row['receipts']):
            row['reasons'].append('missing or incomplete cleanup receipts')
        if row['worker_alive']:
            row['reasons'].append('worker still alive')
        if any(row[k] is None or row[k] for k in observed):
            row['reasons'].append('owned resources remain or live inventory unavailable')
        row['verified'] = not row['reasons']
    return dict(checked_at=datetime.now(timezone.utc).isoformat(), inventory=observed, jobs=jobs)


def assess(detail, excluded, screened, clean, process_problems=None):
    process_problems = process_problems or {}
    clean_by_id = {row['job_id']: row for row in clean['jobs']}
    jobs = []
    for job in detail['jobs']:
        mechanism = job.get('mechanism_observation') or {}
        health = job.get('execution_health') or {}
        operations = mechanism.get('operations_on_successful_requests') or {}
        logged = mechanism.get('operation_logging_available') is True
        native_known = (mechanism.get('native_compaction_logging_available') is True and
                        mechanism.get('native_compaction_metadata_coverage') is True)
        eligible = (screened and job['status'] == 'completed' and job['quality_admissible'] and
                    job['job_id'] not in excluded and not health.get('execution_invalid') and
                    not process_problems.get(job['job_id']))
        jobs.append(dict(job_id=job['job_id'], method=job['method'], task_id=job['task_id'], repeat=job['repeat'],
            status=job['status'], official_quality_valid=job['quality_admissible'],
            cost_complete=job['api_cost_complete'], api_cost_usd=job['api_cost_usd'],
            api_cost_by_role=(job.get('bill') or {}).get('api_cost_by_role'),
            defect_reviewed=screened, excluded=job['job_id'] in excluded,
            exclusion_reason=excluded.get(job['job_id'], {}).get('reason'),
            process_problems=process_problems.get(job['job_id'], []),
            comparison_eligible=eligible, cleanup_verified=clean_by_id[job['job_id']]['verified'],
            operations_observed=any(v > 0 for v in operations.values()) if logged else None,
            native_compaction_observed=mechanism.get('completed_native_compactions', 0) > 0 if native_known else None,
            metrics=job['metrics']))
    by_id = {j['job_id']: j for j in jobs}
    candidates = []
    for candidate in detail['candidates']:
        pairs = []
        for pair in candidate['pairs']:
            base = by_id.get(pair['reference'].get('job_id'))
            other = by_id.get(pair['candidate'].get('job_id'))
            allowed = bool(base and other and base['comparison_eligible'] and other['comparison_eligible'])
            quality_ok = allowed and pair['quality_admissible']
            cost_ok = allowed and pair['api_cost_comparable']
            pairs.append(dict(task_id=pair['task_id'], repeat=pair['repeat'], quality_comparable=quality_ok,
                cost_comparable=cost_ok, metrics={k: dict(v, comparable=quality_ok and v['comparable'],
                    delta=v['delta'] if quality_ok and v['comparable'] else None) for k, v in pair['metrics'].items()},
                reference_cost=pair['reference_api_cost_usd'] if cost_ok else None,
                candidate_cost=pair['candidate_api_cost_usd'] if cost_ok else None))
        valid = [p for p in pairs if p['cost_comparable']]
        candidates.append(dict(method=candidate['method'], planned_pairs=len(pairs),
            quality_pairs=sum(p['quality_comparable'] for p in pairs), cost_pairs=len(valid),
            paired_reference_cost_subtotal=sum(p['reference_cost'] for p in valid) if valid else None,
            paired_candidate_cost_subtotal=sum(p['candidate_cost'] for p in valid) if valid else None,
            complete_cost_cohort=bool(pairs) and len(valid) == len(pairs), pairs=pairs))
    return jobs, candidates


def review(directory, reference, output, exclusion_file=None, inspect_resources=False, analysis=True):
    from ctxpress.harness.results import analysis as run_analysis
    root = Path(directory).expanduser().resolve()
    output = Path(output).expanduser().absolute()
    # Reports cannot become part of any frozen runtime, inputs or task artifacts.
    plan = eval_plan.load(root / 'plan.json', check_inputs=False)
    protected = [root / name for name in ('runtime', 'inputs', 'jobs')]
    protected += [Path(item['tree']['root']).resolve() for item in plan.get('input_trees', {}).values()]
    if (output.exists() or any(p.is_symlink() for p in (output, *output.parents)) or
            any(output == p or p in output.parents for p in protected)):
        raise ValueError('review output must be a new directory outside frozen evidence')
    from ctxpress.harness.results.task_compare import compare_results
    detail = compare_results(root, reference)
    excluded, exclusion_ref = exclusions(detail, exclusion_file)
    clean = cleanup(root, detail, inspect_resources)
    jobs, candidates = assess(detail, excluded, exclusion_ref is not None, clean)
    output.mkdir(parents=True, exist_ok=False)
    write(output / 'comparison.json', detail)
    _, detail_ref = read(output / 'comparison.json')
    write(output / 'cleanup.json', clean)
    summary = dict(schema='ctxpress.eval.review_input', version=1, reference=reference,
        source=detail['code_sha256'], families=[dict(benchmark=detail['benchmark'],
            detail_artifact=detail_ref['path'], detail_sha256=detail_ref['sha256'])])
    write(output / 'summary.json', summary)
    _, report_ref = read(output / 'summary.json')
    # Unreviewed jobs are deliberately excluded from method-effect aggregates.
    eligibility = dict(report=report_ref, jobs=[dict(benchmark=detail['benchmark'], job_id=j['job_id'],
        exclude_from_method_effect_comparison=not j['comparison_eligible']) for j in jobs])
    write(output / 'defect-eligibility.json', eligibility)
    analyzed = None
    if analysis:
        analyzed, tables = run_analysis.analyze(output / 'summary.json', output / 'defect-eligibility.json')
        problems = {j['job_id']: j['problems'] for f in analyzed['families'] for j in f['jobs']}
        jobs, candidates = assess(detail, excluded, exclusion_ref is not None, clean, problems)
        run_analysis.write(analyzed, tables, output / 'analysis', figures=False)
    final_eligibility = dict(report=report_ref, jobs=[dict(benchmark=detail['benchmark'], job_id=j['job_id'],
        exclude_from_method_effect_comparison=not j['comparison_eligible']) for j in jobs])
    write(output / 'comparison-eligibility.json', final_eligibility)
    evidence = [read(output / name)[1] for name in ('comparison.json', 'cleanup.json', 'summary.json', 'defect-eligibility.json', 'comparison-eligibility.json')]
    from ctxpress.harness.results.paired_statistics import candidate_statistics
    for candidate in candidates:
        candidate['statistics'] = candidate_statistics(candidate)
    result = dict(schema='ctxpress.eval.review', version=1, checked_at=datetime.now(timezone.utc).isoformat(),
        directory=str(root), benchmark=detail['benchmark'], plan_sha256=detail['plan_sha256'],
        code_sha256=detail['code_sha256'], analysis_code_sha256=eval_plan.fingerprint(),
        process_analysis_checked=analysis, exclusions=exclusion_ref, artifacts=evidence, jobs=jobs, candidates=candidates,
        counts=dict(status=dict(Counter(j['status'] for j in jobs)), planned=len(jobs),
            quality_valid=sum(j['official_quality_valid'] for j in jobs),
            costs_complete=sum(j['cost_complete'] for j in jobs),
            eligible=sum(j['comparison_eligible'] for j in jobs),
            cleanups_verified=sum(j['cleanup_verified'] for j in jobs)),
        limits=['A reviewed job is not proof of absence of other defects.',
                'Quality, accounting, cleanup and method activation are independent evidence dimensions.',
                'Only original frozen task/repeat pairs; no retries, imputation or cross-benchmark aggregation.',
                'Synthetic mechanism checks and technical repair runs do not replace formal outcomes.'])
    write(output / 'review.json', result)
    lines = ['# Evaluation review', '', f"Benchmark: {detail['benchmark']}", '',
             '| Status | Count |', '|---|---|']
    lines += [f'| {k} | {v} |' for k, v in result['counts'].items() if k != 'status']
    lines += ['', 'Unknown or unreviewed evidence is not a pass. See review.json for job-level reasons.',
              'Process analysis: analysis/report.md.' if analysis else 'Process analysis was not requested.']
    lines += ['', '## Exploratory paired statistics', '',
              'Repeats are averaged within each task before resampling. Incomplete task blocks are excluded.',
              'Intervals and unadjusted tests do not establish quality non-inferiority.', '',
              '| Method | Complete tasks | Mean cost delta (USD/task) | 95% task bootstrap interval |',
              '|---|---:|---:|---|']
    for candidate in candidates:
        stats = candidate['statistics']['cost']
        interval = stats['confidence_interval']
        band = f"[{interval['lower']:.6g}, {interval['upper']:.6g}]" if interval else 'unavailable'
        mean = f"{stats['mean_delta']:.6g}" if stats['mean_delta'] is not None else 'unknown'
        lines.append(f"| {candidate['method']} | {stats['complete_tasks']} | {mean} | {band} |")
    (output / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return result
