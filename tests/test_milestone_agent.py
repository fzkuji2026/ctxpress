"""Native Agent hook contracts with invocation substitutes; no Docker/models."""
import sys,time
from pathlib import Path
from types import ModuleType,SimpleNamespace
import pytest
from ctxpress.benchmarks.milestone import agent as milestone_agent, budget as milestone_budget


def modules(tmp_path,monkeypatch,events):
    source=tmp_path/'official';owner=SimpleNamespace(record={'container':'owned-agent'},
        inspect=lambda:events.append('inspect'),credentials=lambda file:events.append(('credentials',str(file))))
    class Native:
        def __init__(self,**kwargs):
            self.container_name=kwargs.get('container_name','owned-agent');self.agent_name=kwargs.get('agent_name','codex')
            self.timeout_ms=10000;self._framework=SimpleNamespace(ctxpress_private_runtime=True)
        def run(self,*args,**kwargs):events.append(('run',self.timeout_ms));return True
        def resume_session(self,session_id,message,timeout_ms=None):events.append(('resume',timeout_ms));return True
        def _handle_invocation_timeout(self,context):events.append(('author-timeout',context))
        def _append_session_history(self,row):events.append(('history',row))
    class Trial:
        def run_agent_with_recovery(self,resume_session_first=False):
            events.append(('recovery',resume_session_first));time.sleep(5)
    package=ModuleType('harness');package.__path__=[];e2e=ModuleType('harness.e2e');e2e.__path__=[]
    monkeypatch.setitem(sys.modules,'harness',package);monkeypatch.setitem(sys.modules,'harness.e2e',e2e)
    for name in ('agent_runner','run_e2e'):
        module=ModuleType('harness.e2e.'+name);module.__file__=str(source/'harness/e2e'/(name+'.py'))
        module.E2EAgentRunner=Native;setattr(e2e,name,module);monkeypatch.setitem(sys.modules,module.__name__,module)
    e2e.run_e2e.E2ETrialRunner=Trial
    monkeypatch.setattr(milestone_agent,'docker',lambda *args:events.append(('stop',args)))
    return source,owner,e2e,Native


def test_native_new_resume_refresh_and_timeout_share_budget_and_restore_author_aliases(tmp_path,monkeypatch):
    events=[];source,owner,e2e,original=modules(tmp_path,monkeypatch,events)
    current=milestone_budget.Budget(tmp_path/'sessions',5,10)
    with milestone_agent.installed(source,owner,tmp_path/'explicit-auth',current) as Agent:
        agent=Agent();assert agent.run('prompt') and agent.timeout_ms==10000
        assert agent.resume_session('session','message',timeout_ms=2000)
        assert agent._refresh_codex_credentials()
        agent._handle_invocation_timeout('resume timeout')
        assert e2e.run_e2e.E2EAgentRunner is Agent
    assert e2e.run_e2e.E2EAgentRunner is e2e.agent_runner.E2EAgentRunner is original
    assert next(value[1] for value in events if isinstance(value,tuple) and value[0]=='run')<=5000
    assert ('resume',2000) in events and ('credentials',str(tmp_path/'explicit-auth')) in events
    timeout=events.index(('author-timeout','resume timeout'))
    assert events[timeout-1][0]=='stop'


def test_exhausted_budget_cannot_launch_or_refresh_credentials(tmp_path,monkeypatch):
    events=[];source,owner,e2e,original=modules(tmp_path,monkeypatch,events)
    current=milestone_budget.Budget(tmp_path/'sessions',60,10);current.reason='max_calls'
    with milestone_agent.installed(source,owner,tmp_path/'auth',current) as Agent:
        agent=Agent()
        for action in (lambda:agent.run('prompt'),lambda:agent.resume_session('session','message'),agent._refresh_codex_credentials):
            with pytest.raises(milestone_budget.Limit):action()
    assert not events


def test_foreign_container_is_rejected_before_any_invocation(tmp_path,monkeypatch):
    events=[];source,owner,e2e,original=modules(tmp_path,monkeypatch,events)
    current=milestone_budget.Budget(tmp_path/'sessions',60,10)
    with milestone_agent.installed(source,owner,tmp_path/'auth',current) as Agent:
        with pytest.raises(ValueError,match='owned container'):Agent(container_name='foreign')
    assert not events


@pytest.mark.skipif(sys.platform!='linux',reason='native main-thread budget watchdog')
def test_author_recovery_wait_is_bounded_and_original_trial_class_is_restored(tmp_path,monkeypatch):
    events=[];source,owner,e2e,original=modules(tmp_path,monkeypatch,events)
    trial=e2e.run_e2e.E2ETrialRunner;current=milestone_budget.Budget(tmp_path/'sessions',0.1,100)
    with milestone_agent.installed(source,owner,tmp_path/'auth',current):
        with pytest.raises(milestone_budget.Limit,match='timeout'):
            e2e.run_e2e.E2ETrialRunner().run_agent_with_recovery(resume_session_first=True)
    assert e2e.run_e2e.E2ETrialRunner is trial and events==[('recovery',True)]
