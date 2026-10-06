"""Catalog selection, parser validation and immutable task input propagation."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctxpress.harness.runtime import codex_catalog as catalog
from ctxpress.harness.results import host_compare as eval_host_compare
from ctxpress.harness.jobs import inputs as eval_inputs, protocol as eval_protocol
from test_eval_families import NAMES
from test_eval_protocol import inputs


def document(model='gpt-6.1-sol'):
    return {'models': [{'slug': model, 'base_instructions': 'synthetic instructions only',
                        'supported_reasoning_levels': [{'effort': 'medium', 'description': 'fixture'}]}]}


def write(tmp_path, value=None):
    path = tmp_path / 'models.json'
    path.write_text(json.dumps(document() if value is None else value), encoding='utf-8')
    return path


@pytest.mark.parametrize('change', ['missing-model', 'duplicate', 'empty', 'bare-array', 'no-instructions', 'reasoning'])
def test_bad_selection_fails_without_running_binary(tmp_path, monkeypatch, change):
    value = document()
    if change == 'missing-model': value['models'][0]['slug'] = 'another-model'
    elif change == 'duplicate': value['models'] *= 2
    elif change == 'empty': value['models'] = []
    elif change == 'bare-array': value = value['models']
    elif change == 'no-instructions': value['models'][0].pop('base_instructions')
    elif change == 'reasoning': value['models'][0]['supported_reasoning_levels'] = []
    path = write(tmp_path, value)
    monkeypatch.setattr(catalog.subprocess, 'run', lambda *a, **k: pytest.fail('invalid catalog reached CLI'))
    with pytest.raises(ValueError): catalog.preflight(tmp_path, path, 'gpt-6.1-sol', 'medium')


@pytest.mark.parametrize('alias', [False, True])
def test_credential_path_rejected_before_reading(tmp_path, monkeypatch, alias):
    path = tmp_path / 'auth.json'; path.write_text('synthetic forbidden fixture', encoding='utf-8')
    if alias:
        target = path; path = tmp_path / 'catalog.json'
        path.symlink_to(target)
    monkeypatch.setattr(Path, 'read_bytes', lambda *a: pytest.fail('credential file read'))
    with pytest.raises(ValueError, match='non-credential'): catalog.inspect(path, 'model')


def test_cli_preflight_preserves_catalog_and_never_inherits_host_environment(tmp_path, monkeypatch):
    path = write(tmp_path)
    seen = []
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-do-not-inherit')
    monkeypatch.setenv('CODEX_HOME', '/synthetic-user-home')
    def run(command, **kwargs):
        seen.append(command)
        assert '--bundled' not in command and command[-2:] == ['debug', 'models']
        assert kwargs['env']['OPENAI_API_KEY'] != 'synthetic-do-not-inherit'
        assert kwargs['env']['CODEX_HOME'] != '/synthetic-user-home'
        assert not (Path(kwargs['cwd']) / 'auth.json').exists()
        assert kwargs['env']['HTTPS_PROXY'] == 'http://127.0.0.1:9'
        normalized = document(); normalized['models'][0]['derived_field'] = 'allowed'
        return SimpleNamespace(returncode=0, stdout=json.dumps(normalized).encode(), stderr=b'')
    monkeypatch.setattr(catalog.subprocess, 'run', run)
    result = catalog.preflight(tmp_path, path, 'gpt-6.1-sol', 'medium')
    assert result['parser_verified'] and result['model_calls'] == 0 and len(seen) == 1
    assert 'synthetic instructions' not in str(result)


@pytest.mark.parametrize('mode', ['parser-error', 'malformed', 'fallback', 'modified', 'duplicate', 'race'])
def test_cli_failure_or_reinterpretation_blocks_launch_without_disclosing_prompt(tmp_path, monkeypatch, mode):
    path = write(tmp_path)
    value = document()
    if mode == 'fallback': value = {'models': []}
    if mode == 'modified': value['models'][0]['base_instructions'] = 'different'
    if mode == 'duplicate': value['models'] *= 2
    def run(*args, **kwargs):
        if mode == 'race': path.write_text(json.dumps(document('other')), encoding='utf-8')
        return SimpleNamespace(returncode=1 if mode == 'parser-error' else 0,
            stdout=b'not JSON' if mode == 'malformed' else json.dumps(value).encode(),
            stderr=b'PRIVATE PROMPT MUST NOT LEAK')
    monkeypatch.setattr(catalog.subprocess, 'run', run)
    with pytest.raises(ValueError) as error: catalog.preflight(tmp_path, path, 'gpt-6.1-sol', 'medium')
    assert 'PRIVATE' not in str(error.value)


@pytest.mark.parametrize('family', NAMES)
def test_all_eight_planners_freeze_catalog_and_execution_uses_only_copy(tmp_path, monkeypatch, family):
    source, root, binary, name = inputs(tmp_path, family)
    path = write(tmp_path)
    cfg, receipt, plan = eval_protocol.configure(source, family=name, phase='pilot', data=root, bindir=binary,
                                                model_catalog=path)
    assert plan['artifacts'][str(path)] == catalog.inspect(path, cfg['model'])['sha256']
    assert plan['codex_model_catalog']['model'] == cfg['model']
    frozen = tmp_path / 'frozen'
    paths = eval_inputs.prepare(plan, frozen)
    path.unlink()
    seen = []
    def preflight(bindir, copied, model, reasoning=None):
        assert bindir == str(frozen / 'bin') and copied == paths[str(path)]
        seen.append(catalog.inspect(copied, model, reasoning))
    monkeypatch.setattr(catalog, 'preflight', preflight)
    actual, _ = eval_inputs.execution(plan, frozen)
    assert actual['environment']['model_catalog'] == paths[str(path)] and len(seen) == 1
    # Frozen mutation fails before any parser/model execution.
    Path(paths[str(path)]).write_text(json.dumps(document('wrong')), encoding='utf-8')
    with pytest.raises(ValueError, match='snapshot changed'): eval_inputs.execution(plan, frozen)
    assert len(seen) == 1


def test_catalog_identity_cannot_be_substituted_in_plan_metadata(tmp_path, monkeypatch):
    source, root, binary, name = inputs(tmp_path)
    _, _, plan = eval_protocol.configure(source, family=name, phase='pilot', data=root, bindir=binary,
                                         model_catalog=write(tmp_path))
    plan['codex_model_catalog']['entry_sha256'] = '0' * 64
    frozen = tmp_path / 'frozen'; eval_inputs.prepare(plan, frozen)
    monkeypatch.setattr(catalog, 'preflight', lambda *a: pytest.fail('invalid binding reached parser'))
    with pytest.raises(ValueError, match='identity differs'): eval_inputs.execution(plan, frozen)


@pytest.mark.parametrize('family', NAMES)
def test_reader_binds_selected_catalog_to_dispatch_without_cli(tmp_path, monkeypatch, family):
    source, root, binary, name = inputs(tmp_path, family)
    path = write(tmp_path)
    _, _, plan = eval_protocol.configure(source, family=name, phase='pilot', data=root, bindir=binary,
                                         model_catalog=path)
    run = tmp_path / 'run'; run.mkdir()
    paths = eval_inputs.prepare(plan, run / 'inputs')
    attempt = run / 'attempt'; attempt.mkdir()
    if family == 'swe-milestone':
        filename = 'milestone-execute-request.json'
        request = {'execution': {'model_catalog': paths[str(path)]}}
        settings = request['execution']
    else:
        filename = 'harbor-request.json' if family in ('terminal-bench', 'terminal-bench-science', 'deep-swe', 'swe-bench-pro') else 'swe-request.json'
        request = {'model_catalog': paths[str(path)]}; settings = request
    record = attempt / filename; record.write_text(json.dumps(request), encoding='utf-8')
    settings['model_catalog_sha256'] = plan['codex_model_catalog']['sha256']
    record.write_text(json.dumps(request), encoding='utf-8')
    monkeypatch.setattr(catalog.subprocess, 'run', lambda *a, **k: pytest.fail('reader executed CLI'))
    evidence = []
    identity = eval_host_compare.binding(plan, attempt, run, paths, evidence)
    assert identity['sha256'] == plan['artifacts'][str(path)] and len(evidence) == 2
    settings['model_catalog'] = str(path)
    record.write_text(json.dumps(request), encoding='utf-8')
    with pytest.raises(ValueError, match='actual Codex model catalog differs'):
        eval_host_compare.binding(plan, attempt, run, paths, [])


def test_configure_cli_accepts_explicit_catalog(tmp_path, capsys):
    from ctxpress.__main__ import main
    source, root, binary, name = inputs(tmp_path)
    path = write(tmp_path); output = tmp_path / 'config.json'
    main(['eval', 'configure', str(source), '--family', name, '--phase', 'pilot',
          '--data', str(root), '--bindir', str(binary), '--model-catalog', str(path), '--output', str(output)])
    capsys.readouterr()
    assert json.loads(output.read_text(encoding='utf-8'))['environment']['model_catalog'] == str(path)
