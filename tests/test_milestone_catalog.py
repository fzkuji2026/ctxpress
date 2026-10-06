"""Optional frozen catalog binding through native dispatch; no Docker or models."""
import contextlib
import json
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest
from ctxpress.benchmarks import harbor_driver, milestone_codex, milestone_driver
from ctxpress.harness import codex_binary, codex_catalog, milestone_runner as runner, milestone_version
from test_milestone_codex import OfficialFixture, private_settings, settings
from test_milestone_runner import fixture as runner_fixture

MODEL = 'gpt-6.1-sol'
RUNNER_VALIDATE = runner.validate


def catalog_file(tmp_path, value=None):
    path = tmp_path / 'models catalog.json'
    if value is None:
        value = {'models': [{'slug': MODEL, 'base_instructions': 'Synthetic test instructions.',
                            'supported_reasoning_levels': [{'effort': 'low', 'description': 'Test'}]}]}
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def offline_preflight(monkeypatch, events):
    def check(bindir, path, model, reasoning=None):
        events.append('catalog-preflight')
        # Exercise the real selection/JSON validator; don't launch a CLI in unit tests.
        return dict(codex_catalog.inspect(path, model, reasoning), parser_verified=True, model_calls=0)
    monkeypatch.setattr(codex_catalog, 'preflight', check)


def driver_inputs(tmp_path, monkeypatch, with_catalog=True):
    events = []
    payload = {'resources': 'fixture', 'original_task': {}}
    lock = {'native_data_version': {}, 'runtime': {'python': '/frozen/python'}}
    monkeypatch.setattr(milestone_driver, 'request', lambda *args: payload)
    monkeypatch.setattr(milestone_driver.task_resources, 'read', lambda *args: (lock, None))
    monkeypatch.setattr(milestone_version, 'validate', lambda *args: None)
    monkeypatch.setattr(milestone_driver, 'check_images', lambda *args: events.append('images'))
    monkeypatch.setattr(codex_binary, 'preflight', lambda *args: events.append('binary') or
                        {'version': '0.159.0-alpha.12.1'})
    monkeypatch.setattr(harbor_driver, 'method_inputs', lambda entry, path: dict(entry))
    auth = tmp_path / 'synthetic-auth.json'
    auth.write_text('synthetic', encoding='utf-8')
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE', str(auth))
    config = {'environment': {'bindir': str(tmp_path)}, 'model': MODEL, 'reasoning': 'low',
              'run': {'grade': True}}
    if with_catalog:
        config['environment']['model_catalog'] = str(catalog_file(tmp_path))
    offline_preflight(monkeypatch, events)
    return config, payload, events


@pytest.mark.linux_only
@pytest.mark.parametrize('with_catalog', [False, True])
def test_driver_propagates_only_explicit_catalog_and_preflights_before_images(tmp_path, monkeypatch, with_catalog):
    config, payload, events = driver_inputs(tmp_path, monkeypatch, with_catalog)
    result, runtime, auth = milestone_driver.prepare_execution(
        {}, {'class': 'NoCompaction'}, config, {'task': {}, 'compact_limit': 230000}, tmp_path, 'owner')
    assert result is payload and runtime == {'python': '/frozen/python'}
    assert auth.endswith('synthetic-auth.json')
    if with_catalog:
        assert result['execution']['model_catalog'] == config['environment']['model_catalog']
        assert result['execution']['model_catalog_sha256'] == codex_catalog.inspect(
            config['environment']['model_catalog'], MODEL, 'low')['sha256']
        assert events == ['catalog-preflight', 'images', 'binary']
    else:
        assert 'model_catalog' not in result['execution']
        assert events == ['images', 'binary']
    assert result['execution']['model'] == MODEL


@pytest.mark.linux_only
@pytest.mark.parametrize('contents,match', [
    ('{', 'invalid model catalog JSON'),
    ('{"models":[]}', 'nonempty models'),
    ('{"models":[{"slug":"different-model"}]}', 'exact selected model'),
])
def test_driver_bad_catalog_fails_before_images_binary_and_auth(tmp_path, monkeypatch, contents, match):
    config, payload, events = driver_inputs(tmp_path, monkeypatch)
    Path(config['environment']['model_catalog']).write_text(contents, encoding='utf-8')
    monkeypatch.delenv('CTXPRESS_CODEX_AUTH_FILE')
    with pytest.raises(ValueError, match=match):
        milestone_driver.prepare_execution(
            {}, {'class': 'NoCompaction'}, config, {'task': {}, 'compact_limit': 230000}, tmp_path, 'owner')
    assert events == []
    assert 'execution' not in payload


def runner_inputs(tmp_path, monkeypatch):
    source, request, events = runner_fixture(tmp_path, monkeypatch)
    lock = runner.validate(request)
    request.update(resources='fixture', original_task=request['task'])
    request['execution'].update(via=None, model=MODEL, reasoning='low',
                                model_catalog=str(catalog_file(tmp_path)))
    request['execution']['model_catalog_sha256'] = codex_catalog.inspect(
        request['execution']['model_catalog'], MODEL, 'low')['sha256']
    monkeypatch.setattr(runner, 'validate', RUNNER_VALIDATE)
    monkeypatch.setattr(runner.task_resources, 'read', lambda *args: (lock, None))
    monkeypatch.setattr(runner.milestone_version, 'validate', lambda *args: None)
    offline_preflight(monkeypatch, events)
    return source, request, events, lock


@pytest.mark.linux_only
def test_runner_preflights_exact_selection_and_passes_safe_identity_to_native_logs(tmp_path, monkeypatch):
    source, request, events, lock = runner_inputs(tmp_path, monkeypatch)
    captured = {}

    @contextlib.contextmanager
    def installed(hook_settings):
        captured.update(hook_settings)
        events.append('codex')
        yield
        events.append('codex-restore')

    monkeypatch.setattr(runner.milestone_codex, 'installed', installed)
    expected = codex_catalog.inspect(request['execution']['model_catalog'], MODEL, 'low')
    state = runner.run(request, source)
    assert events.index('catalog-preflight') < events.index('registry') < events.index('channel')
    assert captured['model_catalog'] == request['execution']['model_catalog']
    assert captured['private_runtime'] is True
    assert state['model_catalog'] == expected
    assert json.loads((Path(request['folder']) / 'native-execution.json').read_text(encoding='utf-8'))['model_catalog'] == expected
    assert json.loads((tmp_path / 'trial/trial_metadata.json').read_text(encoding='utf-8'))['ctxpress_model_catalog'] == expected
    assert set(expected) == {'path', 'sha256', 'model', 'entry_sha256'}
    assert 'Synthetic test instructions.' not in json.dumps(state)


@pytest.mark.linux_only
@pytest.mark.parametrize('contents,match', [
    ('{', 'invalid model catalog JSON'),
    ('{"models":[{"slug":"different-model"}]}', 'exact selected model'),
])
def test_runner_rejects_bad_catalog_before_native_preparation_auth_or_resources(tmp_path, monkeypatch, contents, match):
    source, request, events, lock = runner_inputs(tmp_path, monkeypatch)
    Path(request['execution']['model_catalog']).write_text(contents, encoding='utf-8')
    monkeypatch.delenv('CTXPRESS_CODEX_AUTH_FILE')
    monkeypatch.setattr(runner.milestone_native, 'prepare', lambda *args: pytest.fail('native preparation started'))
    with pytest.raises(ValueError, match=match):
        runner.run(request, source)
    assert events == []
    assert not (Path(request['folder']) / 'native-execution.json').exists()


@pytest.mark.linux_only
def test_runner_rejects_missing_catalog_file_without_resource_startup(tmp_path, monkeypatch):
    source, request, events, lock = runner_inputs(tmp_path, monkeypatch)
    Path(request['execution']['model_catalog']).unlink()
    with pytest.raises(ValueError, match='explicit regular'):
        runner.run(request, source)
    assert events == []


@pytest.mark.linux_only
def test_runner_rejects_unsupported_reasoning_before_resources(tmp_path, monkeypatch):
    source, request, events, lock = runner_inputs(tmp_path, monkeypatch)
    request['execution']['reasoning'] = 'high'
    with pytest.raises(ValueError, match='reasoning level'):
        runner.run(request, source)
    assert events == []


@pytest.mark.linux_only
@pytest.mark.parametrize('path', [None, '', 42, [], 'relative/models.json', 'C:/models.json', '/path:models.json'])
def test_optional_catalog_path_is_strictly_validated_before_cli(tmp_path, monkeypatch, path):
    source, request, events, lock = runner_inputs(tmp_path, monkeypatch)
    request['execution']['model_catalog'] = path
    with pytest.raises(ValueError, match='model_catalog file path'):
        runner.validate(request)
    assert events == []
    cfg = settings()
    cfg['model_catalog'] = path
    with pytest.raises(ValueError, match='mount path'):
        milestone_codex.framework(OfficialFixture, cfg)


@pytest.mark.linux_only
def test_runner_keeps_legacy_requests_valid_and_rejects_unrelated_optional_keys(tmp_path, monkeypatch):
    source, request, events, lock = runner_inputs(tmp_path, monkeypatch)
    request['execution'].pop('model_catalog')
    request['execution'].pop('model_catalog_sha256')
    assert runner.validate(request) is lock
    assert events == []
    request['execution']['model_catalog_untrusted'] = 'a' * 64
    with pytest.raises(ValueError, match='invalid native execution settings'):
        runner.validate(request)


@pytest.mark.linux_only
@pytest.mark.parametrize('missing', ['model_catalog', 'model_catalog_sha256'])
def test_runner_rejects_partial_catalog_binding(tmp_path, monkeypatch, missing):
    source, request, events, lock = runner_inputs(tmp_path, monkeypatch)
    request['execution'].pop(missing)
    with pytest.raises(ValueError, match='declared together'):
        runner.validate(request)
    assert events == []


@pytest.mark.linux_only
@pytest.mark.parametrize('digest', [None, 42, '', 'a' * 63, 'z' * 64, 'A' * 64])
def test_runner_rejects_invalid_digest_before_preflight(tmp_path, monkeypatch, digest):
    source, request, events, lock = runner_inputs(tmp_path, monkeypatch)
    request['execution']['model_catalog_sha256'] = digest
    with pytest.raises(ValueError, match='model_catalog SHA-256'):
        runner.validate(request)
    assert events == []


@pytest.mark.linux_only
def test_runner_rejects_changed_bound_file_before_preflight(tmp_path, monkeypatch):
    source, request, events, lock = runner_inputs(tmp_path, monkeypatch)
    path = Path(request['execution']['model_catalog'])
    path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='differs from bound SHA-256'):
        runner.run(request, source)
    assert events == []


@pytest.mark.linux_only
@pytest.mark.parametrize('boundary', ['driver', 'runner'])
def test_catalog_changed_during_preflight_is_rejected(tmp_path, monkeypatch, boundary):
    if boundary == 'driver':
        config, payload, events = driver_inputs(tmp_path, monkeypatch)
        invoke = lambda: milestone_driver.prepare_execution(
            {}, {'class': 'NoCompaction'}, config, {'task': {}, 'compact_limit': 230000}, tmp_path, 'owner')
    else:
        source, request, events, lock = runner_inputs(tmp_path, monkeypatch)
        invoke = lambda: runner.run(request, source)

    def change(bindir, path, model, reasoning=None):
        identity = codex_catalog.inspect(path, model, reasoning)
        path = Path(path)
        path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        return dict(identity, parser_verified=True, model_calls=0)

    monkeypatch.setattr(codex_catalog, 'preflight', change)
    with pytest.raises(ValueError, match='changed during preflight'):
        invoke()
    assert events == []


@pytest.mark.linux_only
def test_runner_rejects_preflight_digest_that_differs_from_request(tmp_path, monkeypatch):
    source, request, events, lock = runner_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(codex_catalog, 'preflight', lambda *args: {'sha256': 'a' * 64})
    with pytest.raises(ValueError, match='changed during preflight'):
        runner.run(request, source)
    assert events == []


@pytest.mark.linux_only
@pytest.mark.parametrize('mismatch', [None, 'before', 'after'])
def test_container_catalog_checks_survive_optimization_and_precede_auth_upload(tmp_path, monkeypatch, mismatch):
    source, request, events, lock = runner_inputs(tmp_path, monkeypatch)
    inspect = codex_catalog.inspect
    expected = inspect(request['execution']['model_catalog'], MODEL, 'low')

    def container_inspect(path, model, reasoning=None):
        if path == codex_catalog.CONTAINER_PATH:
            assert model == MODEL and reasoning == 'low'
            return dict(expected, path=path, sha256='0' * 64 if mismatch == 'before' else expected['sha256'])
        return inspect(path, model, reasoning)

    def preflight(bindir, path, model, reasoning=None):
        if path == codex_catalog.CONTAINER_PATH:
            assert bindir == '/cxbin'
            events.append('container-parser')
            return dict(container_inspect(path, model, reasoning),
                        sha256='0' * 64 if mismatch == 'after' else expected['sha256'])
        events.append('catalog-preflight')
        return dict(inspect(path, model, reasoning), parser_verified=True, model_calls=0)

    def docker(*args):
        assert args[:5] == ('exec', '--user', 'fakeroot', '-e', 'PYTHONPATH=/ctxpress-runtime')
        assert args[5:8] == ('synthetic-agent', 'python3', '-c')
        # optimize=2 models PYTHONOPTIMIZE=2: explicit raises must still execute.
        exec(compile(args[8], '<container catalog readiness>', 'exec', optimize=2), {})

    @contextlib.contextmanager
    def containers(*args):
        owner = SimpleNamespace(record={'container': 'synthetic-agent'})
        args[-1](owner, SimpleNamespace())
        yield

    monkeypatch.setattr(codex_catalog, 'inspect', container_inspect)
    monkeypatch.setattr(codex_catalog, 'preflight', preflight)
    monkeypatch.setattr(runner.milestone_resources, 'docker', docker)
    monkeypatch.setattr(runner.milestone_containers, 'installed', containers)
    monkeypatch.setattr(runner.milestone_transport, 'initialize', lambda *args: events.append('auth-upload'))
    if mismatch:
        with pytest.raises(ValueError, match='native container catalog'):
            runner.run(request, source)
        assert 'auth-upload' not in events
    else:
        state = runner.run(request, source)
        assert state['model_catalog'] == expected
        assert events.index('container-parser') < events.index('auth-upload')


@pytest.mark.linux_only
@pytest.mark.parametrize('private', [False, True])
def test_catalog_is_a_read_only_file_mount_and_root_override_for_new_and_resume(tmp_path, private):
    cfg = private_settings() if private else settings()
    cfg['model_catalog'] = str(catalog_file(tmp_path))
    cls = milestone_codex.framework(OfficialFixture, cfg)
    if private:
        cls.set_model_relay('http://127.0.0.1:32123')
    agent = cls()
    mounts = agent.get_container_mounts()
    assert mounts[-2:] == ['-v', cfg['model_catalog'] + ':' + codex_catalog.CONTAINER_PATH + ':ro']
    commands = [agent.build_run_command(MODEL, None, '/prompt'),
                agent.build_resume_command(MODEL, 'thread', '/resume')]
    for command in commands:
        args = shlex.split(command)
        passthrough = args[max(i for i, value in enumerate(args) if value == '--') + 1:]
        assert passthrough[:4] == ['-c', 'model_auto_compact_token_limit=230000',
                                  '-c', 'model_catalog_json=' + json.dumps(codex_catalog.CONTAINER_PATH)]
        assert passthrough[4] == 'exec'
        assert json.loads(passthrough[3].split('=', 1)[1]) == '/cxmetadata/models.json'
    assert 'resume' in shlex.split(commands[1])


@pytest.mark.parametrize('with_catalog', [False, True])
def test_parent_result_retains_optional_native_catalog_identity(tmp_path, monkeypatch, with_catalog):
    folder = tmp_path / 'attempt'
    metadata = codex_catalog.inspect(catalog_file(tmp_path), MODEL, 'low')
    monkeypatch.setattr(milestone_driver, 'prepare_execution', lambda *args:
                        ({'execution': {'binary_version': '0.159.0-alpha.12.1'}}, {'python': '/frozen/python'}, '/fake/auth.json'))
    monkeypatch.setattr(milestone_driver.subprocess, 'run', lambda *args, **kwargs: None)

    class Process:
        def __init__(self, *args, **kwargs):
            pass

        def wait(self):
            state = {'stop': 'completed', 'calls': 0, 'cleanup_complete': True}
            if with_catalog:
                state['model_catalog'] = metadata
            (folder / 'native-execution.json').write_text(json.dumps(state), encoding='utf-8')
            (folder / 'native-grade.json').write_text('{}', encoding='utf-8')
            return 0

    monkeypatch.setattr(milestone_driver.subprocess, 'Popen', Process)
    monkeypatch.setattr(milestone_driver, 'recover_attempt', lambda *args: None)
    monkeypatch.setattr(milestone_driver, 'summary', lambda *args: {'requests': 0})
    from ctxpress.harness import execution_health
    monkeypatch.setattr(execution_health, 'retain', lambda result: result)
    adapter = SimpleNamespace(task_start_description=lambda: {'name': 'swe-milestone'})
    result = milestone_driver.execute(adapter, {'id': 'repo'}, {'class': 'NoCompaction'},
                                      {'model': MODEL, 'reasoning': 'low'}, {},
                                      paths=None, folder=folder, label='owner')
    if with_catalog:
        assert result['model_catalog'] == metadata
    else:
        assert 'model_catalog' not in result
