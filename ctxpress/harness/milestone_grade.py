"""Read one native trial with the frozen author's collector, without running tests."""
from __future__ import annotations
import json, re, tempfile
from pathlib import Path
from ctxpress.harness import eval_plan, eval_trees
from ctxpress.harness.task import validate


def read(task, code, trial):
    validate(task)
    if task['benchmark']!='swe-milestone' or task['start_mode']!='task_start':
        raise ValueError('native result reader requires a task-start SWE-Milestone itinerary')
    from harness.e2e import collect_results
    code=Path(code).resolve();trial=Path(trial).absolute()
    if Path(collect_results.__file__).resolve()!=code/'harness/e2e/collect_results.py':
        raise ValueError('native result reader imported an undeclared collector')
    active=task['evaluation']['active_milestones'];graded=task['evaluation']['graded_milestones']
    if not active or len(set(active))!=len(active) or not set(graded)<=set(active):
        raise ValueError('native result reader requires an explicit itinerary selection')
    for mid in active:
        if len(eval_trees.relative(mid).parts)!=1 or re.search(r'-retry\d+$',mid):
            raise ValueError('native itinerary ID conflicts with collector retry directories')
    if trial.is_symlink() or not trial.is_dir():
        raise ValueError('native result reader requires a regular trial directory')
    evidence={}
    def document(relative, destination):
        source=trial/eval_trees.relative(relative)
        if any(p.is_symlink() for p in (source,*source.parents) if p==trial or trial in p.parents):
            raise ValueError('native trial results cannot use symbolic links')
        digest=eval_plan.file_sha256(source)
        content=source.read_bytes();raw=json.loads(content)
        if not isinstance(raw,dict):raise ValueError('native result JSON must be an object')
        if eval_plan.file_sha256(source)!=digest:raise ValueError('native result changed while reading')
        destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(content)
        if eval_plan.file_sha256(destination)!=digest:raise ValueError('native result copy differs from its evidence hash')
        evidence[relative]=dict(path=str(source),sha256=digest)
        return raw
    def base_id(key):
        if key in active:return key,0
        matches=[(mid,re.fullmatch(re.escape(mid)+r'-retry([1-9][0-9]*)',key)) for mid in active]
        matches=[(mid,int(match.group(1))) for mid,match in matches if match]
        if len(matches)!=1:raise ValueError('native trial contains an unselected or ambiguous milestone')
        return matches[0]
    def counters(raw):
        values=raw.get('test_summary',{})
        if not isinstance(values,dict):raise ValueError('native test_summary must be an object')
        for value in values.values():
            if type(value) is not int or value<0:raise ValueError('native test counters must be nonnegative integers')
        if 'resolved' in raw and type(raw['resolved']) is not bool:
            raise ValueError('native resolved outcome must be boolean')
        if 'infra_invalid' in raw and type(raw['infra_invalid']) is not bool:
            raise ValueError('native infrastructure outcome must be boolean')
    summary_file=trial/'evaluation/summary.json'
    if not summary_file.is_file():
        return dict(resolved=None,infra_invalid=False,failure_kind='grading_error',
            error='native summary missing',official_metrics=None,quality_kind='milestone',
            official_metrics_valid=False,scoring_complete=False,submission_complete=False)
    # Native APIs expect workspace/e2e_trial/<trial>. Stage selected result
    # documents there rather than expose original grading paths to the collector.
    with tempfile.TemporaryDirectory(prefix='ctxpress-native-results-') as temporary:
        workspace=Path(temporary)/task['id'];copied=workspace/'e2e_trial'/'ctxpress'
        (workspace/'selected_milestone_ids.txt').parent.mkdir(parents=True)
        (workspace/'selected_milestone_ids.txt').write_text(''.join(mid+'\n' for mid in active),encoding='utf-8')
        (workspace/'non-graded_milestone_ids.txt').write_text(''.join(mid+'\n' for mid in active if mid not in graded),encoding='utf-8')
        raw=document('evaluation/summary.json',copied/'evaluation/summary.json')
        if (raw.get('repo_name')!=task['id'] or raw.get('agent_name')!='codex' or
                type(raw.get('total_milestones')) is not int or raw['total_milestones']!=len(active) or
                not isinstance(raw.get('results'),dict)):
            raise ValueError('native summary differs from selected Codex itinerary')
        latest={}
        for key,entry in raw['results'].items():
            mid,attempt=base_id(key)
            if not isinstance(entry,dict) or type(entry.get('attempt',attempt)) is not int or entry.get('attempt',attempt)!=attempt:
                raise ValueError('native summary attempt differs from its result key')
            counters(entry)
            if mid not in latest or attempt>latest[mid][0]:latest[mid]=(attempt,entry)
        evaluation=trial/'evaluation'
        documents={};directories={}
        for directory in sorted(evaluation.iterdir()):
            if not directory.is_dir():continue
            if directory.is_symlink():raise ValueError('native result directories cannot use symbolic links')
            mid,attempt=base_id(directory.name)
            directories.setdefault(mid,[]).append(attempt)
            for name in ('evaluation_result.json','evaluation_result_filtered.json'):
                source=directory/name
                if not source.exists() and not source.is_symlink():continue
                relative='evaluation/'+directory.name+'/'+name
                row=document(relative,copied/'evaluation'/directory.name/name)
                counters(row)
                if row.get('milestone_id')!=mid:raise ValueError('native result does not bind its milestone identity')
                if type(row.get('resolved')) is not bool or 'total' not in row.get('test_summary',{}):
                    raise ValueError('native evaluation file requires a boolean verdict and test counts')
                documents[(directory.name,name)]=row
        if (trial/'agent_stats.json').is_file():document('agent_stats.json',copied/'agent_stats.json')
        results,types=collect_results.load_e2e_results(workspace,'ctxpress',prefer_filtered=True)
        cells=collect_results.authoritative_cells(copied/'evaluation',prefer_filtered=True)
        metrics=collect_results.compute_repo_summary(workspace,['ctxpress'],trial_type='e2e',prefer_filtered=True)
        served={mid:path.name for mid,path in cells.items()}
        # The collector normalizes summary errors (including compile-looking
        # exceptions) into scored failures. Check the original latest attempt
        # as well, so that normalization cannot certify a missing/failed grader.
        grading_errors={};infra_invalid=[];unsubmitted=[]
        claimed=set()
        for name in ('completed','failed','errors'):
            claimed.update(raw.get(name,[]) if isinstance(raw.get(name),list) else [])
        statuses=raw.get('milestone_status',{})
        if isinstance(statuses,dict):
            for name in ('passed','failed','error','submitted'):
                claimed.update(statuses.get(name,[]) if isinstance(statuses.get(name),list) else [])
        for mid in active:
            attempt,entry=latest.get(mid,(-1,{}))
            cell=served.get(mid)
            cell_attempt=base_id(cell)[1] if cell else -1
            row=(documents.get((cell,'evaluation_result_filtered.json')) or
                 documents.get((cell,'evaluation_result.json'))) if cell else None
            current=entry if attempt>=cell_attempt else (row or {})
            if (current.get('error') or current.get('failure_kind') or
                    current.get('eval_status')=='error' or
                    (row and (row.get('error') or row.get('failure_kind') or row.get('eval_status')=='error'))):
                grading_errors[mid]='evaluation_error'
            if collect_results.is_infra_invalid(results.get(mid,{})):
                infra_invalid.append(mid)
            if any(value>max(attempt,cell_attempt) for value in directories.get(mid,[])):
                grading_errors[mid]='newer_attempt_missing_report'
            if cell is None:
                attempted=(bool(entry) and entry.get('eval_status')!='not_run') or mid in claimed or mid in directories
                if attempted:grading_errors.setdefault(mid,'missing_report')
                else:unsubmitted.append(mid)
        valid=bool(graded) and metrics.get('error') is False and not grading_errors and not infra_invalid and not raw.get('error')
        # Preserve the old meaning: every graded node has valid served evidence.
        # Budget-limited unsubmitted nodes can still have valid author metrics.
        complete=valid and all(mid in served for mid in graded)
        submission_complete=bool(graded) and not any(mid in unsubmitted for mid in graded)
        outcomes={mid:dict(resolved=bool(collect_results.is_resolved(results[mid])) if mid in results else None,
            infra_invalid=bool(collect_results.is_infra_invalid(results[mid])) if mid in results else None,
            served_attempt=served.get(mid),raw_result=results.get(mid)) for mid in active}
        # Keep the author's values and denominator unchanged, even for invalid
        # trials. Validity gates their use; coverage describes unfinished work.
        for item in evidence.values():
            if eval_plan.file_sha256(item['path'])!=item['sha256']:raise ValueError('native result changed during collection')
        return dict(resolved=all(outcomes[mid]['resolved'] for mid in graded) if complete else None,
            infra_invalid=bool(infra_invalid),failure_kind=None if valid else 'grading_error',
            quality_kind='milestone',official_metrics_valid=valid,scoring_complete=complete,
            submission_complete=submission_complete,
            coverage=dict(graded=len(graded),submitted=metrics.get('submitted'),
                served_graded=sum(mid in served for mid in graded),unsubmitted=unsubmitted,
                grading_errors=grading_errors,infra_invalid=infra_invalid),
            official_metrics=metrics,milestones=outcomes,result_type_counts=types,
            reports=evidence,raw_summary=raw,collector='harness.e2e.collect_results',
            model_calls=0,containers_started=0,real_run_verified=False)
