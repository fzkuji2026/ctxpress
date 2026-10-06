"""Descriptive paired evidence for the declared per-boundary quality constraint.

Complete observed cohorts do not establish population noninferiority. API costs
include recorded main, summary and native compaction requests, not the complete experiment bill.
"""
from __future__ import annotations
import json, math
from pathlib import Path
from ctxpress.harness import eval_plan
from ctxpress.live import usage as eval_usage


def _assessment(plan,job):
    if job is None:
        return dict(reasons=['missing planned job'],result=None,quality=None,cost=None,usage=None)
    result = json.loads(job['result']) if job.get('result') is not None else None
    reasons = []
    if job['status']!='completed' or result is None:
        return dict(reasons=[job['status']],result=result,quality=None,cost=None,usage=None)
    grade = result.get('grade') or {}
    if result.get('test_only') or grade.get('test_only'):
        reasons.append('synthetic execution')
    if grade.get('infra_invalid') is not False or type(grade.get('resolved')) is not bool or grade.get('error'):
        reasons.append('missing or invalid official grade')
    if result.get('model')!=plan['config']['model'] or result.get('reasoning')!=plan['config'].get('reasoning','low'):
        reasons.append('unverified model or reasoning')
    spec = json.loads(job['spec'])
    point = spec['boundary']
    context = result.get('boundary_context_evidence') or {}
    measured = context.get('catalog_prefix_tokens')
    if (context.get('declared')!=point['context_tokens'] or isinstance(measured,bool) or
        not isinstance(measured,(int,float)) or not math.isfinite(measured) or measured<128000):
        reasons.append('unverified formal boundary context')
    environment = plan.get('environment_snapshot')
    evidence = result.get('environment') or {}
    if not environment:
        reasons.append('environment manifest not declared')
    elif (evidence.get('manifest_sha256')!=environment['sha256'] or evidence.get('workspace_frozen') is not True or
          evidence.get('base_image_id')!=environment['images']['base']['id'] or
          evidence.get('boundary_image_id')!=environment['images']['boundaries'][f"{point['n']}:{point['j']}"]['id']):
        reasons.append('unverified execution environment')
    grading = plan.get('grading_snapshot')
    if not grading:
        reasons.append('grading manifest not declared')
    elif grade.get('grading_manifest_sha256')!=grading['sha256']:
        reasons.append('unverified grading environment')
    else:
        try:
            path = Path(grade['report'])
            if eval_plan.file_sha256(path)!=grade['report_sha256']:
                raise ValueError()
            raw = json.loads(path.read_text(encoding='utf-8'))
            binding = raw.get('evaluation_environment') or {}
            integrity = raw.get('snapshot_integrity') or {}
            record = grading['boundaries'][f"{point['n']}:{point['j']}"]
            files = grading['trees']['trials']['files']
            if (raw.get('resolved') is not grade.get('resolved') or raw.get('infra_invalid') is not False or
                binding.get('harness_revision')!='ctxpress-frozen:'+grading['sha256'] or raw.get('test_only') or
                raw.get('milestone_id')!=record['milestone'] or grade.get('milestone_id')!=record['milestone'] or
                not grade.get('graded_commit') or binding.get('snapshot_agent_tag_commit')!=grade['graded_commit'] or
                binding.get('repo_config_binding_mode')!='trial-pinned' or binding.get('repo_config_sha256')!=files[record['repo_config']] or
                binding.get('runtime_policy_binding_mode')!='trial-pinned' or binding.get('runtime_policy_mode')!='protected' or
                binding.get('runtime_policy_sha256')!=files[record['runtime_policy']] or
                integrity.get('ok') is not True or integrity.get('legacy_unverified') is not False):
                raise ValueError()
        except (OSError,KeyError,ValueError,TypeError):
            reasons.append('missing, changed or unbound official report')
    usage = eval_usage.analyze(result, plan['config']['model'])
    return dict(reasons=reasons,result=result,quality=None if reasons else grade['resolved'],
                cost=eval_usage.price(usage,plan['config'].get('prices')),usage=usage)


def compare(plan,jobs):
    setting = plan['config'].get('comparison')
    if setting is None:
        return None
    reference = setting['reference']
    lookup,duplicates = {},set()
    for job in jobs:
        spec = json.loads(job['spec'])
        key = (spec['label'],spec['boundary']['id'],spec['repeat'])
        if key in lookup:
            duplicates.add(key)
        lookup[key]=job
    assessment = {}
    for spec in plan['jobs']:
        key = (spec['label'],spec['boundary']['id'],spec['repeat'])
        if key in lookup and json.loads(lookup[key]['spec'])!=spec:
            value = dict(reasons=['job specification differs from the approved plan'],result=None,quality=None,cost=None,usage=None)
        else:
            value = _assessment(plan,lookup.get(key))
        if key in duplicates:
            value['reasons'].append('duplicate cohort member'); value['quality']=None
        assessment[key]=value
    labels = list(dict.fromkeys(spec['label'] for spec in plan['jobs']))
    candidates = []
    for label in labels:
        if label==reference:
            continue
        pairs,boundaries = [],[]
        for point in plan['config']['boundaries']:
            cohort = []
            repeats = sorted(spec['repeat'] for spec in plan['jobs'] if spec['label']==reference and spec['boundary']['id']==point['id'])
            for repeat in repeats:
                base = assessment[(reference,point['id'],repeat)]
                candidate = assessment.get((label,point['id'],repeat),dict(reasons=['missing planned candidate'],quality=None,cost=None,usage=None,result=None))
                reasons = ['reference: '+reason for reason in base['reasons']] + ['candidate: '+reason for reason in candidate['reasons']]
                pair = dict(boundary=point['id'],repeat=repeat,reference_quality=base['quality'],candidate_quality=candidate['quality'],
                            reasons=reasons,quality_admissible=not reasons,
                            reference_api_cost_usd=base['cost'],candidate_api_cost_usd=candidate['cost'],
                            reference_usage=base['usage'],candidate_usage=candidate['usage'])
                if not reasons and base['result']['boundary_context_evidence']['catalog_prefix_tokens']!=candidate['result']['boundary_context_evidence']['catalog_prefix_tokens']:
                    pair['reasons'].append('boundary context differs across the pair');pair['quality_admissible']=False
                cohort.append(pair);pairs.append(pair)
            complete = bool(cohort) and all(pair['quality_admissible'] for pair in cohort)
            n = len(cohort)
            base_success = sum(pair['reference_quality'] is True for pair in cohort)
            candidate_success = sum(pair['candidate_quality'] is True for pair in cohort)
            boundaries.append(dict(boundary=point['id'],planned_pairs=n,admissible_pairs=sum(pair['quality_admissible'] for pair in cohort),
                complete=complete,reference_resolved=base_success if complete else None,candidate_resolved=candidate_success if complete else None,
                reference_resolve_rate=base_success/n if complete else None,candidate_resolve_rate=candidate_success/n if complete else None,
                quality_satisfied=candidate_success>=base_success if complete else None))
        formal = plan['config']['scope']=='formal'
        quality_complete = formal and bool(boundaries) and all(row['complete'] for row in boundaries)
        quality_satisfied = all(row['quality_satisfied'] for row in boundaries) if quality_complete else None
        cost_complete = quality_complete and all(pair['reference_api_cost_usd'] is not None and pair['candidate_api_cost_usd'] is not None for pair in pairs)
        base_cost = sum(pair['reference_api_cost_usd'] for pair in pairs) if cost_complete else None
        candidate_cost = sum(pair['candidate_api_cost_usd'] for pair in pairs) if cost_complete else None
        saving = base_cost-candidate_cost if cost_complete else None
        if not formal:
            verdict = 'mechanism_only'
        elif not quality_complete:
            verdict = 'incomplete_or_unverified'
        elif not quality_satisfied:
            verdict = 'observed_quality_regression'
        elif not cost_complete:
            verdict = 'observed_quality_constraint_met_cost_unavailable'
        elif saving>0:
            verdict = 'observed_quality_constraint_met_api_cost_lower'
        else:
            verdict = 'observed_quality_constraint_met_api_cost_not_lower'
        candidates.append(dict(method=label,verdict=verdict,quality_complete=quality_complete,quality_satisfied=quality_satisfied,
            api_cost_complete=cost_complete,reference_api_cost_usd=base_cost,candidate_api_cost_usd=candidate_cost,
            api_saving_usd=saving,api_saving_fraction=saving/base_cost if cost_complete and base_cost>0 else None,
            boundaries=boundaries,pairs=pairs))
    return dict(reference=reference,quality=setting['quality'],
        quality_scope='complete observed resolved-rate cohorts on every declared boundary; no statistical noninferiority or general quality guarantee',
        cost_scope='reported main, summary and recorded native compaction API usage at model-specific declared prices; unidentified models or missing rates have unknown cost; legacy logs may omit native compaction; host compute and total bill not assessed',
        candidates=candidates)
