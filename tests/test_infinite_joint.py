import copy
import pytest
from ctxpress.core import infinite_policy as policy
from ctxpress.methods.infinite_context import InfiniteContext
from ctxpress.methods.infinite_joint import InfiniteContextJoint
from test_infinite_v3 import frozen, writer, run, add_turn


def joint(window=18000):
    b=frozen()
    b['version']=4
    b['joint_context']=dict(models={'test':dict(context_limit=window,
        max_output_tokens=4000, default_output_reserve=2000, source='synthetic test service')},
        growth_quantile=.9,growth_prior=1000,token_ratio_prior=1.,
        max_aux_cost=100000000.,aux_output_limit=8192,proposal_seconds_prior=0.,latency_rate=0.,planner_beam=64,
        retention_ratio_prior=.5,forecast_prior_weight=4.,forecast_quantile=.9,forecast_history_limit=128)
    return policy.seal(b)


def test_joint_factory_and_clone_keep_explicit_physical_model_limits():
    method=InfiniteContext(policy=joint(),agent_mode='control',agent_model='test-agent')
    assert isinstance(method,InfiniteContextJoint)
    cloned=copy.deepcopy(method)
    assert cloned.joint == method.joint and cloned.joint is not method.joint


def test_candidate_export_preserves_joint_runtime_and_physical_model_contract(tmp_path):
    import json
    from ctxpress.replay.infinite_v3_candidates import export
    original=joint()
    export(original,{'growth_quantile':[.8,.95]},str(tmp_path/'candidates'))
    for i,q in enumerate((.8,.95)):
        b=policy.load(tmp_path/'candidates'/f'candidate-{i:03d}.json')
        assert b['version']==4
        assert b['joint_context']['growth_quantile']==q
        assert b['joint_context']['models']==original['joint_context']['models']
    methods=json.loads((tmp_path/'candidates'/'methods.json').read_text())
    assert methods[0]['label']=='infinite-v4-000'
    with pytest.raises(ValueError):
        export(original,{'models':[{}]},str(tmp_path/'invalid'))
    for unused_or_physical in ({'trigger_ratio':[.4]},{'aux_output_limit':[4096]}):
        with pytest.raises(ValueError):
            export(original,unused_or_physical,str(tmp_path/'invalid'))


def test_real_host_uses_physical_capacity_output_and_growth_not_old_ratios(tmp_path):
    w,_=writer(tmp_path,b=joint(100000))
    try:
        _,info=run(w,[dict(role='user',content='Keep requirements')])
        d=info['method_decision'];j=d['joint_context']
        assert d['version']==4
        assert j['context_limit']==100000
        assert j['input_limit']==98000 and j['working_limit']==97000
        assert d['trigger']==d['target']==97000
        assert d['target_basis']=='model_capacity_minus_output_growth_uncertainty'
        assert d['capacity_feasible'] and info['host_contract']['valid']
    finally:w.close()


def test_no_compression_paths_still_collect_one_growth_sample_per_request(tmp_path):
    w,_=writer(tmp_path,b=joint(100000))
    try:
        items=[dict(role='user',content='Keep requirements')]
        run(w,items)
        method=w.sessions['session'].ctx.method
        add_turn(items,0,n=10);run(w,items)
        assert len(method.growth)==1 and method.growth[0]>0
        ctx=w.sessions['session'].ctx
        snapshot=list(method.growth)
        method.step(ctx,ctx.r-1)
        assert method.growth==snapshot
    finally:w.close()


def test_impossible_protected_content_is_reported_and_never_removed(tmp_path):
    w,_=writer(tmp_path,b=joint(5000))
    try:
        items=[dict(role='user',content='Mandatory requirement. '*2000)]
        output,info=run(w,items)
        assert output['input']==items
        assert not info['method_decision']['capacity_feasible']
        assert not info['method_decision']['target_feasible']
        assert info['host_contract']['valid']
    finally:w.close()


def test_request_output_limit_changes_reserved_space(tmp_path):
    w,_=writer(tmp_path,b=joint(100000))
    try:
        _,info=w.rewrite_body(dict(model='test',max_output_tokens=3500,
            input=[dict(role='user',content='Task')]),'session')
        assert info['method_decision']['joint_context']['working_limit']==95500
        assert info['method_decision']['joint_context']['output_source']=='request'
    finally:w.close()


def test_default_output_reserve_is_enforced_on_wire_without_mutating_caller(tmp_path):
    w,_=writer(tmp_path,b=joint(100000))
    try:
        body=dict(model='test',input=[dict(role='user',content='Task')])
        outgoing,info=w.rewrite_body(body,'session')
        assert outgoing['max_output_tokens']==2000
        assert info['request_output_limit']==outgoing['max_output_tokens']
        assert 'max_output_tokens' not in body
        assert info['method_decision']['joint_context']['output_reserve']==outgoing['max_output_tokens']
        assert info['method_decision']['joint_context']['output_source']=='configured_enforced'
    finally:w.close()


def test_returned_model_alias_feedback_is_used_by_next_capacity_decision(tmp_path):
    w,_=writer(tmp_path,b=joint(100000))
    try:
        body=dict(model='test',input=[dict(role='user',content='Task')])
        _,info=w.rewrite_body(body,'session')
        info.update(status=200,response_model='test-actual',usage={'input_tokens':100})
        assert w.observe_response(info)
        _,second=w.rewrite_body(body,'session')
        # the bill of the requested model sizes its next request: unchanged, it is what was billed
        assert second['method_decision']['joint_context']['calibration']['tokens']==100
    finally:w.close()


@pytest.mark.parametrize('changes',[dict(model='unknown'),dict(max_output_tokens=5000)])
def test_bad_capacity_configuration_cannot_fall_back_to_unmodified_upstream_request(tmp_path,changes):
    from ctxpress.live.contract import HostContractError
    w,_=writer(tmp_path,b=joint(100000))
    try:
        body=dict(model='test',input=[dict(role='user',content='Task')],**{})
        body.update(changes)
        with pytest.raises(HostContractError):w.rewrite_body(body,'session')
    finally:w.close()


def test_explicit_overflow_tightens_estimate_retries_and_preserves_output_cap(tmp_path):
    b=joint(1000000);b['v3']['eviction_k']=64;b=policy.seal(b)
    w,_=writer(tmp_path,b=b)
    try:
        items=[dict(role='user',content='Keep requirements')]
        add_turn(items,0);run(w,items);add_turn(items,1)
        original=dict(model='test',instructions='Keep requirements',input=copy.deepcopy(items))
        rejected,info=w.rewrite_body(original,'session')
        assert not info['method_decision']['pressure']
        method=w.sessions['session'].ctx.method
        growth=list(method.growth)
        budget=method.budget
        result=w.retry_body(original,rejected,info)
        assert result is not None
        outgoing,updated=result
        assert outgoing['max_output_tokens']==2000
        assert updated['method_decision']['joint_context']['upstream_rejection_rendered_ceiling'] is not None
        assert updated['host_contract']['valid']
        assert method.growth==growth
        assert method.budget is budget
        assert all(m.last is None for m in w.sessions['session'].ctx.size_models.values())
    finally:w.close()


def test_zero_auxiliary_budget_prevents_paid_calls_even_under_capacity_pressure(tmp_path):
    b=joint();b['joint_context']['max_aux_cost']=0;b=policy.seal(b)
    w,agent=writer(tmp_path,b=b)
    try:
        items=[dict(role='user',content='Keep requirements')]
        add_turn(items,0);run(w,items);add_turn(items,1)
        output,info=run(w,items)
        assert agent.calls==[]
        assert info['method_decision']['rejections']['auxiliary_cost_budget']>0
        assert info['method_decision']['auxiliary_calls']==0
        assert output['input']==items
        assert info['host_contract']['valid']
    finally:w.close()


def test_joint_compression_executes_with_complete_source_archive(tmp_path):
    w,agent=writer(tmp_path,b=joint(100000))
    try:
        items=[dict(role='user',content='Keep requirements')]
        add_turn(items,0);run(w,items);add_turn(items,1)
        output,info=run(w,items)
        assert agent.calls and info['method_decision']['selected']
        assert info['method_decision']['combination_plan']['commit_complete']
        plan=info['method_decision']['combination_plan']
        selected=next(c for c in plan['candidates'] if c['key']==plan['selected'])
        assert selected['input_tokens']==pytest.approx(info['method_decision']['host_tokens_after'])
        assert info['method_decision']['auxiliary_reserved_cost']>0
        method=w.sessions['session'].ctx.method
        assert method._forecasts().predict('annotation')['samples']>0
        assert info['method_decision']['forecast_decisions']
        assert output['input'][0]==items[0]
        assert info['host_contract']['valid']
        from ctxpress.methods import infinite_memory as memory
        ctx=w.sessions['session'].ctx
        for segment in ctx.ctx:
            if segment.get('memory_key'):
                assert memory.get(ctx.store_dir,segment['memory_key'])
    finally:w.close()


def test_infeasible_recent_content_does_not_spend_on_useless_summary(tmp_path):
    w,agent=writer(tmp_path,b=joint())
    try:
        items=[dict(role='user',content='Keep requirements')]
        add_turn(items,0);run(w,items);add_turn(items,1)
        output,info=run(w,items)
        assert not agent.calls
        assert not info['method_decision']['selected']
        assert info['method_decision']['rejections']['protected_floor_infeasible']>0
        assert output['input']==items
    finally:w.close()


def test_combination_scheduler_executes_recursive_stage_and_preserves_children(tmp_path):
    from ctxpress.methods import infinite_memory as memory
    w,_=writer(tmp_path,b=joint(100000))
    try:
        items=[dict(role='user',content='Keep requirements')]
        stages=[]
        for i in range(5):
            add_turn(items,i)
            _,info=run(w,items)
            stages.extend(s['key'] for s in info['method_decision']['selected'] if s['op']=='stage')
        assert stages
        ctx=w.sessions['session'].ctx
        nodes=[memory.get(ctx.store_dir,key) for key in stages]
        assert any(any(memory.get(ctx.store_dir,c)['kind']=='stage' for c in n['children']) for n in nodes)
        for key,node in zip(stages,nodes):
            assert memory.read(ctx.store_dir,key,chars=80)['children']==node['children']
    finally:w.close()


def test_forecast_counts_failed_admitted_calls_but_not_cached_rejections(tmp_path):
    from test_infinite_v3 import Agent
    w,agent=writer(tmp_path,b=joint(100000),agent=Agent(invalid=True))
    try:
        items=[dict(role='user',content='Keep requirements')]
        add_turn(items,0);run(w,items);add_turn(items,1);run(w,items)
        method=w.sessions['session'].ctx.method
        first=method._forecasts().predict('annotation')
        assert first['samples']>0 and first['failures']==first['samples']
        assert first['retained_ratio']>method.joint['retention_ratio_prior']
        calls=len(agent.calls)
        # Repeat the identical failed proposal, not a new request which can
        # make additional formerly recent units eligible.
        body={k:agent.calls[0][k] for k in ('operation','task','records','max_tokens')}
        assert method._ask3(w.sessions['session'].ctx,body) is None
        assert method._forecasts().predict('annotation')['samples']==first['samples']
        assert len(agent.calls)==calls
    finally:w.close()


def test_late_worker_cannot_pollute_replaced_history_forecast(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    w,_=writer(tmp_path,b=joint(100000))
    release=threading.Event();started=threading.Event()
    try:
        run(w,[dict(role='user',content='Old task')])
        old=w.sessions['session'].ctx.method
        def delayed():
            started.set();assert release.wait(5)
            old._forecasts().observe('annotation',input_tokens=1000,output_tokens=100,
                seconds=1,cost_estimate=.01,usable=True)
        old.executor=ThreadPoolExecutor(max_workers=1)
        pending=old.executor.submit(delayed)
        assert started.wait(2)
        _,info=run(w,[dict(role='user',content='Replacement task')])
        current=w.sessions['session'].ctx.method
        assert current is not old and old.retired
        assert info['history_rebased']
        release.set();pending.result(timeout=2)
        assert old._forecasts().predict('annotation')['samples']==1
        assert current._forecasts().predict('annotation')['samples']==0
        assert not current.retired
    finally:
        release.set();w.close()
