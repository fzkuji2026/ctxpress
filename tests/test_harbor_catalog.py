"""Pinned catalog task-start wiring; fake CLIs and containers, no model calls."""
import asyncio
import copy
import json
import shlex
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctxpress.benchmarks.harbor import codex_hook as harbor_codex, driver as harbor_driver
from ctxpress.benchmarks.swe import driver as swe_driver
from ctxpress.core import toml
from ctxpress.harness.runtime import codex_catalog
from ctxpress.harness.jobs import inputs as eval_inputs, plan as eval_plan, queue as evaluation, task
from ctxpress.benchmarks.bigcode import trial as code_trial
from ctxpress.benchmarks.harbor import modern as harbor_modern, worker as harbor_worker
from ctxpress.benchmarks.deepswe import pier_trial
from ctxpress.benchmarks.swe import containers as swe_containers, trial as swe_trial
from test_harbor_codex import EnvironmentFixture, OfficialFixture, settings
from test_harbor_driver import prepared as harbor_prepared
from test_swe_runner import Client, request as swe_request, prepared as swe_prepared

PROMPT = 'synthetic catalog instructions that must never appear in run metadata'


def catalog_file(tmp_path, model='fixture-model'):
    path = tmp_path / 'models.json'
    path.write_text(json.dumps({'models': [{'slug': model, 'base_instructions': PROMPT,
        'supported_reasoning_levels': [{'effort': effort, 'description': 'fixture'}
                                       for effort in ('low', 'medium', 'high')]}]}), encoding='utf-8')
    return path


def fake_binary(bindir):
    """Accept only the offline parser and version commands; no exec/API path."""
    binary = Path(bindir) / 'codex'
    binary.write_text('#!' + sys.executable + '\n' + '''import json, pathlib, sys
if sys.argv[1:] == ['--version']:
    print('codex-cli fixture-version')
elif sys.argv[-2:] == ['debug', 'models']:
    values = dict(value.split('=', 1) for value in sys.argv[1:] if '=' in value)
    path = json.loads(values['model_catalog_json'])
    print(pathlib.Path(path).read_text())
else:
    raise SystemExit('fake CLI rejects all model commands')
''', encoding='utf-8')
    binary.chmod(0o755)


def catalog_request(tmp_path, model='fixture-model'):
    path = catalog_file(tmp_path, model)
    return dict(model=model, reasoning='medium', bindir=str(tmp_path / 'bin'),
                model_catalog=str(path), model_catalog_sha256=eval_plan.file_sha256(path))


@pytest.mark.linux_only
@pytest.mark.parametrize('family', ['harbor', 'swe'])
def test_frozen_catalog_reaches_direct_driver_after_original_inputs_are_removed(tmp_path, monkeypatch, family):
    initial = harbor_prepared(tmp_path) if family == 'harbor' else swe_prepared(tmp_path)
    cfg = initial[0]
    source = catalog_file(tmp_path)
    fake_binary(cfg['environment']['bindir'])
    cfg['environment']['model_catalog'] = str(source)
    plan = eval_plan.compile_plan(cfg)
    directory = evaluation.prepare(plan, tmp_path / 'catalog-run')
    effective, paths = eval_inputs.execution(plan, directory / 'inputs')
    copied = paths[str(source)]
    source.unlink()
    for name in ('data', 'harbor', 'swebench', 'dependencies', 'bin'):
        if (tmp_path / name).exists():
            shutil.rmtree(tmp_path / name)
    credentials = tmp_path / 'runtime-fixture.json'
    credentials.write_text('synthetic runtime fixture', encoding='utf-8')
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE', str(credentials))
    monkeypatch.setattr(harbor_driver, 'docker', lambda *args: pytest.fail('prepare contacted Docker'))
    seen = []
    preflight = codex_catalog.preflight
    def tracked(bindir, path, model, reasoning=None):
        seen.append((bindir, path, model, reasoning))
        return preflight(bindir, path, model, reasoning)
    monkeypatch.setattr(codex_catalog, 'preflight', tracked)
    job = plan['jobs'][0]
    frozen = task.remap(job['task'], paths)
    folder = directory / 'attempt'
    folder.mkdir()
    driver = harbor_driver if family == 'harbor' else swe_driver
    request, auth = driver.prepare(frozen, job['method'], effective, job, folder, 'plan-job')
    assert request['model_catalog'] == copied
    assert request['model_catalog_sha256'] == plan['artifacts'][str(source)]
    assert seen == [(effective['environment']['bindir'], copied, cfg['model'], effective['reasoning'])]
    assert auth == str(credentials)
    assert PROMPT not in json.dumps(request)
    assert harbor_codex.check_catalog(request) == codex_catalog.inspect(copied, request['model'], request['reasoning'])


@pytest.mark.parametrize('driver', [harbor_driver, swe_driver])
@pytest.mark.parametrize('failure', ['missing-model', 'reasoning', 'parser'])
def test_catalog_rejected_before_resource_reads_or_binary_startup(tmp_path, monkeypatch, driver, failure):
    path = catalog_file(tmp_path, 'another-model' if failure == 'missing-model' else 'fixture-model')
    cfg = dict(environment={'model_catalog': str(path), 'bindir': str(tmp_path / 'bin')},
               model='fixture-model', reasoning='unsupported' if failure == 'reasoning' else 'medium')
    def parser(*args):
        if failure == 'parser':
            raise ValueError('fixture parser rejection')
        pytest.fail('invalid selection reached CLI')
    monkeypatch.setattr(codex_catalog, 'preflight', parser)
    monkeypatch.setattr(driver.task_resources, 'read', lambda *args: pytest.fail('read resources before catalog validation'))
    with pytest.raises(ValueError):
        driver.prepare({}, {}, cfg, {}, tmp_path, 'plan-job')


@pytest.mark.parametrize('change', ['path-only', 'hash-only', 'relative', 'empty', 'bad-hash', 'changed'])
def test_worker_catalog_declaration_is_strict_and_immutable(tmp_path, monkeypatch, change):
    request = catalog_request(tmp_path)
    if change == 'path-only': request.pop('model_catalog_sha256')
    elif change == 'hash-only': request.pop('model_catalog')
    elif change == 'relative': request['model_catalog'] = 'models.json'
    elif change == 'empty': request['model_catalog'] = None
    elif change == 'bad-hash': request['model_catalog_sha256'] = 'not-a-digest'
    else: Path(request['model_catalog']).write_text(Path(request['model_catalog']).read_text(encoding='utf-8') + '\n', encoding='utf-8')
    monkeypatch.setattr(codex_catalog, 'preflight', lambda *args: pytest.fail('bad binding reached parser'))
    with pytest.raises(ValueError): harbor_codex.check_catalog(request, probe=True)


def test_change_during_parser_preflight_fails_closed(tmp_path, monkeypatch):
    request = catalog_request(tmp_path)
    def mutate(*args):
        path = Path(request['model_catalog'])
        path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    monkeypatch.setattr(codex_catalog, 'preflight', mutate)
    with pytest.raises(ValueError, match='changed during preflight'):
        harbor_codex.check_catalog(request, probe=True)


@pytest.mark.parametrize('runner', [harbor_worker, harbor_modern, pier_trial])
def test_harbor_runner_mounts_exact_file_read_only_without_creation(tmp_path, runner):
    request = dict(catalog_request(tmp_path), package=str(tmp_path / 'package'), profiles=str(tmp_path / 'profiles'),
        task=str(tmp_path / 'task'), folder=str(tmp_path), project='ctxp-hb-' + 'a' * 24,
        run={'timeout':10, 'grade':True})
    config = runner.trial_config(request, lambda **kwargs: kwargs, tmp_path / 'channel')
    mounts = config['environment'].get('mounts_json', config['environment'].get('mounts'))
    actual = [mount for mount in mounts if mount['target'] == codex_catalog.CONTAINER_PATH]
    assert actual == [dict(type='bind', source=request['model_catalog'], target=codex_catalog.CONTAINER_PATH,
                           read_only=True, bind={'create_host_path':False})]
    if runner is not pier_trial:
        replay = dict(request, pro_replay=True)
        config = runner.trial_config(replay, lambda **kwargs: kwargs, tmp_path / 'channel')
        assert not config['environment'].get('mounts_json', config['environment'].get('mounts'))
        assert harbor_codex.catalog_settings(replay) == {}


@pytest.mark.parametrize('environment_class', [swe_containers.AgentEnvironment, code_trial.CodeEnvironment])
def test_swe_and_bigcode_agents_mount_catalog_but_separate_verifier_does_not(tmp_path, monkeypatch, environment_class):
    request = dict(swe_request(tmp_path), **catalog_request(tmp_path))
    request['task']['initial_state']['seed_code'] = 'def fixture(): pass\n'
    client = Client()
    original_create = client.create
    def create(**options):
        container = original_create(**options)
        container.put_archive = lambda *args: True
        return container
    client.containers.create = create
    agent = swe_containers.Owner(request, client, 'agent', 'daemon', tmp_path / 'channel')
    asyncio.run(environment_class(agent, tmp_path / 'channel').start())
    volumes = client.events[0][1]['volumes']
    assert volumes[request['model_catalog']] == {'bind':codex_catalog.CONTAINER_PATH, 'mode':'ro'}
    assert agent.record['model_catalog'] == codex_catalog.inspect(request['model_catalog'], request['model'], request['reasoning'])
    assert PROMPT not in json.dumps(agent.record)
    agent.cleanup()
    verifier = swe_containers.Owner(request, client, 'verifier', 'daemon')
    verifier.create()
    assert not any(value['bind'] == codex_catalog.CONTAINER_PATH
                   for value in verifier.container.options.get('volumes', {}).values())
    assert 'model_catalog' not in verifier.record
    verifier.cleanup()


def test_catalog_agent_checks_parser_before_credentials_and_relay_and_writes_root_key():
    cfg = dict(settings(), model_catalog=codex_catalog.CONTAINER_PATH, model_catalog_sha256='a' * 64)
    agent = harbor_codex.framework(OfficialFixture, SimpleNamespace, cfg)()
    environment = EnvironmentFixture()
    asyncio.run(agent.setup(environment))
    first = shlex.split(environment.commands[0][0])[-1]
    assert 'catalog_preflight' in first and codex_catalog.CONTAINER_PATH in first
    assert cfg['model'] in first and cfg['reasoning'] in first
    assert cfg['model_catalog_sha256'] in first
    writes = [shlex.split(command)[-1] for command, _ in environment.commands if '.write_text(' in command]
    assert len(writes) == 1
    # Evaluate only the recorded synthetic config writer into an in-memory sink.
    namespace = {}
    class Sink:
        def __init__(self, path): assert path == harbor_codex.HOME + '/config.toml'
        def write_text(self, data): namespace['data'] = data
    source = writes[0].replace('from pathlib import Path; ', '')
    exec(source, {'Path':Sink})
    parsed = toml.loads(namespace['data'] + '\n[features]\nunified_exec = true\n')
    assert parsed['model_catalog_json'] == codex_catalog.CONTAINER_PATH
    assert parsed['features'] == {'unified_exec':True}
    assert PROMPT not in namespace['data']


def test_container_catalog_failure_never_uploads_credentials_or_starts_model_relay():
    cfg = dict(settings(), model_catalog=codex_catalog.CONTAINER_PATH, model_catalog_sha256='a' * 64)
    class Rejected(EnvironmentFixture):
        async def exec(self, command, **kwargs):
            self.commands.append((command, kwargs))
            assert 'catalog_preflight' in command
            return SimpleNamespace(return_code=1, stdout=PROMPT)
    environment = Rejected()
    agent = harbor_codex.framework(OfficialFixture, SimpleNamespace, cfg)()
    with pytest.raises(RuntimeError) as error: asyncio.run(agent.setup(environment))
    assert not environment.uploads and len(environment.commands) == 1
    assert PROMPT not in str(error.value)


@pytest.mark.parametrize('changed', [False, True])
def test_container_probe_executes_frozen_hash_check_before_any_auth(tmp_path, monkeypatch, changed):
    from ctxpress.harness.runtime import codex_binary
    cfg = dict(settings(), model_catalog=codex_catalog.CONTAINER_PATH, model_catalog_sha256='a' * 64)
    monkeypatch.setattr(codex_binary, 'preflight', lambda bindir: {})
    seen = []
    def preflight(bindir, path, model, reasoning):
        seen.append((bindir, path, model, reasoning))
        return {'sha256':('b' if changed else 'a') * 64}
    monkeypatch.setattr(codex_catalog, 'preflight', preflight)
    class Environment(EnvironmentFixture):
        async def exec(self, command, **kwargs):
            if 'catalog_preflight' in command:
                self.commands.append((command, kwargs))
                try:
                    exec(shlex.split(command)[-1], {})
                except ValueError:
                    return SimpleNamespace(return_code=1, stdout='')
                return SimpleNamespace(return_code=0, stdout='')
            return await super().exec(command, **kwargs)
    environment = Environment()
    agent = harbor_codex.framework(OfficialFixture, SimpleNamespace, cfg)()
    if changed:
        with pytest.raises(RuntimeError): asyncio.run(agent.setup(environment))
        assert not environment.uploads and len(environment.commands) == 1
    else:
        asyncio.run(agent.setup(environment))
        assert len(environment.uploads) == 1
    assert seen == [('/cxbin', codex_catalog.CONTAINER_PATH, cfg['model'], cfg['reasoning'])]


@pytest.mark.parametrize('runner', ['legacy', 'modern', 'pier'])
def test_catalog_survives_complete_harbor_lifecycle_and_separate_verifier(tmp_path, monkeypatch, runner):
    """Run existing official lifecycle fixtures with the optional frozen input."""
    catalog = catalog_request(tmp_path)
    if runner == 'legacy':
        import test_harbor_driver as fixture
        target = fixture
    elif runner == 'modern':
        import test_harbor_modern as fixture
        target = harbor_modern
    else:
        import test_deep_swe as fixture
        target = pier_trial
    original_run = target.run_trial
    async def run(request, *args, **kwargs):
        return await original_run(dict(request, **catalog), *args, **kwargs)
    monkeypatch.setattr(target, 'run_trial', run)
    original_framework = harbor_codex.framework
    received = []
    def framework(base, exec_input, cfg, *args, **kwargs):
        received.append(copy.deepcopy(cfg))
        return original_framework(base, exec_input, cfg, *args, **kwargs)
    monkeypatch.setattr(harbor_codex, 'framework', framework)
    if runner == 'legacy':
        fixture.test_worker_invokes_trial_with_owned_hooks_and_verifier_after_agent(tmp_path, monkeypatch, False)
    elif runner == 'modern':
        fixture.test_modern_trial_create_preserves_author_collection_and_fresh_grader_lifecycle(
            tmp_path, monkeypatch, True, False, False, False, False)
    else:
        fixture.test_pier_worker_uses_fresh_grader_after_collect_and_checked_agent_stop(tmp_path, monkeypatch, False)
    assert len(received) == 1
    assert received[0]['model_catalog'] == codex_catalog.CONTAINER_PATH
    assert received[0]['model_catalog_sha256'] == catalog['model_catalog_sha256']
    records = [json.loads(path.read_text(encoding='utf-8')) for path in tmp_path.glob('resources-harbor-*.json')]
    for record in records:
        if record.get('role') == 'verifier':
            assert 'model_catalog' not in record
        else:
            assert record['model_catalog'] == codex_catalog.inspect(catalog['model_catalog'], catalog['model'], catalog['reasoning'])
        assert PROMPT not in json.dumps(record)


@pytest.mark.parametrize('path', [None, '', '/host/models.json'])
def test_agent_catalog_settings_accept_only_fixed_container_path(path):
    with pytest.raises(ValueError, match='container catalog path'):
        harbor_codex.framework(OfficialFixture, SimpleNamespace, dict(settings(), model_catalog=path))


@pytest.mark.parametrize('digest', [None, '', 'bad-hash'])
def test_agent_requires_catalog_hash_with_container_path(digest):
    with pytest.raises(ValueError, match='frozen SHA-256'):
        harbor_codex.framework(OfficialFixture, SimpleNamespace,
            dict(settings(), model_catalog=codex_catalog.CONTAINER_PATH, model_catalog_sha256=digest))


def test_optional_catalog_keeps_old_requests_and_agent_settings_compatible(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_catalog, 'inspect', lambda *args: pytest.fail('old request inspected a catalog'))
    monkeypatch.setattr(codex_catalog, 'preflight', lambda *args: pytest.fail('old request invoked catalog parser'))
    assert harbor_codex.catalog_input({}, 'fixture-model', 'medium') == {}
    assert harbor_codex.check_catalog({}) is None
    assert harbor_codex.catalog_mounts({}) == [] and harbor_codex.catalog_settings({}) == {}
    instance = harbor_codex.framework(OfficialFixture, SimpleNamespace, settings())()
    environment = EnvironmentFixture()
    asyncio.run(instance.setup(environment))
    assert not any('catalog_preflight' in command or 'model_catalog_json' in command for command, _ in environment.commands)


def test_shared_swe_agent_forwards_catalog_to_strict_hook(tmp_path, monkeypatch):
    request = dict(catalog_request(tmp_path), method={'class':'NoCompaction'}, folder=str(tmp_path),
        binary_version='fixture-version', compact_limit=1000, upstream='https://provider.invalid/v1',
        run={'max_calls':3})
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE', '/runtime-only/fixture.json')
    received = []
    def framework(base, exec_input, cfg, **kwargs):
        received.append(copy.deepcopy(cfg))
        return base
    monkeypatch.setattr(harbor_codex, 'framework', framework)
    swe_trial.agent_class(request, lambda *args: None)
    assert received[0]['model_catalog'] == codex_catalog.CONTAINER_PATH
    assert received[0]['model_catalog_sha256'] == request['model_catalog_sha256']
    assert request['model_catalog'] not in json.dumps(received)
