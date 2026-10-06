"""Scoped native Agent runtime: owned credentials, cumulative budget and groups."""
from __future__ import annotations
import contextlib
from pathlib import Path
from ctxpress.benchmarks.milestone.resources import docker


def stop_invocation(owner):
    owner.inspect()
    docker('exec','--user','root','-e','PYTHONPATH=/ctxpress-runtime',owner.record['container'],
        'python3','-m','ctxpress.harness.runtime.agent_process','--pid-file','/ctxpress-private/agent.json','--stop')


@contextlib.contextmanager
def installed(code,owner,auth_file,budget):
    from harness.e2e import agent_runner,run_e2e
    code=Path(code).resolve()
    for module in (agent_runner,run_e2e):
        if Path(module.__file__).resolve()!=code/'harness/e2e'/(module.__name__.rsplit('.',1)[1]+'.py'):
            raise ValueError('native Agent hook imported an undeclared source')
    original=agent_runner.E2EAgentRunner
    original_trial=run_e2e.E2ETrialRunner

    class Agent(original):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            if (self.container_name!=owner.record['container'] or self.agent_name!='codex' or
                    getattr(self._framework,'ctxpress_private_runtime',False) is not True):
                raise ValueError('native Agent must use its owned container and private framework')

        def _refresh_codex_credentials(self):
            budget.remaining_ms(self.timeout_ms)
            owner.credentials(auth_file)
            self._append_session_history({'event':'credentials_refreshed','provider':'codex'})
            return True

        def run(self,*args,**kwargs):
            previous=self.timeout_ms;self.timeout_ms=budget.remaining_ms(previous)
            try:return super().run(*args,**kwargs)
            finally:
                self.timeout_ms=previous;stop_invocation(owner)

        def resume_session(self,session_id,message,timeout_ms=None):
            bounded=budget.remaining_ms(self.timeout_ms if timeout_ms is None else timeout_ms)
            try:return super().resume_session(session_id,message,timeout_ms=bounded)
            finally:stop_invocation(owner)

        def _handle_invocation_timeout(self,context):
            stop_invocation(owner)
            return super()._handle_invocation_timeout(context)

    class Trial(original_trial):
        def run_agent_with_recovery(self,resume_session_first=False):
            # Enclose the author's whole recovery loop, including native
            # backoffs and waits. Leave final grading/container teardown out
            # of the timer so cancellation cannot interrupt checked cleanup.
            with budget.watch():
                return super().run_agent_with_recovery(resume_session_first=resume_session_first)

    originals=[]
    try:
        for module,name,value in ((agent_runner,'E2EAgentRunner',Agent),(run_e2e,'E2EAgentRunner',Agent),
                                  (run_e2e,'E2ETrialRunner',Trial)):
            originals.append((module,name,getattr(module,name)));setattr(module,name,value)
        yield Agent
    finally:
        for module,name,value in reversed(originals):setattr(module,name,value)
