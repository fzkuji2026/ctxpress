"""Real local watcher/pool draining with substitute native IO; no Docker/models."""
import concurrent.futures, json, signal, sys, threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
import pytest
from ctxpress.benchmarks.milestone import trial as milestone_trial


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='native Linux worker signal lifecycle')


def fixture(tmp_path, monkeypatch, events, *, exists=True, failure=False):
    code = tmp_path / 'official'
    class Native:
        def __init__(self):
            self.orchestrator = SimpleNamespace(container_name='owned-agent', trial_root=tmp_path)
            self.watcher_stop_event = threading.Event()
            self.watcher_thread = None
            self._watcher_exited_clean = False
            self.remove_container = True
        def cleanup(self):
            assert not self.remove_container
            events.append('author-cleanup')
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if failure:
                raise RuntimeError('substitute copy failure')
            return 'native-return'
    package = ModuleType('harness'); package.__path__ = []
    e2e = ModuleType('harness.e2e'); e2e.__path__ = []
    module = ModuleType('harness.e2e.run_e2e')
    module.__file__ = str(code / 'harness/e2e/run_e2e.py')
    module.E2ETrialRunner = Native; e2e.run_e2e = module
    for name, value in (('harness', package), ('harness.e2e', e2e), (module.__name__, module)):
        monkeypatch.setitem(sys.modules, name, value)
    active = set()
    def protect(container):
        assert container not in active
        active.add(container)
    def release(record):
        assert record['phase'] == 'author-cleanup-returned'
        assert record['watcher_joined'] and record['agent_quiesced']
        active.remove(record['container'])
    registry = SimpleNamespace(native_trials=active, protect_trial=protect, release_trial=release)
    owner = SimpleNamespace(record={'container': 'owned-agent'}, registry=registry, existing=lambda: exists,
                            stop_agent=lambda: events.append('agent-stopped'))
    return code, owner, module, Native


def pool(trial, events, release):
    started = threading.Event()
    def grade():
        started.set()
        assert release.wait(5)
        events.append('grade-finished')
    def watch():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(grade)
            assert trial.watcher_stop_event.wait(5)
        trial._watcher_exited_clean = True
        events.append('watcher-finished')
    trial.watcher_thread = threading.Thread(target=watch)
    trial.watcher_thread.start()
    assert started.wait(2)


def journal(tmp_path):
    return json.loads((tmp_path / 'ctxpress-drain.json').read_text(encoding='utf-8'))


def test_cleanup_waits_for_running_grader_before_author_cleanup_and_restores_alias(tmp_path, monkeypatch):
    events = []; code, owner, module, original = fixture(tmp_path, monkeypatch, events)
    previous = signal.getsignal(signal.SIGTERM)
    with milestone_trial.installed(code, owner, 2) as Trial:
        trial = Trial(); release = threading.Event(); pool(trial, events, release)
        def allow_grade():
            assert trial.watcher_stop_event.wait(2)
            assert events == ['agent-stopped']
            release.set()
        trigger = threading.Thread(target=allow_grade); trigger.start()
        try:
            assert trial.cleanup() == 'native-return'
        finally:
            release.set(); trigger.join(2); trial.watcher_thread.join(2)
        assert trial.remove_container
    assert module.E2ETrialRunner is original
    assert signal.getsignal(signal.SIGTERM) == previous
    assert events == ['agent-stopped', 'grade-finished', 'watcher-finished', 'author-cleanup']
    assert journal(tmp_path)['author_cleanup_returned'] and journal(tmp_path)['watcher_joined']
    assert not owner.registry.native_trials


def test_timeout_preserves_resources_and_allows_cleanup_retry_after_real_pool_exits(tmp_path, monkeypatch):
    events = []; code, owner, module, original = fixture(tmp_path, monkeypatch, events)
    previous = signal.getsignal(signal.SIGTERM)
    with milestone_trial.installed(code, owner, 0.02) as Trial:
        trial = Trial(); release = threading.Event(); pool(trial, events, release)
        try:
            with pytest.raises(milestone_trial.DrainTimeout, match='retain owned resources'):
                trial.cleanup()
            assert events == ['agent-stopped'] and trial.watcher_thread.is_alive()
            assert journal(tmp_path)['phase'] == 'drain-incomplete'
            assert not journal(tmp_path)['author_cleanup_returned']
            assert trial.remove_container and signal.getsignal(signal.SIGTERM) == previous
            assert owner.registry.native_trials == {'owned-agent'}
        finally:
            release.set(); trial.watcher_thread.join(2)
        assert trial.cleanup() == 'native-return'
    assert journal(tmp_path)['phase'] == 'author-cleanup-returned'
    assert not owner.registry.native_trials


def test_never_started_trial_needs_no_container_and_keeps_native_cleanup(tmp_path, monkeypatch):
    events = []; code, owner, module, original = fixture(tmp_path, monkeypatch, events, exists=False)
    with milestone_trial.installed(code, owner, 1) as Trial:
        assert Trial().cleanup() == 'native-return'
    assert events == ['author-cleanup'] and journal(tmp_path)['watcher_joined']


def test_author_cleanup_failure_restores_flags_and_signals(tmp_path, monkeypatch):
    events = []; code, owner, module, original = fixture(tmp_path, monkeypatch, events, failure=True)
    previous = signal.getsignal(signal.SIGTERM)
    with milestone_trial.installed(code, owner, 1) as Trial:
        trial = Trial()
        with pytest.raises(RuntimeError, match='copy failure'):
            trial.cleanup()
        assert trial.remove_container and signal.getsignal(signal.SIGTERM) == previous
    assert journal(tmp_path)['phase'] == 'author-cleanup-failed'
    assert owner.registry.native_trials == {'owned-agent'}


@pytest.mark.parametrize('change', ['foreign-container', 'foreign-source', 'zero-timeout', 'self-join'])
def test_invalid_lifecycle_fails_before_cleanup(tmp_path, monkeypatch, change):
    events = []; code, owner, module, original = fixture(tmp_path, monkeypatch, events)
    if change == 'foreign-source': module.__file__ = str(tmp_path / 'other/run_e2e.py')
    with pytest.raises(ValueError):
        with milestone_trial.installed(code, owner, 0 if change == 'zero-timeout' else 1) as Trial:
            trial = Trial()
            if change == 'foreign-container': trial.orchestrator.container_name = 'foreign'
            if change == 'self-join': trial.watcher_thread = threading.current_thread()
            trial.cleanup()
    assert not events and module.E2ETrialRunner is original
