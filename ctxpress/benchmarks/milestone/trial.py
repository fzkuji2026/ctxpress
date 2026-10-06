"""Drain the native watcher before author cleanup or owned resource removal."""
from __future__ import annotations
import contextlib, signal, threading
from pathlib import Path
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.core import artifacts as artifact_io


class DrainTimeout(RuntimeError):
    """The worker must retain its resources while a native grader is active."""


def _seconds(value):
    eval_plan._positive(value, 'native grading shutdown timeout', integer=False)
    return float(value)


def drain(trial, owner, seconds):
    """Stop new Agent work and wait for the author's executor to finish.

    The author watcher owns its ThreadPoolExecutor context. Joining the watcher
    also waits for running evaluations; no parallel scoring policy is replaced.
    A timeout records an incomplete drain and forbids subsequent cleanup.
    """
    seconds = _seconds(seconds)
    if trial.orchestrator.container_name != owner.record['container']:
        raise ValueError('native trial cleanup requires its owned Agent')
    watcher, stop = trial.watcher_thread, trial.watcher_stop_event
    if not isinstance(stop, threading.Event) or watcher is not None and not isinstance(watcher, threading.Thread):
        raise ValueError('native watcher lifecycle differs from the author contract')
    if watcher is threading.current_thread():
        raise ValueError('native watcher cannot join itself during cleanup')
    path = Path(trial.orchestrator.trial_root) / 'ctxpress-drain.json'
    record = dict(schema='ctxpress.eval.milestone_drain', version=1,
                  container=owner.record['container'], phase='stopping-agent',
                  agent_quiesced=False, watcher_present=watcher is not None,
                  watcher_joined=False, author_cleanup_returned=False)
    artifact_io.atomic_json(path, record)
    try:
        if owner.existing():
            owner.stop_agent()
        record.update(agent_quiesced=True, phase='draining-grades')
        artifact_io.atomic_json(path, record)
        stop.set()
        if watcher is not None:
            # An existing Thread object that never started is a contract error,
            # not evidence that its work has finished.
            watcher.join(seconds)
            if watcher.is_alive():
                raise DrainTimeout('native grading workers are still active; retain owned resources')
        record.update(phase='drained', watcher_joined=True,
                      watcher_exited_clean=bool(getattr(trial, '_watcher_exited_clean', False)))
        artifact_io.atomic_json(path, record)
        return path, record
    except BaseException as error:
        record.update(phase='drain-incomplete', error_type=type(error).__name__)
        artifact_io.atomic_json(path, record)
        raise


@contextlib.contextmanager
def installed(code, owner, shutdown_seconds):
    """Install after the Agent hook so its cumulative budget remains active.

    Author cleanup captures statistics/testbed and releases its trial lock. Its
    unchecked container removal is disabled; the resource registry owns that
    separate step, which the worker may perform only after this drain succeeds.
    """
    from harness.e2e import run_e2e
    if Path(run_e2e.__file__).resolve() != Path(code).resolve() / 'harness/e2e/run_e2e.py':
        raise ValueError('native trial cleanup imported an undeclared source')
    seconds = _seconds(shutdown_seconds)
    original = run_e2e.E2ETrialRunner

    class Trial(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if self.orchestrator.container_name != owner.record['container']:
                raise ValueError('native trial cleanup requires its owned Agent')
            owner.registry.protect_trial(owner.record['container'])

        def cleanup(self):
            if threading.current_thread() is not threading.main_thread():
                raise ValueError('native trial cleanup requires the main worker thread')
            previous_signal = signal.getsignal(signal.SIGTERM)
            previous_remove = self.remove_container
            # Match the author's bounded cleanup protection, then restore the
            # caller's signal behavior even on a drain or cleanup failure.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            record = path = None
            try:
                path, record = drain(self, owner, seconds)
                self.remove_container = False
                result = super().cleanup()
                record.update(phase='author-cleanup-returned', author_cleanup_returned=True)
                artifact_io.atomic_json(path, record)
                owner.registry.release_trial(record)
                return result
            except BaseException as error:
                if record is not None:
                    record.update(phase='author-cleanup-failed', error_type=type(error).__name__)
                    artifact_io.atomic_json(path, record)
                raise
            finally:
                self.remove_container = previous_remove
                signal.signal(signal.SIGTERM, previous_signal)

    run_e2e.E2ETrialRunner = Trial
    try:
        yield Trial
    finally:
        run_e2e.E2ETrialRunner = original
