"""Exit evidence survives author parser failure without changing grading."""
import json
import types
import pytest
from ctxpress.benchmarks.milestone import grading_diagnostics as diagnostics


@pytest.mark.parametrize('code,stderr,cause', [
    (-1, 'Command timed out after 3600 seconds', 'timeout'),
    (-1, 'terminated by signal', 'nonzero_exit'),
    (2, 'SECRET PRIVATE STDERR', 'nonzero_exit'),
])
def test_failed_parser_preserves_exit_evidence(tmp_path, code, stderr, cause):
    result = (code, 'SECRET STDOUT', stderr)
    calls = []
    class Runner:
        name = 'native'
        def run(self, *args, **kwargs):
            calls.append((args, kwargs))
            return result
    def author(runner, **kwargs):
        assert runner.name == 'native'
        assert runner.run('private command', timeout=3600) is result
        raise RuntimeError('author refused partial report')
    for _ in range(2):
        with pytest.raises(RuntimeError, match='partial report'):
            diagnostics.observed_tests(author)(Runner(), output_dir=tmp_path, milestone_id='M1')
    records = list(tmp_path.glob('ctxpress-runner-exit-*.json'))
    assert len(records) == 2  # retries must not overwrite evidence
    for path in records:
        row = json.loads(path.read_text())
        assert row['cause'] == cause and row['returncode'] == code
        assert row['timeout_seconds'] == (3600 if cause == 'timeout' else None)
        assert row['elapsed_seconds'] >= 0
        assert 'SECRET' not in path.read_text() and 'private command' not in path.read_text()
    assert calls == [(('private command',), {'timeout':3600})]*2


def test_success_has_identical_result_and_no_failure_artifact(tmp_path):
    result = (0, 'ok', '')
    runner = types.SimpleNamespace(run=lambda *a, **k: result)
    author = lambda r, **k: r.run('test')
    assert diagnostics.observed_tests(author)(runner, output_dir=tmp_path) is result
    assert not list(tmp_path.glob('ctxpress-runner-exit-*.json'))
    state = json.loads(next(tmp_path.glob('ctxpress-runner-state-*.json')).read_text())
    assert state['phase'] == 'returned' and state['returncode'] == 0


def test_write_failure_does_not_replace_author_result(tmp_path, monkeypatch, caplog):
    def fail(*args): raise OSError('disk full')
    monkeypatch.setattr(diagnostics, 'atomic_json', fail)
    result = (-1, '', 'Command timed out after 1 seconds')
    runner = types.SimpleNamespace(run=lambda: result)
    assert diagnostics.observed_tests(lambda r, **k: r.run())(runner, output_dir=tmp_path) is result
    assert 'Could not persist' in caplog.text


def test_binding_is_scoped_to_method_globals(tmp_path):
    original = lambda runner, **kwargs: runner.run()
    scope = {'run_single_state_tests': original}
    exec('def run_tests(self):\n return run_single_state_tests(self.runner, output_dir=self.output_dir)\n', scope)
    bound = diagnostics.bind(scope['run_tests'])
    result = (-1, '', 'Command timed out after 2 seconds')
    obj = types.SimpleNamespace(runner=types.SimpleNamespace(run=lambda: result), output_dir=tmp_path)
    assert bound(obj) is result
    assert scope['run_single_state_tests'] is original
    assert len(list(tmp_path.glob('ctxpress-runner-exit-*.json'))) == 1


def test_collector_does_not_follow_diagnostic_symlinks(tmp_path):
    evaluation = tmp_path/'trial/evaluation'
    evaluation.mkdir(parents=True)
    outside = tmp_path/'outside'
    outside.mkdir()
    (outside/'ctxpress-runner-exit-secret.json').write_text('private data')
    try:
        (evaluation/'linked').symlink_to(outside, target_is_directory=True)
        (evaluation/'ctxpress-runner-exit-linked.json').symlink_to(outside/'ctxpress-runner-exit-secret.json')
    except OSError:
        pytest.skip('symlinks unavailable')
    result = diagnostics.collect(tmp_path/'trial')
    assert not result['records'] and len(result['errors']) == 2
    assert 'private data' not in json.dumps(result)


def test_live_runner_state_precedes_command_and_survives_exception(tmp_path):
    error = KeyboardInterrupt()
    def run(script, timeout):
        state = json.loads(next(tmp_path.glob('ctxpress-runner-state-*.json')).read_text())
        assert state['phase'] == 'running' and state['timeout_seconds'] == 14400
        assert 'finished_wall' not in state and script not in json.dumps(state)
        raise error
    runner = types.SimpleNamespace(run=run)
    def author(r, **kwargs): return r.run('private command', 14400)
    with pytest.raises(KeyboardInterrupt) as caught:
        diagnostics.observed_tests(author)(runner, output_dir=tmp_path, milestone_id='M1')
    assert caught.value is error
    state = json.loads(next(tmp_path.glob('ctxpress-runner-state-*.json')).read_text())
    assert state['phase'] == 'raised' and state['error_type'] == 'KeyboardInterrupt'
    assert state['elapsed_seconds'] >= 0 and state['finished_wall'] >= state['started_wall']
