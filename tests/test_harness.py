import json, shutil, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from ctxpress.harness import codex_docker as harness
from ctxpress.hosts.codex import launch
from ctxpress.methods import CodexAutoCompact, Composed, WithMemory


@pytest.mark.parametrize("mode", [None, "legacy", "paginated"])
def test_flat_history_import_changes_only_storage_metadata(tmp_path, mode):
    import copy
    rows = [dict(type="session_meta", ordinal=0, payload=dict(id="fixture-session", history_mode=mode)),
            dict(type="compacted", payload=dict(message="retained summary")),
            dict(type="response_item", payload=dict(type="function_call_output", output="retained source"))]
    original = copy.deepcopy(rows)
    destination = tmp_path / "rollout.jsonl"
    evidence = harness._stage_rollout(destination, rows)
    restored = [json.loads(line) for line in destination.read_text(encoding='utf-8').splitlines()]
    assert rows == original and restored[1:] == original[1:]
    assert restored[0] == dict(original[0], payload=dict(original[0]["payload"], history_mode="legacy"))
    assert evidence == dict(source_history_mode=mode, staged_history_mode="legacy", rows=3, metadata_only=True)


def test_flat_history_import_rejects_unknown_storage_before_writing(tmp_path):
    destination = tmp_path / "rollout.jsonl"
    with pytest.raises(ValueError, match="unsupported"):
        harness._stage_rollout(destination, [dict(type="session_meta", payload=dict(history_mode="unknown"))])
    assert not destination.exists()


def test_native_compact_threshold_reaches_wrapped_codex_profile(tmp_path, monkeypatch):
    import tomllib
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CTXPRESS_HOME", str(tmp_path / "ctxpress"))
    got = []
    def fake_process(command, env):
        name = command[command.index("--profile") + 1]
        got.append(tomllib.loads((tmp_path / "codex" / (name + ".config.toml")).read_text(encoding='utf-8')))
        return 0
    monkeypatch.setattr(launch.subprocess, "call", fake_process)
    launch.run({"class": "WithMemory", "args": {"inner": {"class": "CodexAutoCompact", "args": {"t": 128000}}}}, codex_bin="unused")
    assert got[0]["model_auto_compact_token_limit"] == 128000
    with pytest.raises(ValueError, match="conflicting"):
        Composed([WithMemory(CodexAutoCompact(128000)), CodexAutoCompact(230000)])


def test_harness_help_does_not_need_benchmark_files(tmp_path):
    import os
    process = subprocess.run([sys.executable, "-m", "ctxpress.harness.codex_docker", "--help"], capture_output=True, text=True,
                             env=dict(os.environ, CTXPRESS_DATA=str(tmp_path / "missing")))
    assert process.returncode == 0 and "--bindir" in process.stdout


def test_installed_logs_include_per_run_summaries_and_skip_partial_rows(tmp_path):
    path = tmp_path / "ctxpress/runs/a/requests.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"request":1}\n{"type":"summary"}\n{"unfinished":', encoding="utf-8")
    output = tmp_path / "out.jsonl"
    harness._installed_log(tmp_path, output)
    assert [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()] == [dict(request=1), dict(type="summary")]


def test_failed_container_cleanup_preserves_credential_directory(tmp_path, monkeypatch):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir(); workspace.mkdir()
    (home / "auth.json").write_text("offline test fixture", encoding="utf-8")
    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="preserved"):
        harness._cleanup(SimpleNamespace(image="fixture-image"), home, workspace)
    assert (home / "auth.json").exists() and workspace.exists()


@pytest.mark.parametrize("source_path", ["unmapped", "absolute", "relative"])
def test_docker_start_failure_still_cleans_temporary_home_inside_container(tmp_path, monkeypatch, source_path):
    fixture_auth = tmp_path / "dummy-auth"
    fixture_auth.write_text("offline test fixture", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "codex").write_text("offline executable fixture", encoding="utf-8")
    live_scripts, frozen_scripts = tmp_path/'original-scripts', tmp_path/'frozen-scripts'
    live_scripts.mkdir(); frozen_scripts.mkdir()
    live_source = live_scripts/'fixture-rollout.jsonl'
    frozen_source = frozen_scripts/'frozen-rollout.jsonl'
    seen = []
    def build(source, *args):
        seen.append(source)
        return [dict(type='session_meta',payload=dict(id='fixture-session'))], {}
    metadata_source = str(live_source) if source_path == 'absolute' else 'fixture-rollout.jsonl'
    mapping = None if source_path == 'unmapped' else {
        str(live_scripts/'valid_points.json'):str(frozen_scripts/'valid_points.json'), str(live_source):str(frozen_source)}
    env = SimpleNamespace(auth=fixture_auth, image="fixture-image", bindir=str(binary), workspace=workspace, scripts=frozen_scripts,
        points={(3, 14): dict(src=metadata_source, i=0)}, agent_env=[], off_features=[], last_queue=lambda *a: None,
        host_proxy=lambda: "", build_kept=build)
    monkeypatch.setattr(harness, "environment", lambda *a: env)
    cleanup_calls = []
    def fake_docker(command, **kwargs):
        if command[1:3] == ['image','inspect']:
            digest = 'sha256:' + ('1' if command[-1] == 'fixture-image' else '2') * 64
            return SimpleNamespace(returncode=0, stdout=json.dumps([dict(Id=digest,Os='linux',Architecture='amd64')]), stderr='')
        if command[1:3] == ["network", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="172.17.0.1", stderr="")
        if "-d" in command:
            home_mount = command[command.index('-v') + 1]
            home = Path(home_mount.removesuffix(':/cxhome'))
            assert (home/'sessions/2026/09/22/fixture-rollout.jsonl').is_file()
            assert not (home/'sessions/2026/09/22/frozen-rollout.jsonl').exists()
            raise subprocess.CalledProcessError(125, command)
        if "--network" in command and "none" in command and "/cxbin/codex" not in command:
            mounts = [command[i + 1] for i, value in enumerate(command) if value == "-v"]
            for mount in mounts:
                source = Path(mount.rsplit(":", 1)[0])
                cleanup_calls.append(source)
                for child in source.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(harness.subprocess, "run", fake_docker)
    with pytest.raises(subprocess.CalledProcessError):
        harness.run(3, 14, {"class": "NoCompaction"}, installed=True, outdir=tmp_path / "run", input_paths=mapping)
    assert seen == [metadata_source if mapping is None else str(frozen_source)]
    assert cleanup_calls and all(not path.exists() for path in cleanup_calls)
    resources = json.loads(next((tmp_path / "run").glob("resources-*.json")).read_text(encoding='utf-8'))
    assert resources["cleaned"]


def test_recovery_refuses_foreign_container_before_cleanup(tmp_path, monkeypatch):
    import tempfile
    home = Path(tempfile.mkdtemp(prefix='cxh-'))
    workspace = Path(tempfile.mkdtemp(prefix='ctxws-'))
    manifest = tmp_path / 'resources-ctxp-0123456789.json'
    manifest.write_text(json.dumps(dict(container='ctxp-0123456789', run_label='mine', image='fixture-image',
        home=str(home), workspace=str(workspace), store=None, cleaned=False)), encoding='utf-8')
    calls = []
    def docker(command, **kwargs):
        calls.append(command)
        if command[1:3] == ['ps', '-aq']:
            return SimpleNamespace(returncode=0, stdout='fixture-container', stderr='')
        if command[1] == 'inspect':
            return SimpleNamespace(returncode=0, stdout=json.dumps({'ctxpress.managed':'true', 'ctxpress.run':'foreign'}), stderr='')
        pytest.fail('attempted to change a foreign container')
    monkeypatch.setattr(harness.subprocess, 'run', docker)
    try:
        with pytest.raises(ValueError, match='another experiment'):
            harness.recover(manifest, 'mine')
        assert home.exists() and workspace.exists() and len(calls) == 2
    finally:
        shutil.rmtree(home); shutil.rmtree(workspace)


@pytest.mark.parametrize('change', ['contents', 'extra_file', 'missing_directory'])
def test_locked_run_rechecks_staged_workspace_before_copying_auth_or_starting_agent(tmp_path, monkeypatch, change):
    workspace, binary = tmp_path / 'workspace', tmp_path / 'bin'
    workspace.mkdir(); binary.mkdir()
    (workspace / 'task.md').write_text('frozen requirements', encoding='utf-8')
    (workspace / 'empty').mkdir()
    (binary / 'codex').write_text('offline executable fixture', encoding='utf-8')
    # No fixture credential is needed: a rejected staging copy must never open it.
    env = SimpleNamespace(auth=tmp_path / 'must-not-be-opened-auth', image='fixture-image', bindir=str(binary),
        workspace=workspace, points={(3, 14): dict(src='fixture-rollout.jsonl', i=0)}, agent_env=[], off_features=[],
        last_queue=lambda *a: None, host_proxy=lambda: '',
        build_kept=lambda *a: ([dict(type='session_meta',payload=dict(id='fixture-session'))], {}))
    monkeypatch.setattr(harness, 'environment', lambda *a: env)
    commands, cleaned = [], []
    def docker(command, **kwargs):
        commands.append(command)
        if command[1:3] == ['image', 'inspect']:
            reference = command[-1]
            digest = reference if reference.startswith('sha256:') else 'sha256:' + ('1' if reference == 'fixture-image' else '2') * 64
            return SimpleNamespace(returncode=0, stdout=json.dumps([dict(Id=digest, Os='linux', Architecture='amd64')]), stderr='')
        if command[1:3] == ['network', 'inspect']:
            return SimpleNamespace(returncode=0, stdout='172.17.0.1', stderr='')
        assert '-d' not in command, 'started an agent with a changed auxiliary workspace'
        if '--network' in command and 'none' in command and '/cxbin/codex' not in command:
            for i, value in enumerate(command):
                if value == '-v':
                    source = Path(command[i+1].rsplit(':', 1)[0])
                    assert not (source / 'auth.json').exists()
                    cleaned.append(source)
                    for child in source.iterdir():
                        shutil.rmtree(child) if child.is_dir() else child.unlink()
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(harness.subprocess, 'run', docker)
    lock = harness.eval_environment.capture(workspace, 'fixture-image', [(3, 14)])
    manifest = tmp_path / 'environment.json'
    harness.atomic_json(manifest, lock)
    original_copytree = shutil.copytree
    def changed_copy(source, destination, *args, **kwargs):
        result = original_copytree(source, destination, *args, **kwargs)
        if Path(destination).name == 'e2e_workspace':
            if change == 'contents':
                (Path(destination) / 'task.md').write_text('changed during copy', encoding='utf-8')
            elif change == 'extra_file':
                (Path(destination) / 'unexpected.txt').write_text('undeclared', encoding='utf-8')
            else:
                (Path(destination) / 'empty').rmdir()
        return result
    monkeypatch.setattr(harness.shutil, 'copytree', changed_copy)
    monkeypatch.setattr(harness.shutil, 'copy', lambda *a, **k: pytest.fail('copied credentials before checking the staged workspace'))
    with pytest.raises(ValueError, match='auxiliary workspace differs'):
        harness.run(3, 14, {'class': 'NoCompaction'}, installed=True, outdir=tmp_path / 'run', snapshot=manifest)
    assert cleaned and all(not path.exists() for path in cleaned)
    assert not any('-d' in command for command in commands)
    resource = json.loads(next((tmp_path / 'run').glob('resources-*.json')).read_text(encoding='utf-8'))
    assert resource['cleaned']


@pytest.mark.parametrize('remove_fails', [False, True])
@pytest.mark.parametrize('locked', [False, True])
def test_installed_run_collects_summary_usage_and_requires_container_removal(tmp_path, monkeypatch, remove_fails, locked):
    fixture_auth = tmp_path / 'dummy-auth'
    fixture_auth.write_text('offline test fixture', encoding='utf-8')
    workspace, binary = tmp_path / 'workspace', tmp_path / 'bin'
    workspace.mkdir(); binary.mkdir()
    (workspace/'task.md').write_text('frozen requirements',encoding='utf-8')
    (binary / 'codex').write_text('offline executable fixture', encoding='utf-8')
    env = SimpleNamespace(auth=fixture_auth, image='fixture-image', bindir=str(binary), workspace=workspace,
        points={(3, 14): dict(src='fixture-rollout.jsonl', i=0)}, agent_env=[], off_features=[], last_queue=lambda *a: None,
        host_proxy=lambda: '', build_kept=lambda *a: ([dict(type='session_meta',payload=dict(id='fixture-session'))], {}))
    monkeypatch.setattr(harness, 'environment', lambda *a: env)
    temporary = []
    def docker(command, **kwargs):
        if command[1:3] == ['image','inspect']:
            digest = command[-1] if command[-1].startswith('sha256:') else 'sha256:' + ('1' if command[-1] == 'fixture-image' else '2') * 64
            return SimpleNamespace(returncode=0, stdout=json.dumps([dict(Id=digest,Os='linux',Architecture='amd64')]), stderr='')
        if command[1:3] == ['network', 'inspect']:
            return SimpleNamespace(returncode=0, stdout='172.17.0.1', stderr='')
        if '-d' in command:
            assert command[command.index('/testbed') + 1] == 'sha256:' + '2' * 64
            mounts = [command[i+1] for i,value in enumerate(command) if value == '-v']
            copied_workspace = Path(next(mount.removesuffix(':/e2e_workspace') for mount in mounts if mount.endswith(':/e2e_workspace')))
            assert (copied_workspace/'task.md').read_text(encoding='utf-8') == 'frozen requirements'
            home = Path(command[command.index('-v') + 1].removesuffix(':/cxhome'))
            temporary.append(home)
            log = home / 'ctxpress/runs/fixture/requests.jsonl'
            log.parent.mkdir(parents=True)
            rows = [dict(request=1, tokens_before=100, tokens_after=80, changed=1, usage=dict(input_tokens=100, cached_tokens=10, output_tokens=2)),
                    dict(type='summary', usage=dict(input_tokens=5, cached_tokens=0, output_tokens=1))]
            log.write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
            rollout = next(home.rglob('fixture-rollout.jsonl'))
            with rollout.open('a', encoding='utf-8') as f:
                for kind in ('function_call', 'function_call_output'):
                    f.write(json.dumps(dict(type='response_item', payload=dict(type=kind, call_id='fixture-call'))) + '\n')
        if command[1:3] == ['ps', '-aq']:
            return SimpleNamespace(returncode=0, stdout='fixture-container', stderr='')
        if command[1:3] == ['rm', '-f'] and remove_fails:
            raise subprocess.CalledProcessError(1, command)
        if '--network' in command and 'none' in command and '/cxbin/codex' not in command:
            assert not remove_fails, 'cleaned while container removal was unconfirmed'
            for i, value in enumerate(command):
                if value == '-v':
                    source = Path(command[i+1].rsplit(':', 1)[0])
                    for child in source.iterdir():
                        shutil.rmtree(child) if child.is_dir() else child.unlink()
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(harness.subprocess, 'run', docker)
    options = {}
    if locked:
        lock = harness.eval_environment.capture(workspace,'fixture-image',[(3,14)])
        path = tmp_path/'environment.json'
        harness.atomic_json(path,lock)
        frozen_workspace = tmp_path/'frozen-workspace'
        shutil.copytree(workspace,frozen_workspace)
        shutil.rmtree(workspace)
        options = dict(snapshot=str(path),workspace=str(frozen_workspace))
    try:
        if remove_fails:
            with pytest.raises(subprocess.CalledProcessError):
                harness.run(3, 14, {'class':'NoCompaction'}, max_calls=1, installed=True, outdir=tmp_path / 'run', **options)
            assert (temporary[0] / 'auth.json').exists()
        else:
            result = harness.run(3, 14, {'class':'NoCompaction'}, max_calls=1, installed=True, outdir=tmp_path / 'run', **options)
            assert result['stop'] == 'max_calls' and result['calls'] == result['requests'] == 1
            assert len(result['rewrites']) == 2 and result['usage']['summary_calls'] == 1
            assert result['usage']['api_input_tokens'] == 105 and result['usage']['api_output_tokens'] == 3
            assert result['environment']['base_image_id'] == 'sha256:' + '1' * 64
            assert result['environment']['workspace_frozen'] is locked
            assert not temporary[0].exists()
        manifest = json.loads(next((tmp_path / 'run').glob('resources-*.json')).read_text(encoding='utf-8'))
        assert manifest['cleaned'] is (not remove_fails)
    finally:
        # These are exclusively offline fixture files, never user credentials.
        for manifest_path in (tmp_path / 'run').glob('resources-*.json'):
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            for key in ('home', 'workspace', 'store'):
                if manifest.get(key):
                    shutil.rmtree(manifest[key], ignore_errors=True)
