"""Collector-only regression against real author source; no models or containers.

Run with Python -I -S -B and --code pointing at SWE-Milestone. All scores below
are synthetic fixtures. Optional --pilot-grade reads existing failed evidence.
"""
import argparse
import copy
import json
import sys
import tempfile
from pathlib import Path


def check(code, pilot_grade=None):
    sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(code.resolve())]
    from harness.e2e import collect_results
    from ctxpress.harness.results import outcomes as eval_outcomes
    from ctxpress.benchmarks.milestone import grade as milestone_grade

    assert Path(collect_results.__file__).resolve() == code.resolve() / 'harness/e2e/collect_results.py'
    task = dict(id='fixture_repo', benchmark='swe-milestone', start_mode='task_start',
                initial_state={'fixture': True}, inputs=[],
                evaluation=dict(kind='native', active_milestones=['M1', 'M2', 'M3'], graded_milestones=['M2', 'M3']))
    counts = dict(total=2, passed=2, failed=0, error=0, skipped=0,
                  fail_to_pass_required=1, fail_to_pass_achieved=1,
                  none_to_pass_required=0, none_to_pass_achieved=0,
                  pass_to_pass_required=1, pass_to_pass_achieved=1,
                  pass_to_pass_failed=0, pass_to_pass_missing=0)
    passing = dict(resolved=True, test_summary=counts)
    failed = dict(resolved=False, test_summary=dict(counts, passed=1, failed=1, fail_to_pass_achieved=0))
    empty = dict(counts, total=0, passed=0, fail_to_pass_achieved=0, pass_to_pass_achieved=0)
    cases = {}

    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    with tempfile.TemporaryDirectory(prefix='ctxpress-author-scoring-') as temporary:
        root = Path(temporary)

        def run(name, entries, files, *, statuses=None, empty_dirs=()):
            # The direct collector runs on the original fixture; the reader
            # independently stages hash-bound documents in its private workspace.
            workspace = root / name
            trial = workspace / 'e2e_trial' / 'ctxpress'
            workspace.mkdir()
            (workspace / 'selected_milestone_ids.txt').write_text('M1\nM2\nM3\n', encoding='utf-8')
            (workspace / 'non-graded_milestone_ids.txt').write_text('M1\n', encoding='utf-8')
            summary = dict(repo_name=task['id'], agent_name='codex', total_milestones=3, results=entries)
            if statuses is not None:
                summary['milestone_status'] = statuses
            write(trial / 'evaluation/summary.json', summary)
            for key, values in files.items():
                for filename, row in values.items():
                    write(trial / 'evaluation' / key / filename,
                          dict(row, milestone_id=key.split('-retry')[0]))
            for key in empty_dirs:
                (trial / 'evaluation' / key).mkdir(parents=True, exist_ok=True)
            direct = collect_results.compute_repo_summary(workspace, ['ctxpress'], trial_type='e2e', prefer_filtered=True)
            grade = milestone_grade.read(task, code, trial)
            assert grade['official_metrics'] == direct, (name, grade, direct)
            assert grade['official_metrics']['graded'] == 2
            assert grade['model_calls'] == grade['containers_started'] == 0
            assert grade['real_run_verified'] is False
            observation = eval_outcomes.observe(grade)
            assert not any(observation[k] for k in ('boolean_valid', 'rewards_valid', 'code_sample_valid'))
            cases[name] = dict(valid=grade['official_metrics_valid'], scoring_complete=grade['scoring_complete'],
                               submission_complete=grade['submission_complete'], metrics=direct)
            return grade, observation

        def entries(*keys):
            return {key: dict(attempt=int(key.split('-retry')[1]) if '-retry' in key else 0,
                              dag_status='completed', eval_status='passed') for key in keys}

        def files(**rows):
            return {key: {'evaluation_result.json': row} for key, row in rows.items()}

        complete, observed = run('complete_success', entries('M1', 'M2', 'M3'), files(M1=passing, M2=passing, M3=passing))
        assert complete['scoring_complete'] and complete['submission_complete'] and complete['infra_invalid'] is False
        assert complete['resolved'] is True and observed['milestone_metrics_valid']
        assert complete['official_metrics']['resolve_pct'] == 100

        partial, observed = run('budget_unsubmitted', entries('M1', 'M2'), files(M1=passing, M2=passing),
                                statuses={'passed': ['M1', 'M2'], 'blocked': ['M3']})
        assert partial['official_metrics_valid'] and not partial['scoring_complete'] and not partial['submission_complete']
        assert partial['resolved'] is None and partial['failure_kind'] is None
        assert observed['milestone_metrics_valid'] and not observed['grading_error'] and not observed['ungraded']
        assert partial['official_metrics']['resolved'] == 1 and partial['official_metrics']['resolve_pct'] == 50
        # Do not replace author score variants by resolve_pct: even not_run
        # placeholders can have author-specific score edge cases.
        assert partial['coverage']['unsubmitted'] == ['M3']

        zero, observed = run('budget_no_submissions', {}, {}, statuses={'available': ['M1'], 'blocked': ['M2', 'M3']})
        assert zero['official_metrics_valid'] and observed['milestone_metrics_valid']
        assert not zero['scoring_complete'] and not zero['submission_complete']
        assert zero['official_metrics']['resolved'] == 0 and zero['official_metrics']['submitted'] == 0

        error_entries = entries('M2')
        error_entries['M2'].update(eval_status='error', error='ValueError: could not compile evaluator')
        bad, observed = run('scoring_error', error_entries, {})
        assert not bad['official_metrics_valid'] and observed['grading_error']
        assert bad['coverage']['grading_errors']['M2'] == 'evaluation_error'

        missing, observed = run('missing_report', entries('M2'), {})
        assert not missing['official_metrics_valid'] and observed['grading_error']
        assert missing['coverage']['grading_errors']['M2'] == 'missing_report'

        retry_entries = entries('M2', 'M2-retry1')
        retry_entries['M2-retry1'].update(eval_status='error', error='retry evaluator failed')
        retry, observed = run('newer_retry_error', retry_entries, files(M2=passing))
        assert not retry['official_metrics_valid'] and observed['grading_error']
        assert retry['milestones']['M2']['served_attempt'] is None
        assert retry['official_metrics']['resolved'] == 0

        retry_missing, observed = run('newer_retry_report_missing', entries('M2', 'M2-retry1'), files(M2=passing))
        assert not retry_missing['official_metrics_valid'] and observed['grading_error']
        assert retry_missing['milestones']['M2']['served_attempt'] is None

        orphan, observed = run('newer_directory_without_report', entries('M2'), files(M2=passing), empty_dirs=('M2-retry1',))
        assert not orphan['official_metrics_valid'] and observed['grading_error']
        assert orphan['coverage']['grading_errors']['M2'] == 'newer_attempt_missing_report'

        infra, observed = run('infra_invalid', entries('M2', 'M3'),
                              files(M2=passing, M3=dict(resolved=False, infra_invalid=True, test_summary=empty)))
        assert not infra['official_metrics_valid'] and infra['infra_invalid'] and observed['infra_invalid']
        assert not observed['grading_error'] and infra['official_metrics']['infra_invalid'] == 1
        assert infra['official_metrics']['graded'] == 2

        build, observed = run('compile_failure', entries('M2', 'M3'),
                              files(M2=passing, M3=dict(resolved=True, patch_status={'compilation_success': False}, test_summary=empty)))
        assert build['official_metrics_valid'] and build['scoring_complete'] and build['submission_complete']
        assert build['resolved'] is False and build['infra_invalid'] is False and observed['milestone_metrics_valid']
        assert build['official_metrics']['score_1000'] == 50 and build['official_metrics']['infra_invalid'] == 0

        filtered_files = files(M2=passing, M3=passing)
        filtered_files['M2-retry1'] = {'evaluation_result.json': failed, 'evaluation_result_filtered.json': passing}
        filtered, observed = run('filtered_retry_success', entries('M2', 'M2-retry1', 'M3'), filtered_files)
        assert filtered['official_metrics_valid'] and filtered['resolved'] is True and observed['milestone_metrics_valid']
        assert filtered['milestones']['M2']['served_attempt'] == 'M2-retry1'
        assert filtered['result_type_counts'] == {'filtered': 1, 'unfiltered': 1}
        assert filtered['official_metrics']['submitted'] == 2

        # Existing failed results must not become valid merely by adding a new
        # validity marker or by trusting the collector's normalized statuses.
        if pilot_grade is not None:
            legacy = json.loads(pilot_grade.read_text(encoding='utf-8'))
            legacy_observed = eval_outcomes.observe(legacy)
            assert legacy_observed['grading_error'] and not legacy_observed['milestone_metrics_valid']
            pilot_task = copy.deepcopy(task)
            pilot_task.update(id=legacy['raw_summary']['repo_name'])
            pilot_task['evaluation'].update(active_milestones=list(legacy['milestones']), graded_milestones=list(legacy['milestones']))
            pilot_trial = Path(legacy['reports']['evaluation/summary.json']['path']).parent.parent
            reread = milestone_grade.read(pilot_task, code, pilot_trial)
            assert not reread['official_metrics_valid'] and not reread['scoring_complete']
            assert len(reread['coverage']['grading_errors']) == 4
            assert all(value == 'evaluation_error' for value in reread['coverage']['grading_errors'].values())
            assert reread['official_metrics'] == legacy['official_metrics']
            cases['pilot3_remains_invalid'] = dict(valid=False, grading_errors=4)

    return dict(test_only=True, collector=str(Path(collect_results.__file__).resolve()), cases=cases,
                model_calls=0, containers_started=0, real_run_verified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code', required=True, type=Path)
    parser.add_argument('--pilot-grade', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = check(args.code, args.pilot_grade)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
