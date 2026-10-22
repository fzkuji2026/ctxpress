import copy
import json
import time
import pytest
from ctxpress.core import infinite_policy as policy
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import infinite_memory as memory
from ctxpress.methods.infinite_context import InfiniteContext
from ctxpress.methods.infinite_v3 import DEFAULTS, FIELDS, InfiniteContextV3
from test_infinite_adaptive import bundle


def frozen(**changes):
    b=bundle(trigger_ratio=.4,target_ratio=.2,work_turns=1,batch_size=8,summary_tokens=512,evidence_tokens=2048)
    b['version']=3;b['v3']=dict(DEFAULTS,eviction_k=1,group_tokens=512,annotation_workers=2)
    b['v3'].update(changes)
    b['constraints']['capacity']=18000
    for p in b['parameters']:
        if p['name']=='window':p['value']=18000
    return policy.seal(b)


class Agent:
    def __init__(self,retention='working',invalid=False):self.calls=[];self.retention=retention;self.invalid=invalid
    def summarize_prompt(self,system,user,**kw):
        d=json.loads(user);self.calls.append(d)
        if self.invalid:return 'not json'
        state={k:[] for k in FIELDS};state['failures']=['test_x failed; no verified fix'];state['next']=['repair test_x']
        return json.dumps(dict(goal='repair module',phase='implementation',retention=self.retention,completed=False,
                               summary='Same module observations merged. Exact sources remain archived.',state=state))


def writer(tmp_path,b=None,agent=None):
    b=b or frozen();agent=agent or Agent()
    w=Rewriter(lambda:InfiniteContext(policy=b,agent_mode='control',agent_model='test-agent'),store_dir=str(tmp_path),summarizer=agent)
    return w,agent


def add_turn(items,i,n=3000):
    items.extend([dict(type='reasoning',summary=[dict(type='summary_text',text=('Reason through changes. '*n))]),
                  dict(type='function_call',call_id=str(i),name='exec_command',arguments=json.dumps({'cmd':f'cat module/file{i}.py'})),
                  dict(type='function_call_output',call_id=str(i),output=('test_x failed; inspect module\n'*n)),
                  dict(type='message',role='assistant',content=[dict(type='output_text',text='Need repair; commit alone is not verification.')])])


def run(w,items):return w.rewrite_body(dict(model='test',instructions='Keep requirements',input=copy.deepcopy(items)),'session')


def test_full_pipeline_compacts_calls_reasoning_and_retains_exact_sources(tmp_path):
    w,a=writer(tmp_path);items=[dict(role='user',content='Fix module, never remove test_x')]
    add_turn(items,0);run(w,items);add_turn(items,1);out,info=run(w,items)
    assert info['method_decision']['selected']
    assert out['input'][0]==items[0]
    assert info['method_decision']['host_tokens_after']==pytest.approx(info['rendered_prompt_tokens'],abs=1)
    ctx=w.sessions['session'].ctx
    summaries=[s for s in ctx.ctx if s['seg']=='summary'];assert summaries
    stage=memory.get(ctx.store_dir,summaries[0]['memory_key'])
    originals=[memory.get(ctx.store_dir,k) for k in stage['children']]
    assert any(x['seg']=='reason' and x['wire']['type']=='reasoning' for x in originals)
    assert any(x['seg']=='call' for x in originals) and any(x['seg']=='out' for x in originals)
    assert 'test_x failed' in summaries[0]['text']


def test_summary_can_be_remerged_and_provenance_survives(tmp_path):
    w,a=writer(tmp_path);items=[dict(role='user',content='Fix module')]
    oldkeys=[]
    for i in range(5):
        add_turn(items,i);run(w,items)
        oldkeys += [s['memory_key'] for s in w.sessions['session'].ctx.ctx if s['seg']=='summary']
    ctx=w.sessions['session'].ctx
    assert any(s.get('memory_level',0)>1 for s in ctx.ctx)
    assert any(c['operation']=='pressure_compact' for c in a.calls)
    assert all(memory.get(ctx.store_dir,k) for k in oldkeys)
    assert memory.search(ctx.store_dir,'test_x')['matches']


@pytest.mark.parametrize('retention,invalid',[('persistent',False),('working',True)])
def test_failure_or_veto_keeps_original_and_records_capacity(tmp_path,retention,invalid):
    w,a=writer(tmp_path,agent=Agent(retention,invalid));items=[dict(role='user',content='Fix module')]
    add_turn(items,0);run(w,items);add_turn(items,1);out,info=run(w,items)
    assert out['input']==items
    assert not info['method_decision']['selected']
    assert info['method_decision']['capacity_feasible'] is False
    # Char estimate alone must never synthesize provider context-limit errors.
    assert info['host_contract']['valid']


def test_corrupt_whole_history_archive_prevents_commit(tmp_path):
    w,a=writer(tmp_path);items=[dict(role='user',content='Fix module')];add_turn(items,0);run(w,items)
    ctx=w.sessions['session'].ctx
    old=next(s for s in ctx.ctx if s['seg']=='reason')
    (tmp_path/ctx.store_namespace/'memory'/(old['memory_key']+'.json')).write_text('{}')
    add_turn(items,1);out,info=run(w,items)
    assert not info['method_decision']['selected']
    with pytest.raises(ValueError):memory.read(ctx.store_dir,old['memory_key'])


def test_correction_changes_annotation_identity(tmp_path):
    w,a=writer(tmp_path);items=[dict(role='user',content='Fix module')]
    add_turn(items,0);run(w,items);items.append(dict(role='user',content='Correction: keep exact interface'));add_turn(items,1);out,info=run(w,items)
    assert any(x.get('content')=='Correction: keep exact interface' for x in out['input'])
    assert all('Correction' in str(c['task']) for c in a.calls)


def test_memory_paging_and_tamper_validation(tmp_path):
    key=memory.put(tmp_path,dict(kind='record',text='abc'*10000,children=[]))
    x=memory.read(tmp_path,key,chars=100);assert x['next_offset']==100
    assert memory.search(tmp_path,'abc')['matches'][0]['key']==key
    with pytest.raises(ValueError):memory.get(tmp_path,'../../other')


def test_same_strategy_required_for_fitting():
    with pytest.raises(ValueError,match='same control'):InfiniteContext(policy=frozen(),agent_mode='off')


def test_async_annotation_never_commits_stale_user_task(tmp_path):
    import threading
    started=threading.Event();release=threading.Event()
    class Slow(Agent):
        def summarize_prompt(self,*args,**kwargs):
            started.set();release.wait(2)
            return super().summarize_prompt(*args,**kwargs)
    b=frozen();b['constraints']['capacity']=1000000
    for p in b['parameters']:
        if p['name']=='window':p['value']=1000000
    b=policy.seal(b)
    w,a=writer(tmp_path,b=b,agent=Slow());items=[dict(role='user',content='Fix module')]
    add_turn(items,0,200);run(w,items);add_turn(items,1,200)
    t=time.monotonic();out,info=run(w,items)
    assert time.monotonic()-t<1
    assert started.wait(1)
    items.append(dict(role='user',content='New task: preserve previous observations'))
    release.set();w.close()
    # No background worker may mutate the live context, even after completing.
    assert not any(s['seg']=='summary' for s in w.sessions['session'].ctx.ctx)
    out,info=run(w,items)
    assert items[-1] in out['input']
    w.close()


def test_media_and_call_pair_remain_unchanged(tmp_path):
    w,a=writer(tmp_path);items=[dict(role='user',content='Fix module')]
    add_turn(items,0)
    items[3]['output']=[dict(type='input_image',image_url='data:image/png;base64,AA==')]
    run(w,items);add_turn(items,1);out,info=run(w,items)
    assert items[2] in out['input'] and items[3] in out['input']
    assert info['host_contract']['valid'];w.close()


def test_critical_issues_survive_second_summary_even_if_agent_omits_them(tmp_path):
    class Forgets(Agent):
        def summarize_prompt(self,system,user,**kw):
            raw=super().summarize_prompt(system,user,**kw);row=json.loads(raw)
            if json.loads(user)['operation'] in ('pressure_compact','compact','annotation_reduce'):
                row['state']['failures']=[];row['state']['unresolved']=[]
            return json.dumps(row)
    w,a=writer(tmp_path,agent=Forgets());items=[dict(role='user',content='Fix module')]
    for i in range(4):add_turn(items,i,1000);run(w,items)
    ss=[s for s in w.sessions['session'].ctx.ctx if s['seg']=='summary']
    assert ss and all('test_x failed' in s['text'] for s in ss);w.close()


def test_deadline_prevents_late_background_commit(tmp_path):
    class Slow(Agent):
        def summarize_prompt(self,*args,**kwargs):
            time.sleep(1.2)
            return super().summarize_prompt(*args,**kwargs)
    w,a=writer(tmp_path,b=frozen(pressure_seconds=1),agent=Slow());items=[dict(role='user',content='Fix module')]
    add_turn(items,0);run(w,items);add_turn(items,1)
    t=time.monotonic();out,info=run(w,items)
    assert time.monotonic()-t<1.8
    assert not info['method_decision']['selected']
    w.close();assert not any(s['seg']=='summary' for s in w.sessions['session'].ctx.ctx)


def test_candidate_export_binds_all_controls_and_no_jobs(tmp_path):
    from ctxpress.replay.infinite_v3_candidates import export
    grid={'summary_tokens':[256,512],'compact_m':[2,4],'similarity':[.2]}
    result=export(bundle(),grid,tmp_path/'candidates');assert result['candidates']==4 and result['experiments_started']==0
    methods=json.loads((tmp_path/'candidates/methods.json').read_text())
    assert all(m['args']['agent_mode']=='control' for m in methods)
    assert all(policy.load(m['args']['policy'])['version']==3 for m in methods)


def test_exact_measurement_includes_fixed_tools_and_keeps_raw_history(tmp_path):
    w,a=writer(tmp_path);items=[dict(role='user',content='Fix module')]
    add_turn(items,0);run(w,items);add_turn(items,1)
    body=dict(model='test',instructions='instructions '*300,input=copy.deepcopy(items),
              tools=[dict(type='function',name='tool',description='large schema '*100,parameters={'type':'object'})])
    saved=copy.deepcopy(body);out,info=w.rewrite_body(body,'session')
    assert body==saved
    assert info['method_decision']['host_tokens_after']==pytest.approx(info['rendered_prompt_tokens'],abs=1)
    assert out['tools']==body['tools'];w.close()


def test_long_running_history_stays_bounded_with_recursive_memory(tmp_path):
    w,a=writer(tmp_path);items=[dict(role='user',content='Repair module, retain failures')]
    sizes=[];targets=[]
    for i in range(100):
        add_turn(items,i,150)
        out,info=run(w,items)
        sizes.append(info['rendered_prompt_tokens'])
        if info['method_decision'].get('pressure'):targets.append(info['method_decision'].get('target_reached'))
    ctx=w.sessions['session'].ctx
    assert max(sizes)<18000
    assert any(targets)
    assert any(s.get('memory_level',0)>2 for s in ctx.ctx)
    assert len(ctx.ctx)<len(items)//4
    w.close()


def test_invalid_proposal_does_not_repeat_paid_call_for_unchanged_evidence(tmp_path):
    w,a=writer(tmp_path,agent=Agent(invalid=True));items=[dict(role='user',content='Fix module')]
    add_turn(items,0,1000);run(w,items);add_turn(items,1,1000);run(w,items)
    w.close();n=len(a.calls)
    for i in range(3):run(w,items);w.close()
    identities=[json.dumps(x,sort_keys=True) for x in a.calls]
    assert len(identities)==len(set(identities))


def test_coverage_allows_paraphrase_dedup_but_rejects_missing_facts(tmp_path):
    class Covers(Agent):
        def __init__(self,missing=False):super().__init__();self.missing=missing
        def summarize_prompt(self,system,user,**kw):
            d=json.loads(user);row=json.loads(super().summarize_prompt(system,user,**kw))
            row['state']['constraints']=['API unchanged']
            row['coverage']={k:'API unchanged' for k in d.get('required_facts',{})}
            if self.missing and row['coverage']:row['coverage'].pop(next(iter(row['coverage'])))
            return json.dumps(row)
    b=frozen();w,a=writer(tmp_path,b,agent=Covers());items=[dict(role='user',content='Fix module')];run(w,items)
    m=w.sessions['session'].ctx.method;ctx=w.sessions['session'].ctx
    m.deadline=time.monotonic()+10;m.budget={'calls':0,'fee':0.,'deadline':m.deadline}
    row=dict(goal='g',phase='p',retention='working',completed=False,summary='s',state={k:[] for k in FIELDS})
    row['state']['constraints']=['Keep API','Do not change interface']
    body=dict(operation='compact',task=[],records=[row],max_tokens=1024)
    result=m._ask3(ctx,body)
    assert result['state']['constraints']==['API unchanged']
    # A nonempty partial coverage map must never grant permission to drop facts.
    ctx.summarizer=Covers(missing=True);body['task']=['different task']
    assert m._ask3(ctx,body) is None
    w.close()


def test_changed_global_deficit_does_not_rebill_same_summary(tmp_path):
    w,a=writer(tmp_path);run(w,[dict(role='user',content='Fix module')]);ctx=w.sessions['session'].ctx;m=ctx.method
    m.deadline=time.monotonic()+10;m.budget={'calls':0,'fee':0.,'deadline':m.deadline}
    body=dict(operation='pressure_compact',task=[],records=[],max_tokens=1024,required_reduction=10000)
    assert m._ask3(ctx,body)
    body['required_reduction']=20000
    assert m._ask3(ctx,body)
    assert len(a.calls)==1;w.close()


def test_failed_group_does_not_block_valid_local_unit_cleanup(tmp_path):
    class GroupFails(Agent):
        def summarize_prompt(self,system,user,**kw):
            if json.loads(user)['operation'] in ('pressure_compact','compact'):return 'bad group'
            return super().summarize_prompt(system,user,**kw)
    w,a=writer(tmp_path,agent=GroupFails());items=[dict(role='user',content='Fix module')]
    for i in range(3):add_turn(items,i,1000);out,info=run(w,items)
    ctx=w.sessions['session'].ctx
    assert any(s['seg']=='summary' for s in ctx.ctx)
    assert ctx.M['infinite_v3_unit']>0
    assert all(memory.get(ctx.store_dir,s['memory_key']) for s in ctx.ctx if s['seg']=='summary')
    w.close()


def test_host_frozen_factory_isolates_v3_thread_state(tmp_path):
    from ctxpress.live.factory import frozen_factory
    original=InfiniteContext(policy=frozen(),agent_mode='control',agent_model='test-agent')
    factory=frozen_factory(original)
    first,second=factory(),factory()
    assert first.policy==second.policy==original.policy
    assert first.policy is not second.policy
    assert first.cache_lock is not second.cache_lock
    assert first.local is not second.local
    first.local.budget={'calls':7}
    first.pending['sentinel']='not a shared worker'
    assert not hasattr(second.local,'budget') and second.pending=={}
    original.policy['v3']['eviction_k']=9
    assert factory().v3['eviction_k']==1
    # Exercise the exact host factory -> Rewriter boundary omitted by the
    # earlier regression, which used a constructor lambda instead.
    w=Rewriter(factory,store_dir=str(tmp_path),summarizer=Agent())
    try:
        items=[dict(role='user',content='Fix module; retain failed tests')]
        add_turn(items,0);run(w,items)
        add_turn(items,1);_,info=run(w,items)
        assert info['host_contract']['valid']
        assert info['method_decision']['selected']
    finally:
        w.close()


def test_persistent_units_do_not_review_unrelated_conversation(tmp_path):
    w,a=writer(tmp_path,agent=Agent('persistent'))
    items=[dict(role='user',content='Fix module')]
    try:
        for i in range(4):
            add_turn(items,i)
            run(w,items)
        assert any(c['operation']=='annotation' for c in a.calls)
        assert not any(c['operation']=='retention_review' for c in a.calls)
        assert not any(s['seg']=='summary' for s in w.sessions['session'].ctx.ctx)
    finally:w.close()


def test_retention_evidence_tracks_new_related_content_and_overflow():
    from types import SimpleNamespace
    m=InfiniteContextV3(policy=frozen(),agent_mode='control',agent_model='test-agent')
    old=dict(id=1,turn=1,seg='out',res=['module/a.py'],text='old failure')
    unrelated=dict(id=2,turn=2,seg='out',res=['module/b.py'],text='other progress')
    related=dict(id=3,turn=3,seg='out',res=['module/a.py'],text='test now passes')
    sim=SimpleNamespace(ctx=[old,unrelated],render=lambda s:s['text'])
    assert m._retention_evidence(sim,[old])==[]
    sim.ctx.append(related)
    evidence=m._retention_evidence(sim,[old])
    assert evidence==[dict(text='test now passes',resources=['module/a.py'])]
    sim.ctx.append(dict(related,id=4,turn=4))
    assert m._retention_evidence(sim,[old])==evidence
    m.force_pressure=True
    assert any(x['text']=='other progress' for x in m._retention_evidence(sim,[old]))


def test_related_progress_still_requests_retention_review(tmp_path):
    w,a=writer(tmp_path,agent=Agent('persistent'))
    items=[dict(role='user',content='Fix module')]
    try:
        add_turn(items,0);run(w,items)
        add_turn(items,1);run(w,items)
        assert not any(c['operation']=='retention_review' for c in a.calls)
        add_turn(items,2)
        items[-3]['arguments']=json.dumps({'cmd':'cat module/file0.py'})
        run(w,items)
        reviews=[c for c in a.calls if c['operation']=='retention_review']
        assert reviews
        assert any('module/file0.py' in str(e) for c in reviews for e in c['progress'])
        # The review is evidence, never permission to override a persistent veto.
        assert not any(s['seg']=='summary' for s in w.sessions['session'].ctx.ctx)
    finally:w.close()


def test_working_limits_leave_room_above_protected_floor_and_keep_reserve():
    m=InfiniteContextV3(policy=frozen(),agent_mode='control',agent_model='test-agent')
    m.growth=[1000]
    cap,trigger,target,reserve=m._working_limits(None,8000)
    assert cap==18000 and reserve==1000
    assert 8000<target<trigger<=cap-reserve
    assert target<=trigger-reserve
    # With no protected envelope the fitted ratios retain their original meaning.
    assert m._working_limits(None,0)==m._limits(None)
    # A growing mandatory envelope cannot silently enlarge the context window.
    cap,trigger,target,reserve=m._working_limits(None,17500)
    assert target<17500 and trigger<=cap-reserve


def test_large_protected_prompt_uses_working_space_target_and_preserves_it(tmp_path):
    w,a=writer(tmp_path)
    prompt='Exact immutable task requirement. '*1100
    items=[dict(role='user',content=prompt)]
    for i in range(4):
        add_turn(items,i,120)
        out,info=run(w,items)
        assert out['input'][0]==items[0]
        assert info['host_contract']['valid']
    d=info['method_decision']
    assert d['protected_floor']>d['configured_target']
    assert d['target']>d['protected_floor']
    assert d['target_feasible'] is True
    assert d['host_tokens_after']==pytest.approx(info['rendered_prompt_tokens'],abs=1)
    w.close()


def test_persistent_chunk_cannot_hide_later_chunks_from_retention_review(tmp_path):
    class Chunks(Agent):
        def summarize_prompt(self,system,user,**kw):
            body=json.loads(user);self.calls.append(body)
            state={k:[] for k in FIELDS}
            retention='working'
            if body['operation']=='annotation':
                part=body['records'][0]['part']
                state['unresolved']=[f'Unresolved obligation from part {part}']
                if part==1:retention='persistent'
            else:
                state['unresolved']=[v for r in body['records'] for v in r['state']['unresolved']]
            return json.dumps(dict(goal='repair',phase='implementation',retention=retention,
                completed=False,summary='All chunk obligations',state=state))
    w,a=writer(tmp_path,agent=Chunks());run(w,[dict(role='user',content='Fix module')])
    ctx=w.sessions['session'].ctx;m=ctx.method
    m.deadline=time.monotonic()+10;m.budget={'calls':0,'fee':0.,'deadline':m.deadline}
    unit=[dict(id=900,seg='reason',text='Evidence. '*(m.cfg['evidence_tokens']),res=[])]
    row=m._annotate(ctx,unit,[],True)
    chunks=[c for c in a.calls if c['operation']=='annotation']
    assert len(chunks)==chunks[0]['records'][0]['parts'] and len(chunks)>1
    assert row['retention']=='persistent'
    assert len(row['state']['unresolved'])==len(chunks)
    assert row['state']['unresolved'][-1]==f'Unresolved obligation from part {len(chunks)}'
    w.close()


def test_retry_backoff_does_not_consume_model_call_budget(tmp_path):
    from types import SimpleNamespace
    w,a=writer(tmp_path);run(w,[dict(role='user',content='Fix module')])
    ctx=w.sessions['session'].ctx;m=ctx.method
    # Transport failure, so retry is delayed without caching an invalid answer.
    ctx.summarizer=SimpleNamespace(summarize_prompt=lambda *args,**kwargs: None)
    m.deadline=time.monotonic()+10;m.budget={'calls':0,'fee':0.,'deadline':m.deadline}
    body=dict(operation='annotation',task=[],records=[],max_tokens=1024)
    assert m._ask3(ctx,body) is None
    calls=m.budget['calls'];fee=m.budget['fee']
    for _ in range(20):assert m._ask3(ctx,body) is None
    assert m.budget['calls']==calls==1 and m.budget['fee']==fee
    w.close()
