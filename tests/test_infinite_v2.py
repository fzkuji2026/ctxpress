import copy
import json
import pytest
from ctxpress.core import infinite_policy as policy
from ctxpress.live.context import LiveContext
from ctxpress.methods.infinite_context import InfiniteContext
from ctxpress.methods.infinite_v2 import InfiniteContextV2
from test_infinite_adaptive import bundle, Agent


def frozen(**changes):
    b = bundle(**changes)
    b['version'] = 2
    b['settings'].update(summary_tokens=128, evidence_tokens=1024)
    b['constraints']['capacity'] = 10000
    for p in b['parameters']:
        if p['name'] == 'window':
            p['value'] = 10000
    return policy.seal(b)


def context(tmp_path, b=None, agent=None):
    b = b or frozen()
    ctx = LiveContext(InfiniteContext(policy=b, agent_mode='control', agent_model='test-agent'),
                      store_dir=str(tmp_path), summarizer=agent or Agent())
    ctx.add_message('user', 'Must preserve requirements', fixed=True)
    ctx.before_request()
    return ctx


def test_version_dispatch_preserves_v1():
    assert type(InfiniteContext(policy=bundle())) is InfiniteContext
    assert isinstance(InfiniteContext(policy=frozen()), InfiniteContextV2)


def test_horizon_does_not_collapse_after_training_support():
    m = InfiniteContext(policy=frozen())
    assert m._horizon(1000) > 10
    assert m._horizon(1000) == m._horizon(1001)


def test_host_pressure_reviews_all_chunks_and_reduces_capacity(tmp_path):
    class Chunks(Agent):
        def __init__(self):
            super().__init__()
            self.original_parts = []
        def summarize_prompt(self, system, user, **kwargs):
            data = json.loads(user)
            self.original_parts.append(data['records'][0]['text'])
            return json.dumps({'records':[{'id': data['records'][0]['id'], 'retention':'working', 'summary':'Verified errors and source references retained.'}]})
    agent = Chunks()
    ctx = context(tmp_path, agent=agent)
    text = ''.join(f'line {i}: observation\n' for i in range(1500))
    ctx.add_call('a', 'cat old.py')
    ctx.add_output('a', text, chars=60000)
    # Host encoding alone pushes above capacity; mark previously consumed output.
    for s in ctx.ctx:
        s['born'] = 0
    ctx.before_request()
    assert ''.join(agent.original_parts) == text
    assert ctx.method.last_decision['pressure']
    assert ctx.method.last_decision['host_tokens_after'] < 10000
    assert ctx.M['infinite_compacted_records'] == 1
    assert ctx.sent()[0]['text'] == 'Must preserve requirements'


def test_pressure_prices_reuse_instead_of_deadlocking(tmp_path):
    ctx = context(tmp_path)
    ctx.add_call('a', 'cat old.py');ctx.add_output('a', 'abc\n'*11000)
    for s in ctx.ctx:s['born'] = 0
    ctx.method._risk = lambda *args: (.99, 1, 0)
    ctx.before_request()
    assert ctx.M['infinite_compacted_records'] == 1
    assert ctx.method.last_decision['selected'][0]['recovery_probability'] == .99


@pytest.mark.parametrize('retention,invalid', [('persistent',False), ('working',True)])
def test_pressure_cannot_override_agent_veto_or_invalid_output(tmp_path, retention, invalid):
    ctx = context(tmp_path, agent=Agent(retention=retention, invalid=invalid))
    ctx.add_call('a','cat old.py');ctx.add_output('a','abc\n'*11000)
    for s in ctx.ctx:s['born']=0
    from ctxpress.live.contract import HostContractError
    with pytest.raises(HostContractError):ctx.before_request()
    assert not ctx.M['infinite_compacted_records']


def test_failed_chunk_leaves_entire_record_original(tmp_path):
    class Fails(Agent):
        def summarize_prompt(self, system, user, **kwargs):
            d=json.loads(user)
            if d['part'] == 2:return 'invalid'
            return super().summarize_prompt(system,user,**kwargs)
    ctx = context(tmp_path, agent=Fails())
    ctx.add_call('a','cat old.py');ctx.add_output('a','abc\n'*11000)
    for s in ctx.ctx:s['born']=0
    from ctxpress.live.contract import HostContractError
    with pytest.raises(HostContractError):ctx.before_request()
    assert not ctx.M['infinite_compacted_records']


def test_growth_reserve_moves_trigger_earlier(tmp_path):
    ctx = context(tmp_path, b=frozen(trigger_ratio=.9))
    ctx.method.growth=[4000]*5
    cap, trigger, target, reserve=ctx.method._limits(ctx)
    assert trigger==6000 and target<trigger and reserve==4000


def test_disabled_policy_never_calls_agent(tmp_path):
    b=frozen();b['evidence']['enabled']=False;b=policy.seal(b)
    a=Agent();ctx=context(tmp_path,b=b,agent=a)
    ctx.add_call('a','cat old.py');ctx.add_output('a','abc\n'*1000)
    ctx.before_request();ctx.before_request()
    assert not a.calls and not ctx.M['infinite_compacted_records']


def test_control_does_not_pay_before_trigger(tmp_path):
    a=Agent();ctx=context(tmp_path,agent=a)
    ctx.add_call('a','cat old.py');ctx.add_output('a','abc\n'*100)
    ctx.before_request();ctx.before_request()
    assert not a.calls and ctx.method.last_decision['rejections']=={'below_trigger':1}


def test_invalid_content_cached_but_transport_failure_retryable(tmp_path):
    a=Agent(invalid=True);ctx=context(tmp_path,agent=a)
    body={'records':[{'id':1,'text':'data'}],'summary_tokens':128,'summary_max_chars':512}
    assert ctx.method._ask(ctx,body)[0] is None
    assert ctx.method._ask(ctx,body)[1] == 0
    assert len(a.calls)==1
    a.invalid=False
    body['records'][0]['text']='new evidence'
    assert ctx.method._ask(ctx,body)[0] is not None
