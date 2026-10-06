"""Real tool exceptions invalidate quality; assistant prose/task exits do not."""
import hashlib, json
from pathlib import Path
import pytest
from ctxpress.harness import execution_health, evaluation, eval_outcomes

ERROR='failed to spawn code-mode host /cxbin/codex-code-mode-host: No such file or directory (os error 2)'


def test_method_route_failure_invalidates_quality_but_preserves_cost_and_grade(tmp_path):
    run, path = result(tmp_path, transcript(output='ordinary output'))
    run['rewrites'].append(dict(type='method_tool_route_failed', reason='nested method tool', upstream_sent=False))
    run['usage'] = dict(main_input_tokens=100)
    execution_health.retain(run)
    for keep_session in (True, False):
        if not keep_session:
            path.unlink()
        health = execution_health.observe(run)
        assert health['execution_invalid'] and len(health['tool_runtime_failures']) == 1
        assert not eval_outcomes.observe(run['grade'], run)['rewards_valid']
        assert run['usage']['main_input_tokens'] == 100
        assert run['grade']['rewards'] == {'reward': 0}


def test_pending_method_inventory_needs_a_later_complete_observation_in_same_session():
    pending = dict(method_tool_route='direct_namespace_v1', method_tool_pending_cells=['cell'], session='a')
    complete = dict(method_tool_route='direct_namespace_v1', method_tool_pending_cells=[], session='a')
    assert execution_health.observe(dict(rewrites=[pending]))['execution_invalid']
    assert not execution_health.observe(dict(rewrites=[pending, complete]))['execution_invalid']
    assert execution_health.observe(dict(rewrites=[pending, dict(complete, session='b')]))['execution_invalid']


def event(kind, **kwargs):
    return dict(type='response_item',payload=dict(type=kind,**kwargs))


def transcript(kind='custom_tool_call', output=ERROR):
    return [event(kind,call_id='call_1',name='exec'),event(kind+'_output',call_id='call_1',output=output)]


def result(tmp_path, rows):
    root=tmp_path/'agent'; (root/'sessions').mkdir(parents=True)
    session=root/'sessions/rollout-fixture.jsonl'
    session.write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
    return dict(proxy_log=str(root/'ctxpress-requests.jsonl'),requests=1,
        rewrites=[dict(request=1,status=200)],grade=dict(infra_invalid=False,valid_rewards=True,rewards={'reward':0})),session


@pytest.mark.parametrize('kind', ['custom_tool_call','function_call'])
def test_matched_tool_outputs_override_successful_model_http_and_official_zero_reward(tmp_path,kind):
    run,path=result(tmp_path,transcript(kind))
    observed=execution_health.observe(run)
    assert observed['execution_invalid'] and observed['tool_calls']==observed['tool_outputs']==1
    failure=observed['tool_runtime_failures'][0]
    assert failure['line']==2 and failure['call_id']=='call_1' and failure['source']==str(path)
    assert failure['source_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert failure['event_type']==kind+'_output'
    assert evaluation.execution_observation(run)['execution_invalid']
    with pytest.raises(execution_health.ExecutionInfrastructureFailure): evaluation.validate_execution(run)
    outcome=eval_outcomes.observe(run['grade'],run)
    assert outcome['infra_invalid'] and not outcome['rewards_valid'] and outcome['rewards']=={}
    assert run['grade']==dict(infra_invalid=False,valid_rewards=True,rewards={'reward':0})


@pytest.mark.parametrize('rows', [
    [event('message',role='assistant',content=ERROR)],
    [dict(type='event_msg',payload=dict(type='task_complete',last_agent_message=ERROR))],
    [event('custom_tool_call_output',call_id='unmatched',output=ERROR)],
    transcript(output='Script completed\nOutput:\n'+ERROR),
    transcript(output=[dict(type='input_text',text=json.dumps(dict(exit_code=127,output=ERROR)))]),
    transcript(output=json.dumps(dict(exit_code=1,output='test failed'))),
    transcript(output='ordinary JavaScript exception: undefined variable'),
    [event('function_call',call_id='call_1',name='exec_command'),event('function_call_output',call_id='call_1',output=ERROR)],
])
def test_agent_prose_unmatched_outputs_and_task_command_errors_do_not_invalidate_quality(tmp_path,rows):
    run,_=result(tmp_path,rows)
    assert not execution_health.observe(run)['execution_invalid']
    evaluation.validate_execution(run)
    assert eval_outcomes.observe(run['grade'],run)['rewards_valid']


def test_retained_diagnostic_survives_session_removal_without_duplicate_failures(tmp_path):
    run,path=result(tmp_path,transcript())
    execution_health.retain(run)
    assert len(execution_health.observe(run)['tool_runtime_failures'])==1
    path.unlink()
    assert execution_health.observe(run)['execution_invalid']


def test_source_line_numbers_include_malformed_records(tmp_path):
    run,path=result(tmp_path,transcript())
    path.write_text('invalid json\n'+path.read_text(encoding='utf-8'), encoding='utf-8')
    assert execution_health.observe(run)['tool_runtime_failures'][0]['line']==3


@pytest.mark.parametrize('grade', [dict(infra_invalid=False,resolved=True),
    dict(infra_invalid=False,quality_kind='milestone',official_metrics_valid=True,official_metrics={'error':False})])
def test_native_issue_and_milestone_quality_are_invalid_but_usage_is_retained(tmp_path,grade):
    run,_=result(tmp_path,transcript());run['grade']=grade
    run['usage']={'main_input_tokens':100}
    outcome=eval_outcomes.observe(grade,run)
    assert outcome['infra_invalid'] and not outcome['boolean_valid'] and not outcome['milestone_metrics_valid']
    assert run['usage']=={'main_input_tokens':100}


@pytest.mark.parametrize('target', ['credential','outside'])
def test_symlinked_session_artifacts_are_rejected_before_read(tmp_path,monkeypatch,target):
    run,path=result(tmp_path,transcript());path.unlink()
    external=tmp_path/('auth.json' if target=='credential' else 'outside.jsonl')
    external.write_text('must never read', encoding='utf-8')
    path.symlink_to(external)
    original=Path.read_bytes
    def guarded(source):
        assert source.resolve()!=external.resolve(), 'read unsafe session alias'
        return original(source)
    monkeypatch.setattr(Path,'read_bytes',guarded)
    health=execution_health.observe(run)
    assert health['execution_invalid'] and health['health_unknown'] and health['evidence_errors']
    assert health['tool_runtime_failures']==[]


def test_symlinked_session_root_cannot_bypass_artifact_guards(tmp_path,monkeypatch):
    outside=tmp_path/'outside';outside.mkdir()
    path=outside/'rollout-outside.jsonl';path.write_text('must not read', encoding='utf-8')
    root=tmp_path/'agent';root.mkdir();(root/'sessions').symlink_to(outside,target_is_directory=True)
    monkeypatch.setattr(Path,'read_bytes',lambda *a:pytest.fail('read symlinked session root'))
    observed=execution_health.observe(dict(proxy_log=str(root/'ctxpress-requests.jsonl')))
    assert observed['execution_invalid'] and observed['health_unknown']


@pytest.mark.parametrize('stored', [[], 'wrong', {}, {'tool_runtime_failures':'bad'},
    {'execution_invalid':False,'tool_calls':0,'tool_outputs':0,'tool_runtime_failures':[{}]}])
def test_malformed_retained_health_is_explicitly_unknown_and_fails_closed(stored):
    health=execution_health.observe(dict(execution_health=stored))
    assert health['execution_invalid'] and health['health_unknown'] and health['evidence_errors']
    assert health['tool_runtime_failures']==[]


def test_report_excludes_quality_while_preserving_official_reward_and_cost_evidence(tmp_path):
    from test_task_start_plan import config
    from ctxpress.harness import eval_plan, eval_report
    cfg=config(tmp_path);plan=eval_plan.compile_plan(cfg)
    directory=evaluation.prepare(plan,tmp_path/'run')
    run,_=result(tmp_path/'artifacts',transcript())
    run['test_only']=True
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET status='completed',result=? WHERE id=?",(json.dumps(run),plan['jobs'][0]['id']))
    report=eval_report.report(directory);method=report['methods'][0]
    assert method['valid_rewards']==0 and method['infra_invalid']==1 and method['reward_metrics']=={}
    assert method['jobs'][0]['grade']['rewards']=={'reward':0}
    assert method['jobs'][0]['execution_observation']['execution_invalid']
