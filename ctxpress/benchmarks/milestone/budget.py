"""One wall/tool budget across native fresh, resumed and recovered invocations."""
from __future__ import annotations
import contextlib,math,os,signal,threading,time
from pathlib import Path
from ctxpress.harness.runtime.codex_agent import CallProgress
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.core import artifacts as artifact_io


class Limit(KeyboardInterrupt):
    """Do not let the author's broad Exception retries reset an exhausted budget."""


class Budget:
    def __init__(self,sessions,seconds,max_calls,defer_start=False):
        eval_plan._positive(seconds,'native total timeout',integer=False)
        eval_plan._positive(max_calls,'native tool call budget',integer=True)
        if type(defer_start) is not bool:raise ValueError('native deferred budget must be boolean')
        self.seconds,self.max_calls=seconds,max_calls;self.started=None if defer_start else time.monotonic()
        self.progress=CallProgress(sessions);self.count=0;self.complete=False;self.reason=None
        self.lock=threading.RLock();self.watching=False

    def poll(self):
        with self.lock:
            self.count,self.complete=self.progress.update()
            if self.reason is None:
                if self.started is not None and time.monotonic()-self.started>=self.seconds:self.reason='timeout'
                elif self.count>=self.max_calls and self.complete:self.reason='max_calls'
            return self.reason

    def remaining_ms(self,requested):
        if type(requested) is not int or requested<=0:raise ValueError('native invocation timeout must be positive milliseconds')
        self.begin()
        self.poll()
        if self.reason:raise Limit(self.reason)
        return min(requested,max(1,math.ceil((self.seconds-(time.monotonic()-self.started))*1000)))

    def write(self,path):
        self.poll()
        artifact_io.atomic_json(Path(path),dict(schema='ctxpress.eval.milestone_budget',version=1,
            seconds=self.seconds,max_calls=self.max_calls,budget_started=self.started is not None,
            elapsed_seconds=0.0 if self.started is None else time.monotonic()-self.started,
            tool_calls=self.count,all_tool_outputs_observed=self.complete,stop_reason=self.reason))

    @contextlib.contextmanager
    def watch(self):
        """Interrupt native sleeps/waits as well as Docker exec in an isolated worker.

        Scope this around Agent/recovery scheduling, not final grading teardown.
        The signal handler performs no IO/locking; main-thread cleanup may run
        even if the deadline interrupted a call while it held the budget lock.
        """
        if (not hasattr(signal,'SIGALRM') or threading.current_thread() is not threading.main_thread() or
                self.watching or signal.getitimer(signal.ITIMER_REAL)!=(0.0,0.0)):
            raise ValueError('native budget watchdog requires an isolated main thread without another timer')
        self.begin()
        previous=signal.getsignal(signal.SIGALRM);stop=threading.Event();errors=[]
        def interrupt(signum,frame):
            # Native run catches KeyboardInterrupt and begins cleanup inside
            # this scope. Disarm both deadline and any concurrent monitor send
            # so a second alarm cannot interrupt that cleanup.
            signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,signal.SIG_IGN)
            if self.reason is None:self.reason='timeout'
            raise Limit(self.reason)
        def monitor():
            try:
                while not stop.wait(0.1):
                    if self.poll() and not stop.is_set():
                        os.kill(os.getpid(),signal.SIGALRM);return
            except Exception as error:
                errors.append(error);self.reason='budget_observation_error'
                if not stop.is_set():os.kill(os.getpid(),signal.SIGALRM)
        self.watching=True;thread=threading.Thread(target=monitor,name='ctxpress-native-budget',daemon=True)
        signal.signal(signal.SIGALRM,interrupt)
        try:
            if self.poll():raise Limit(self.reason)
            signal.setitimer(signal.ITIMER_REAL,max(0.001,self.seconds-(time.monotonic()-self.started)))
            thread.start()
            yield self
        finally:
            signal.signal(signal.SIGALRM,signal.SIG_IGN);stop.set();signal.setitimer(signal.ITIMER_REAL,0)
            if thread.ident is not None:thread.join(timeout=5)
            self.watching=False
            if thread.is_alive():raise RuntimeError('native budget monitor did not stop')
            signal.signal(signal.SIGALRM,previous)
        if errors:raise RuntimeError('native session budget observation failed') from errors[0]

    def begin(self):
        """Start once, at Agent scheduling rather than environment preparation."""
        with self.lock:
            if self.started is None:self.started=time.monotonic()
