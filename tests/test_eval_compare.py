"""Fabricated cohorts verify comparison logic; these are not experiment results."""
import copy, json
from pathlib import Path
import pytest
from ctxpress.harness.results import compare as eval_compare
from ctxpress.harness.jobs import plan as eval_plan, queue as evaluation
from ctxpress.core import artifacts as artifact_io
from ctxpress.live import usage as eval_usage
from ctxpress.harness.results.report import report, write_report
from test_evaluation import config

BASE='sha256:'+'1'*64
IMAGE='sha256:'+'2'*64
ENV='fixture-environment-digest'
GRADING='fixture-grading-digest'
PRICES=dict(input=1,cached=0.2,output=3,unit='USD_per_million_tokens',source='fixture only',as_of='2000-01-01')


def cohort(tmp_path,reference=((True,True),(False,False)),candidate=((True,True),(False,False)),prices=True,scope='formal'):
    points=[dict(id='b1',n=3,j=14,context_tokens=130000),dict(id='b2',n=0,j=273,context_tokens=140000)]
    cfg=dict(scope=scope,model='fixture-model',reasoning='low',boundaries=points,
             comparison=dict(reference='native-230k',quality='per_boundary_no_regression'))
    if prices: cfg['prices']=PRICES
    plan=dict(config=cfg,jobs=[],environment_snapshot=dict(sha256=ENV,images=dict(base=dict(id=BASE),
              boundaries={f"{point['n']}:{point['j']}":dict(id=IMAGE) for point in points})),grading_snapshot=dict(sha256=GRADING))
    records={f"{point['n']}:{point['j']}":dict(milestone=point['id'],repo_config=point['id']+'/repo.yaml',runtime_policy=point['id']+'/policy.yaml') for point in points}
    plan['grading_snapshot'].update(boundaries=records,trees=dict(trials=dict(files={name:'fixture-config-hash' for record in records.values() for name in (record['repo_config'],record['runtime_policy'])})))
    jobs=[]
    for label,outcomes,tokens in (('native-230k',reference,100),('candidate',candidate,50)):
        for point,values in zip(points,outcomes):
            for repeat,resolved in enumerate(values):
                spec=dict(id=f'{label}-{point["id"]}-{repeat}',label=label,boundary=point,repeat=repeat)
                plan['jobs'].append(spec)
                path=tmp_path/(spec['id']+'.json')
                raw=dict(resolved=resolved,infra_invalid=False,milestone_id=point['id'],snapshot_integrity=dict(ok=True,legacy_unverified=False),
                    evaluation_environment=dict(harness_revision='ctxpress-frozen:'+GRADING,snapshot_agent_tag_commit=spec['id'],
                        repo_config_binding_mode='trial-pinned',repo_config_sha256='fixture-config-hash',runtime_policy_binding_mode='trial-pinned',
                        runtime_policy_mode='protected',runtime_policy_sha256='fixture-config-hash'))
                artifact_io.atomic_json(path,raw)
                usage=dict(input_tokens=tokens,cached_tokens=10,output_tokens=2)
                result=dict(model='fixture-model',reasoning='low',requests=1,rewrites=[dict(request=1,usage=usage)],
                    usage=dict(summary_calls=0,api_input_tokens=tokens,api_cached_tokens=10,api_output_tokens=2),
                    boundary_context_evidence=dict(declared=point['context_tokens'],catalog_prefix_tokens=point['context_tokens']),
                    environment=dict(manifest_sha256=ENV,workspace_frozen=True,base_image_id=BASE,boundary_image_id=IMAGE),
                    grade=dict(**raw,graded_commit=spec['id'],grading_manifest_sha256=GRADING,report=str(path),report_sha256=eval_plan.file_sha256(path)))
                jobs.append(dict(id=spec['id'],spec=json.dumps(spec),status='completed',result=json.dumps(result)))
    return plan,jobs


def change(job,mutate):
    result=json.loads(job['result']);mutate(result);job['result']=json.dumps(result)


def analyzed(plan,jobs):
    return eval_compare.compare(plan,jobs)['candidates'][0]


def test_complete_observed_cohort_counts_summary_and_reports_api_savings(tmp_path):
    plan,jobs=cohort(tmp_path)
    result=analyzed(plan,jobs)
    assert result['quality_complete'] and result['quality_satisfied'] and result['api_cost_complete']
    assert result['verdict']=='observed_quality_constraint_met_api_cost_lower'
    assert result['api_saving_usd']==pytest.approx(4*50/1e6)
    assert len(result['pairs'])==4 and all(boundary['planned_pairs']==2 for boundary in result['boundaries'])
    assert 'no statistical' in eval_compare.compare(plan,jobs)['quality_scope']
    assert 'total bill not assessed' in eval_compare.compare(plan,jobs)['cost_scope']
    # Added summary requests cost more than the main-input saving.
    for job in jobs[4:]:
        def summary(result):
            result['rewrites'].append(dict(type='summary',model='fixture-model',usage=dict(input_tokens=500,cached_tokens=0,output_tokens=10)))
            result['usage'].update(summary_calls=1,api_input_tokens=550,api_output_tokens=12)
        change(job,summary)
    result=analyzed(plan,jobs)
    assert result['quality_satisfied'] and result['api_saving_usd']<0
    assert result['verdict']=='observed_quality_constraint_met_api_cost_not_lower'


def test_native_compaction_cost_cannot_disappear_from_a_comparison(tmp_path):
    plan,jobs=cohort(tmp_path)
    for job in jobs[4:]:
        def native(result):
            result['rewrites'].append(dict(type='native_compaction',usage=dict(input_tokens=500,cached_tokens=0,output_tokens=10)))
            result['usage'].update(native_compaction_calls=1,api_input_tokens=550,api_output_tokens=12)
        change(job,native)
    result=analyzed(plan,jobs)
    assert result['quality_satisfied'] and result['api_cost_complete'] and result['api_saving_usd']<0
    assert result['verdict']=='observed_quality_constraint_met_api_cost_not_lower'


def test_global_average_cannot_hide_regression_on_one_boundary(tmp_path):
    plan,jobs=cohort(tmp_path,candidate=((True,False),(True,False)))
    result=analyzed(plan,jobs)
    assert result['verdict']=='observed_quality_regression'
    assert [row['quality_satisfied'] for row in result['boundaries']]==[False,True]
    assert sum(row['reference_resolved'] for row in result['boundaries'])==sum(row['candidate_resolved'] for row in result['boundaries'])


def test_repeats_are_cohort_members_not_claimed_shared_random_seeds(tmp_path):
    plan,jobs=cohort(tmp_path,reference=((True,False),(False,True)),candidate=((False,True),(True,False)))
    assert analyzed(plan,jobs)['quality_satisfied'] is True


@pytest.mark.parametrize('failure',['pending','failed','interrupted','invalid-grade','missing-job','synthetic','raw-synthetic','changed-report','wrong-environment','wrong-grader','wrong-model','nan-context','changed-spec','changed-coordinate','duplicate-job'])
def test_incomplete_or_unverified_pairs_never_become_successes(tmp_path,failure):
    plan,jobs=cohort(tmp_path)
    target=jobs[-1]
    if failure in ('pending','failed','interrupted'): target['status']=failure
    elif failure=='missing-job': jobs.pop()
    elif failure=='duplicate-job': jobs.append(copy.deepcopy(target))
    elif failure in ('changed-spec','changed-coordinate'):
        spec=json.loads(target['spec'])
        if failure=='changed-spec': spec['unreviewed']='changed'
        else: spec['boundary']['n']+=100
        target['spec']=json.dumps(spec)
    elif failure=='changed-report': Path(json.loads(target['result'])['grade']['report']).write_text('{}',encoding='utf-8')
    else:
        def mutate(result):
            if failure=='invalid-grade': result['grade']['infra_invalid']=True
            elif failure=='synthetic': result['test_only']=True
            elif failure=='raw-synthetic':
                path=Path(result['grade']['report']);raw=json.loads(path.read_text(encoding='utf-8'));raw['test_only']=True
                artifact_io.atomic_json(path,raw);result['grade']['report_sha256']=eval_plan.file_sha256(path)
            elif failure=='wrong-environment': result['environment']['boundary_image_id']='changed'
            elif failure=='wrong-grader': result['grade']['grading_manifest_sha256']='changed'
            elif failure=='wrong-model': result['model']='another-model'
            elif failure=='nan-context': result['boundary_context_evidence']['catalog_prefix_tokens']=float('nan')
        change(target,mutate)
    result=analyzed(plan,jobs)
    assert not result['quality_complete'] and result['quality_satisfied'] is None
    assert result['verdict']=='incomplete_or_unverified' and result['api_saving_usd'] is None
    assert len(result['pairs'])==4 and result['boundaries'][-1]['planned_pairs']==2


@pytest.mark.parametrize('missing',['prices','usage','counts','totals'])
def test_quality_and_missing_cost_are_reported_separately(tmp_path,missing):
    plan,jobs=cohort(tmp_path,prices=missing!='prices')
    if missing!='prices':
        def mutate(result):
            if missing=='usage': del result['rewrites'][0]['usage']['cached_tokens']
            elif missing=='counts': result['requests']=2
            else: result['usage']['api_input_tokens']=999
        change(jobs[-1],mutate)
    result=analyzed(plan,jobs)
    assert result['quality_satisfied'] is True and not result['api_cost_complete']
    assert result['verdict']=='observed_quality_constraint_met_cost_unavailable'


def test_mechanism_and_undeclared_environment_cannot_validate_formal_quality(tmp_path):
    plan,jobs=cohort(tmp_path,scope='mechanism')
    assert analyzed(plan,jobs)['verdict']=='mechanism_only'
    plan['config']['scope']='formal';plan.pop('environment_snapshot')
    assert analyzed(plan,jobs)['verdict']=='incomplete_or_unverified'


@pytest.mark.parametrize('bad',[None,True,-1,1.5,float('nan')])
def test_usage_does_not_price_invalid_token_counts_as_zero(bad):
    result=dict(requests=1,usage=dict(summary_calls=0),rewrites=[dict(request=1,usage=dict(input_tokens=100,cached_tokens=bad,output_tokens=2))])
    accounting=eval_usage.analyze(result)
    assert not accounting['complete'] and eval_usage.price(accounting,PRICES) is None
    result['rewrites'][0]['usage']['cached_tokens']=101
    assert not eval_usage.analyze(result)['complete']


def test_plan_freezes_declared_reference_and_quality_without_launching(tmp_path,monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('comparison plan started a process'))
    cfg=config();cfg['comparison']=dict(reference='CodexAutoCompact',quality='per_boundary_no_regression')
    plan=eval_plan.compile_plan(cfg,tmp_path)
    assert plan['config']['comparison']==cfg['comparison']
    changed=copy.deepcopy(plan);changed['config']['comparison']['reference']='ComplexityTrap'
    with pytest.raises(ValueError,match='changed'): eval_plan.verify(changed,check_inputs=False)
    for setting in ({'reference':'missing','quality':'per_boundary_no_regression'}, {'reference':[],'quality':'per_boundary_no_regression'}, {'reference':'CodexAutoCompact','quality':'aggregate_average'}):
        cfg['comparison']=setting
        with pytest.raises(ValueError,match='comparison requires'): eval_plan.compile_plan(cfg,tmp_path)


def test_report_integration_keeps_incomplete_cohort_and_html_scope(tmp_path):
    cfg=config();cfg['comparison']=dict(reference='CodexAutoCompact',quality='per_boundary_no_regression')
    directory=evaluation.prepare(eval_plan.compile_plan(cfg),tmp_path/'run')
    value=report(directory)['comparison']
    assert value['reference']=='CodexAutoCompact' and value['candidates'][0]['verdict']=='mechanism_only'
    assert len(value['candidates'][0]['pairs'])==2
    write_report(directory)
    markup=(directory/'report.html').read_text(encoding='utf-8')
    assert '逐边界比较' in markup and '不是统计非劣效证明' in markup and '仅机制检查' in markup
