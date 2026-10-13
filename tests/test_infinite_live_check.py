"""A rejected replay stops paid retries without fabricating successful usage."""
import json
from types import SimpleNamespace
import pytest
from ctxpress.live.context import LiveContext
from ctxpress.methods.infinite_context import InfiniteContext
from ctxpress.harness.infinite_live_check import ReplayLedger
from test_deepseek_compression import transport


@pytest.mark.parametrize('status', [401, 402, 403])
def test_replay_stops_transport_after_terminal_rejection(monkeypatch, tmp_path, status):
    sent=transport(monkeypatch,status=status)
    ledger=ReplayLedger(tmp_path/'auxiliary.jsonl')
    ctx=LiveContext(InfiniteContext(),summarizer=SimpleNamespace(record=ledger))
    for _ in range(3):
        assert ctx.model_text('json','data',purpose='infinite_context_compression',model='deepseek-flash') is None
    assert len(ledger.records)==1
    assert ledger.records[0]['status']==status
    assert ledger.records[0]['completed'] is False
    assert 'usage' not in ledger.records[0]
    assert len((tmp_path/'auxiliary.jsonl').read_text().splitlines())==1
    with pytest.raises(RuntimeError,match=f'HTTP {status}'):
        ledger.check_available()
    assert 'test-secret' not in json.dumps(ledger.records)
    assert sent['closed']


def test_transient_errors_do_not_permanently_disable_replay(tmp_path):
    ledger=ReplayLedger(tmp_path/'auxiliary.jsonl')
    ledger({'status':503,'completed':False})
    ledger.check_available()
    assert ledger.terminal_status is None
    # Independent runs do not inherit another run's billing failure.
    ledger({'status':402,'completed':False})
    ReplayLedger(tmp_path/'another.jsonl').check_available()


@pytest.mark.parametrize('failure_phase', ['rewrite', 'close'])
def test_replay_entrypoint_marks_provider_rejection_incomplete(monkeypatch, tmp_path, failure_phase):
    import sys
    from ctxpress.harness import infinite_live_check as replay
    from test_infinite_v3 import frozen
    policy_path=tmp_path/'policy.json'
    policy_path.write_text(json.dumps(frozen()))
    rollout=tmp_path/'rollout.jsonl';rollout.write_text('')
    root=tmp_path/'run'
    state={'rewrites':0,'closed':False}
    class Writer:
        def __init__(self,*args,**kwargs):
            self.record=kwargs['summarizer'].record
            self.sessions={'regression':SimpleNamespace(ctx=SimpleNamespace(
                M={'infinite_compacted_records':0},method=SimpleNamespace(last_decision={}))) }
        def rewrite_body(self,body,session):
            state['rewrites']+=1
            if failure_phase=='rewrite':self.record({'status':402,'completed':False})
            return body,{'changed':False}
        def close(self):
            state['closed']=True
            if failure_phase=='close':self.record({'status':402,'completed':False})
    monkeypatch.setattr(replay,'Rewriter',Writer)
    monkeypatch.setattr(replay,'requests',lambda _:iter([{}, {}, {}]))
    monkeypatch.setattr(sys,'argv',['replay','--rollout',str(rollout),'--policy',str(policy_path),
        '--directory',str(root),'--agent-mode','control'])
    with pytest.raises(SystemExit) as error:replay.main()
    report=json.loads((root/'report.json').read_text())
    assert error.value.code==1 and state['closed']
    assert state['rewrites']==(1 if failure_phase=='rewrite' else 3)
    assert report['completed'] is False and report['provider_terminal_status']==402
    assert report['auxiliary_calls']==report['auxiliary_failed']==1
    assert '402' in report['error']
