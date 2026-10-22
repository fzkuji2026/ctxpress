import copy
import json
from pathlib import Path
import pytest
from ctxpress.core import infinite_policy as policy
from ctxpress.core.params import DEFAULT
from ctxpress.live.context import LiveContext
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import build
from ctxpress.methods.infinite_context import InfiniteContext
from ctxpress.replay.infinite_fit import fit, observations, statistics_bundle, search_space


def history(name='a', length=30, group=None):
    reqs = []
    for i in range(length):
        path = f'{name}/f{i}.py'
        reqs.append(dict(input=(i+1)*2000, cached=i*2000, out_tokens=30, t=i+1,
            before=[dict(seg='call', size=5, kind='read', res=[path], text=f'cat {path}', call_id=str(i)),
                    dict(seg='out', size=2000, kind='read', res=[path], outpaths=[], sub='code',
                         text=f'PRIVATE_{name}_{i}\n'*1000, call_id=str(i))]))
    return dict(name=name, group=group or name, prefix=50, alpha=1., reqs=reqs)


def bundle(**changes):
    config = dict(trigger_ratio=.4, target_ratio=.2, work_turns=0, cooldown=0, batch_size=4,
                  summary_tokens=32, evidence_tokens=65536, horizon_scale=2., risk_scale=1., prior_strength=4, min_gain=0.)
    config.update(changes)
    traces = [history()]
    params = DEFAULT.override(window=200000, cached=.1, write=1., out=2.)
    return policy.make(config, statistics_bundle(traces, config['summary_tokens']), params,
        observations(traces, params), ['a'], max_risk=.02,
        agent=dict(model='test-agent', input=.00001, output=.00001, unit='same_as_main_per_token'))


def live(tmp_path, frozen=None, agent=None, mode='off'):
    method = InfiniteContext(policy=frozen or bundle(), agent_mode=mode,
                             agent_model='test-agent' if mode != 'off' else None)
    ctx = LiveContext(method, store_dir=str(tmp_path), summarizer=agent)
    ctx.add_message('user', 'Keep API contract and unresolved failures', fixed=True)
    for i in range(5):
        ctx.add_call(str(i), f'cat src/f{i}.py')
        ctx.add_output(str(i), f'detail {i}\n'*1000)
    ctx.before_request()  # new observations must be consumed once before compaction
    return ctx


class Agent:
    def __init__(self, retention='working', invalid=False, callback=None):
        self.calls = []; self.retention = retention; self.invalid = invalid; self.callback = callback
    def summarize_prompt(self, system, user, purpose, model):
        self.calls.append((purpose, model))
        if self.callback:
            self.callback()
        if self.invalid:
            return 'not JSON'
        body = json.loads(user)
        return json.dumps(dict(records=[dict(id=r['id'], summary='Unresolved detail remains; consult source.',
                            retention=self.retention) for r in body['records']]))


def test_archive_survives_and_recoverable_text_reduces_context(tmp_path):
    ctx = live(tmp_path)
    before = ctx.request_tokens()
    ctx.before_request()
    assert ctx.request_tokens() < before and ctx.M['infinite_compacted_records']
    for item in ctx.ctx:
        if item['seg'] == 'out':
            assert (tmp_path/f'{item["id"]}.txt').read_text() == item['text']
            if item.get('kept_text'):
                assert 'Source id=' in ctx.render(item)
                assert ctx.retrieve(item['id']) == item['text']


def test_missing_or_corrupt_archive_keeps_original(tmp_path):
    ctx = live(tmp_path)
    for file in tmp_path.glob('*.txt'):
        file.write_text('corrupt')
    ctx.before_request()
    assert not ctx.M['infinite_compacted_records']


def test_no_fitted_policy_keeps_history(tmp_path):
    ctx = LiveContext(InfiniteContext(), store_dir=str(tmp_path))
    ctx.add_call('a','cat a.py');ctx.add_output('a','long\n'*10000)
    ctx.before_request();ctx.before_request()
    assert not ctx.M['infinite_compacted_records']


def test_repeat_read_raises_risk_using_only_observed_history(tmp_path):
    ctx = live(tmp_path)
    ctx.before_request()
    old = next(s for s in ctx.ctx if s['seg']=='out' and s.get('kept_text'))
    risk_before = ctx.method._risk(old,ctx.r,'agent')[0]
    ctx.add_call('again', f'cat {old["res"][0]}')
    assert ctx.M['infinite_recovery_observed'] == 1
    assert ctx.method._risk(old,ctx.r,'agent')[0] > risk_before


def test_agent_persistent_vetoes_rule_candidates(tmp_path):
    agent = Agent(retention='persistent')
    ctx = live(tmp_path, agent=agent, mode='control')
    ctx.before_request()
    assert agent.calls and not ctx.M['infinite_compacted_records']


def test_invalid_agent_keeps_history(tmp_path):
    agent=Agent(invalid=True)
    ctx=live(tmp_path,agent=agent,mode='control');ctx.before_request()
    assert ctx.M['infinite_agent_invalid'] and not ctx.M['infinite_compacted_records']


def test_shadow_does_not_change_rule_decision(tmp_path):
    a=live(tmp_path/'a');b=live(tmp_path/'b',agent=Agent(retention='persistent'),mode='shadow')
    a.before_request();b.before_request()
    assert a.M['infinite_compacted_records']==b.M['infinite_compacted_records']


def test_agent_actual_candidate_and_explicit_model(tmp_path):
    agent=Agent();ctx=live(tmp_path,agent=agent,mode='control');ctx.before_request()
    assert agent.calls==[('infinite_context_compression','test-agent')]
    assert ctx.method.last_decision['agent_candidates']>0
    assert ctx.M['infinite_compacted_records']>0
    with pytest.raises(ValueError):
        InfiniteContext(policy=bundle(),agent_mode='control',agent_model='unknown')


def test_new_user_instruction_invalidates_pending_proposal(tmp_path):
    agent=Agent();ctx=live(tmp_path,agent=agent,mode='control')
    agent.callback=lambda:ctx.add_message('user','Do not discard the earlier details')
    ctx.before_request()
    assert ctx.M['infinite_stale_proposal']==1 and not ctx.M['infinite_compacted_records']


def test_auxiliary_expense_prevents_call(tmp_path):
    b=bundle();b['agent']['input']=1e9;b=policy.seal(b)
    agent=Agent();ctx=live(tmp_path,frozen=b,agent=agent,mode='control');ctx.before_request()
    assert not agent.calls and ctx.M['infinite_agent_not_worth_calling']


def test_capacity_fails_closed_without_erasing_requirements(tmp_path):
    b=bundle();b['constraints']['capacity']=100
    for row in b['parameters']:
        if row['name']=='window':row['value']=100
    b=policy.seal(b)
    ctx=LiveContext(InfiniteContext(policy=b),store_dir=str(tmp_path))
    ctx.add_message('user','must keep\n'*1000,fixed=True)
    from ctxpress.live.contract import HostContractError
    with pytest.raises(HostContractError):ctx.before_request()
    assert len(ctx.sent()[0]['text'])>100


def test_wire_preserves_pairs_and_logs_decision(tmp_path):
    rw=Rewriter(lambda:InfiniteContext(policy=bundle()),store_dir=str(tmp_path))
    inputs=[dict(role='user',content='task')]
    for i in range(5):
        inputs += [dict(type='function_call',call_id=str(i),name='exec',arguments=f'cat f{i}.py'),
                   dict(type='function_call_output',call_id=str(i),output='details\n'*1000)]
    request=dict(model='test',input=inputs)
    rw.rewrite_body(copy.deepcopy(request),'s')
    body,info=rw.rewrite_body(copy.deepcopy(request),'s')
    assert info['method_decision']['selected']
    assert len(body['input'])==len(inputs)
    assert body['input'][0]==inputs[0]


def test_policy_tampering_and_unknown_knobs_rejected():
    b=bundle();b['settings']['cooldown']=10
    with pytest.raises(ValueError):InfiniteContext(policy=b)
    b=bundle();b['settings']['invented']=3
    with pytest.raises(ValueError):policy.seal(b)


def test_grouped_fit_has_no_holdout_leakage_and_no_raw_text():
    traces=[history('a',12),history('b',13),history('c',14)]
    candidates=[bundle()['settings']]
    fitted=fit(traces,params=DEFAULT.override(window=200000),holdout_groups=['c'],candidates=candidates)
    assert fitted['statistics']['provenance']['sessions']==['a','b']
    assert fitted['evidence']['holdout_groups']==['c']
    assert {r['group'] for r in fitted['evidence']['candidates'][0]['folds']}=={'a','b'}
    assert 'PRIVATE_' not in json.dumps(fitted)
    assert not fitted['evidence']['agent_evaluated']
    changed=copy.deepcopy(traces);changed[2]['reqs'][0]['before'][1]['text']='HOLDOUT_ONLY'
    again=fit(changed,params=DEFAULT.override(window=200000),holdout_groups=['c'],candidates=candidates)
    assert fitted['settings']==again['settings'] and fitted['statistics']==again['statistics']


def test_duplicate_histories_and_bad_groups_rejected():
    traces=[history('a',12),history('b',13),history('c',14)]
    traces[1]['reqs']=copy.deepcopy(traces[0]['reqs'])
    with pytest.raises(ValueError,match='duplicate histories'):
        fit(traces,params=DEFAULT,holdout_groups=['c'])
    with pytest.raises(ValueError,match='two development'):
        fit([history('a'),history('b')],params=DEFAULT,holdout_groups=['b'])


def test_all_behavioral_knobs_in_reproducible_search_space():
    rows=search_space([history()],200000,trials=64,seed=3)
    assert rows==search_space([history()],200000,trials=64,seed=3)
    for key in policy.BOUNDS:
        assert len({row[key] for row in rows})>1
    assert build({'class':'infinite-context'}).name=='infinite-context'


def test_archive_search_pagination_integrity_and_session_isolation(tmp_path):
    from ctxpress.methods.infinite_archive import search, read
    ctx=live(tmp_path);ctx.before_request()
    matches=search(tmp_path,'detail 0')['matches']
    assert matches
    iid=matches[0]['id']
    first=read(tmp_path,iid,chars=15)
    second=read(tmp_path,iid,offset=first['next_offset'],chars=15)
    assert first['text']+second['text']==ctx.retrieve(iid)[:30]
    with pytest.raises(ValueError):read(tmp_path,iid,session='../escape')
    for item in ctx.ctx:
        if item['seg']=='out' and item.get('kept_text'):
            (tmp_path/f'{item["id"]}.txt').write_text('tampered')
            with pytest.raises(ValueError,match='integrity'):read(tmp_path,item['id'])
            break


def test_retrieval_tool_call_is_counted_as_recovery(tmp_path):
    ctx=live(tmp_path);ctx.before_request()
    iid=next(iter(ctx.method.removed))
    args=json.dumps({'id':iid})
    ctx.add_call('retrieve',args,name='infinite_context_read',args=args)
    assert ctx.M['infinite_recovery_observed']==1


def test_measured_selector_includes_auxiliary_cost_and_rejects_quality_loss():
    from ctxpress.replay.infinite_select import select
    b=bundle();policies={'cheap':b,'costly':b}
    rows=[]
    for group in ('dev','test'):
        for name,main,aux,score in [('keep',10,0,1),('cheap',5,1,1),('costly',4,8,1)]:
            rows.append(dict(candidate=name,group=group,task=group,repeat=0,comparison=group+'-frozen',
                main_cost=main,auxiliary_cost=aux,total_cost=main+aux,score=score,
                cost_complete=True,quality_reviewed=True,policy_sha256=b['sha256'],agent_mode='off'))
    result=select(policies,rows,holdout_groups=['test'])
    assert result['selected']=='cheap' and result['enabled']
    rows[1]['score']=.5
    result=select(policies,rows,holdout_groups=['test'])
    assert result['selected'] is None


def test_measured_selector_never_retunes_to_holdout_and_rejects_incomplete_bill():
    from ctxpress.replay.infinite_select import select
    b=bundle();rows=[]
    for group in ('dev','test'):
        for name in ('keep','candidate'):
            main=10 if name=='keep' else 5
            score=.5 if group=='test' and name!='keep' else 1
            rows.append(dict(candidate=name,group=group,task=group,repeat=0,comparison=group,
                main_cost=main,auxiliary_cost=0,total_cost=main,score=score,cost_complete=True,
                quality_reviewed=True,policy_sha256=b['sha256'],agent_mode='off'))
    result=select({'candidate':b},rows,holdout_groups=['test'])
    assert result['selected']=='candidate' and not result['enabled']
    rows[0]['cost_complete']=False
    with pytest.raises(ValueError):select({'candidate':b},rows,holdout_groups=['test'])


def test_candidate_export_covers_agent_modes_and_binds_training_inputs(tmp_path):
    from ctxpress.replay.infinite_candidates import export
    traces=[history('a',12),history('b',13),history('c',14)]
    fitted=fit(traces,params=DEFAULT.override(window=200000),holdout_groups=['c'],candidates=[bundle()['settings']])
    result=export(traces,fitted,tmp_path/'candidates',agent=bundle()['agent'])
    assert result['methods']==4 and result['experiments_started']==0
    entries=json.loads((tmp_path/'candidates'/'methods.json').read_text())
    assert {e['args']['agent_mode'] for e in entries[1:]}=={'off','shadow','control'}
    for entry in entries[1:]:
        m=build(entry)
        assert m.policy['evidence']['experiment_only']
        assert m.policy['statistics']['provenance']['sessions']==['a','b']
    traces[0]['reqs'][0]['before'][1]['text']='changed'
    with pytest.raises(ValueError,match='inputs differ'):export(traces,fitted,tmp_path/'bad')
    assert not (tmp_path/'bad').exists()


def test_measured_policy_cannot_silently_change_agent_mode():
    b=bundle();b['measured_selection']={'agent_mode':'control'};b=policy.seal(b)
    m=InfiniteContext(policy=b)
    assert m.agent_mode=='control' and m.agent_model=='test-agent'
    with pytest.raises(ValueError,match='agent mode differs'):
        InfiniteContext(policy=b,agent_mode='off')


def test_agent_timeout_keeps_history(tmp_path):
    class Timeout:
        def summarize_prompt(self,*args,**kwargs):raise TimeoutError('service unavailable')
    ctx=live(tmp_path,agent=Timeout(),mode='control');ctx.before_request()
    assert not ctx.M['infinite_compacted_records'] and ctx.M['summary_failed']==1


def test_persistent_veto_survives_later_requests(tmp_path):
    agent=Agent(retention='persistent');ctx=live(tmp_path,agent=agent,mode='control')
    ctx.before_request();ctx.before_request();ctx.before_request()
    assert not ctx.M['infinite_compacted_records']
    assert len(agent.calls)==2  # four records then the remaining one, no repeated persistent review


def test_disabled_policy_does_not_commit(tmp_path):
    b=bundle();b['evidence']['enabled']=False;b=policy.seal(b)
    ctx=live(tmp_path,frozen=b);ctx.before_request()
    assert not ctx.M['infinite_compacted_records']
