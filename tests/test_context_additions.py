"""Paper adapters exercised through the production history rewriter (fake model)."""
import copy
import json
import threading
from concurrent.futures import Future

import pytest

from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import build
from ctxpress.harness.checks.method import check
from ctxpress.methods._paper_sources.taco.seed_rules import get_seed_rules


class Summary:
    def __init__(self, answer='summary evidence'):
        self.answer, self.calls = answer, []
    def summarize_prompt(self, system, user, **kw):
        self.calls.append((user, kw))
        return self.answer


def pair(cid, name='read', args=None, text='raw evidence ' * 100):
    return [dict(type='function_call', call_id=cid, name=name, arguments=json.dumps(args or {})),
            dict(type='function_call_output', call_id=cid, output=text)]


def send(rw, history):
    before = copy.deepcopy(history)
    out, info = rw.rewrite_body(dict(input=history), 's')
    assert history == before
    return out, info


def writer(name, **args):
    return Rewriter(lambda: build({'class':name,'args':args}))


def test_taco_rule_compression_feedback_restore_and_error_protection():
    rw = writer('TACO', freeze_rules=True)
    raw = 'remote: Counting objects: 100% (5/5)\n' * 80
    h = [dict(role='user', content='TASK')] + pair('a','exec_command', {'cmd':'git fetch'},raw)
    out, _ = send(rw,h)
    assert len(str(out)) < len(str(h)) and 'TACO output_id=' in str(out)
    ctx = rw.sessions['s'].ctx
    oid = next(s['id'] for s in ctx.ctx if s['seg']=='out')
    h += pair('fb','taco_feedback',{'output_id':str(oid),'feedback':'need_full_output'},'recorded')
    out, _ = send(rw,h)
    assert raw in str(out['input']) or any(i.get('output') == raw for i in out['input'])
    assert 'Restored original' in str(out)
    h += pair('b','exec_command',{'cmd':'git fetch'},raw+'\nProcess exited with code 2')
    out,_=send(rw,h)
    assert next(i for i in out['input'] if i.get('call_id')=='b' and i['type']=='function_call_output')['output'].endswith('Process exited with code 2')
    rw.close()


def test_taco_planner_uses_shared_metered_service_and_only_terminal():
    summary=Summary(json.dumps({'selected_rule_ids':[s.rule_id for s in get_seed_rules()]}))
    rw=Rewriter(lambda:build({'class':'TACO','args':{'summary_model':'aux'}}),summarizer=summary)
    raw='remote: Counting objects: 100% (5/5)\n'*80
    h=[dict(role='user',content='TASK')]+pair('a','exec_command',{'cmd':'git fetch'},raw)
    out,_=send(rw,h)
    assert 'TACO output_id=' in str(out)
    assert summary.calls[0][1]==dict(purpose='reflect',model='aux')
    h+=pair('b','read',{'cmd':'git fetch'},raw)
    out,_=send(rw,h)
    assert any(i.get('output')==raw and i.get('call_id')=='b' for i in out['input'])
    rw.close()


@pytest.mark.parametrize('operation,args',[
 ('compress',dict(start=1,end=1,summary='compressed evidence')),
 ('delete',dict(step=1)),
 ('snippet',dict(step_id=1,reserve_prefix='BEGIN',reserve_suffix='END')),
 ('rollback',dict(step=1,reason='discard mistaken branch')),
 ('skip',{}),
])
def test_longseeker_five_tools_on_real_history(operation,args):
    rw=writer('LongSeeker')
    raw='unneeded prefix BEGIN vital evidence END unneeded suffix'
    h=[dict(role='user',content='TASK')]+pair('a',text=raw)
    out,_=send(rw,h)
    assert 'LongSeeker step 1' in str(out)
    h+=pair('ctrl','longseeker_'+operation,args,'recorded')
    out,_=send(rw,h)
    assert 'TASK' in str(out)
    if operation=='skip': assert raw in str(out)
    else: assert raw not in str(out)
    if operation=='snippet': assert 'BEGIN vital evidence END' in str(out)
    if operation=='compress': assert 'compressed evidence' in str(out)
    rw.close()


def test_longseeker_protects_user_correction_and_can_delete_compressed_block():
    rw=writer('LongSeeker')
    h=[dict(role='user',content='TASK')]+pair('a')
    send(rw,h)
    h+=pair('fold','longseeker_compress',dict(start=1,end=1,summary='first fold'),'recorded');send(rw,h)
    h+=pair('del','longseeker_delete',dict(step=1),'recorded')
    out,_=send(rw,h)
    assert 'first fold' in str(out) and 'No context change' in str(out)  # author skips folded blocks
    h+=[dict(role='user',content='NEW REQUIREMENT')]+pair('b')
    send(rw,h)
    h+=pair('rollback','longseeker_rollback',dict(step=1,reason='restart'),'recorded')
    out,_=send(rw,h)
    assert 'NEW REQUIREMENT' in str(out) and 'protected' in str(out)
    rw.close()


def test_ziprl_native_tool_preserves_pending_pairs_and_corrections():
    rw=writer('ZipRL')
    h=[dict(role='user',content='TASK')]+pair('a')
    send(rw,h)
    h+=[dict(role='user',content='CORRECTION'),pair('pending')[0]]
    h+=pair('zip','zip_context',dict(summary_level=3,summary='essential evidence'),'recorded')
    out,_=send(rw,h)
    assert 'essential evidence' in str(out) and 'raw evidence' not in str(out)
    assert 'CORRECTION' in str(out)
    assert any(s.get('call_id')=='pending' for s in rw.sessions['s'].ctx.ctx)  # renderer defers orphan calls
    h+=[pair('pending')[1]]
    send(rw,h)
    rw.close()


@pytest.mark.parametrize('role,text,trigger',[
 ('assistant','<summary_level>3</summary_level><summary>visible evidence</summary>',True),
 ('user','<summary_level>3</summary_level><summary>visible evidence</summary>',False),
 ('assistant','<think><summary_level>3</summary_level><summary>private</summary></think>',False),
 ('assistant','<summary_level>9</summary_level><summary>bad level</summary>',False),
])
def test_ziprl_visible_source_format(role,text,trigger):
    rw=writer('ZipRL');h=[dict(role='user',content='TASK')]+pair('a');send(rw,h)
    h+=[dict(role=role,content=text)];out,_=send(rw,h)
    assert ('raw evidence' not in str(out))==trigger
    rw.close()


def test_comem_async_snapshot_appends_new_turn_and_aux_model_is_captured():
    summary=Summary();rw=Rewriter(lambda:build({'class':'CoMem','args':{'summary_model':'aux','interval':1}}),summarizer=summary)
    h=[dict(role='user',content='TASK')]+pair('a');send(rw,h)
    h+=pair('b');send(rw,h)
    method=rw.sessions['s'].ctx.method
    assert method.pending
    method.pending[2].result(timeout=3)
    h+=pair('c',text='NEWEST EVIDENCE');out,_=send(rw,h)
    assert 'summary evidence' in str(out) and 'NEWEST EVIDENCE' in str(out)
    assert 'raw evidence' not in str(out)
    assert summary.calls[0][1]==dict(purpose='history',model='aux')
    assert 'NEWEST EVIDENCE' not in summary.calls[0][0]
    rw.close();assert method.executor is None


def test_comem_history_revision_does_not_install_old_summary_and_drains():
    started,release=threading.Event(),threading.Event()
    class Blocking(Summary):
        def summarize_prompt(self,*a,**kw):
            started.set();assert release.wait(5);return 'STALE'
    rw=Rewriter(lambda:build({'class':'CoMem'}),summarizer=Blocking())
    h=[dict(role='user',content='TASK')]+pair('a');send(rw,h);h+=pair('b');send(rw,h)
    assert started.wait(3)
    old=rw.sessions['s'].ctx.method
    try:
        out,_=send(rw,[dict(role='user',content='REVISED TASK')])
        assert 'STALE' not in str(out) and old.retired
    finally:
        release.set();rw.close()
    assert old.executor is None


@pytest.mark.parametrize('name,args',[('CoMem',{'lag':0}),('CoMem',{'interval':True}),('TACO',{'freeze_rules':1})])
def test_invalid_method_settings(name,args):
    with pytest.raises(ValueError):build({'class':name,'args':args})


@pytest.mark.parametrize('name',['TACO','CoMem','LongSeeker','ZipRL'])
def test_shared_mechanism_entrypoint(name):
    result=check(name,turns=6)
    assert result['contract_valid'] and result['triggered']


def test_comem_discarded_claude_fork_is_retired_and_drained():
    from ctxpress.live import anthropic
    from test_anthropic_host import conversation
    rw=Rewriter(lambda:build({'class':'CoMem'}),summarizer=Summary())
    base=conversation(2);anthropic.rewrite_messages(rw,copy.deepcopy(base))
    fork=copy.deepcopy(base)
    fork['messages'] += [dict(role='assistant',content=[dict(type='text',text='done')]),
                         dict(role='user',content=[dict(type='text',text='suggest next prompt')])]
    anthropic.rewrite_messages(rw,fork)
    method=next(iter(rw.sessions.values())).ctx.method
    real=copy.deepcopy(base)
    real['messages'] += [dict(role='assistant',content=[dict(type='text',text='done')]),
                         dict(role='user',content=[dict(type='text',text='real next prompt')])]
    _,info=anthropic.rewrite_messages(rw,real)
    assert info['forks_undone']==1 and method.retired
    rw.close();assert method.executor is None


def test_comem_failed_auxiliary_preserves_history():
    class Fail(Summary):
        def summarize_prompt(self,*args,**kw):raise RuntimeError('fixture failure')
    rw=Rewriter(lambda:build({'class':'CoMem'}),summarizer=Fail())
    h=[dict(role='user',content='TASK')]+pair('a');send(rw,h)
    h+=pair('b');send(rw,h)
    h+=pair('c');out,info=send(rw,h)
    assert 'raw evidence' in str(out) and info['summary_failed']>=1
    rw.close()


@pytest.mark.parametrize('complete,nested_count,exit_code,allowed',[(True,1,0,True),(False,1,0,False),(True,2,0,False),(True,1,2,False)])
def test_taco_code_mode_requires_one_complete_successful_execution(complete,nested_count,exit_code,allowed):
    from ctxpress.methods.taco import TACO
    call=dict(name='functions.exec',executed_tool_calls_complete=complete,
              executed_tool_calls=[dict(name='exec_command',arguments={'cmd':'git fetch'})]*nested_count)
    raw='Script output:\n'+json.dumps(dict(exit_code=exit_code,output='original'))+'\n'
    result=TACO._terminal(call,raw)
    assert (result is not None)==allowed
    if result:
        command,body,wrap=result
        assert command=='git fetch' and body=='original'
        assert json.loads(wrap('compressed').splitlines()[1])==dict(exit_code=0,output='compressed')


@pytest.mark.parametrize('name,tool,args',[
 ('ZipRL','zip_context',dict(summary_level=True,summary='x')),
 ('LongSeeker','longseeker_compress',dict(start=2,end=1,summary='x')),
 ('LongSeeker','longseeker_skip',dict(unexpected=True)),
 ('TACO','taco_feedback',dict(output_id=3,feedback='need_full_output')),
])
def test_tools_reject_bad_arguments(name,tool,args):
    _,error=build({'class':name}).call_tool(tool,args)
    assert error


def test_taco_feedback_evolves_author_rule_via_shared_service():
    class Replies(Summary):
        def summarize_prompt(self,system,user,**kw):
            self.calls.append((user,kw))
            if len(self.calls)==1:
                return json.dumps({'selected_rule_ids':['seed_git_noise']})
            return json.dumps(dict(rule_id='seed_git_noise_v2',trigger_regex='git',keep_patterns=['vital'],strip_patterns=['noise']))
    service=Replies();rw=Rewriter(lambda:build({'class':'TACO'}),summarizer=service)
    h=[dict(role='user',content='TASK')]+pair('a','exec_command',{'cmd':'git fetch'},'remote: Counting objects: 100% (5/5)\n'*80)
    send(rw,h);ctx=rw.sessions['s'].ctx
    oid=str(next(s['id'] for s in ctx.ctx if s['seg']=='out'))
    h+=pair('fb','taco_feedback',dict(output_id=oid,feedback='need_full_output'),'recorded');send(rw,h)
    assert len(service.calls)==2
    assert any(r.rule_id=='seed_git_noise_v2' for r in ctx.method.rules)
    assert next(r for r in ctx.method.rules if r.rule_id=='seed_git_noise').confidence==0
    rw.close()
