import json
import sys
from pathlib import Path

import pytest

from ctxpress.harness import author_acm as bridge


def prepared(tmp_path, monkeypatch):
    root = tmp_path/'author'; root.mkdir()
    data = tmp_path/'data.json'; data.write_text('[]')
    config = tmp_path/'config.yaml'; config.write_text('runtime: {}')
    index = tmp_path/'index'; index.mkdir()
    monkeypatch.setattr(bridge, 'verify_checkout', lambda path: (root, {'src/run.py': 'a'*64}))
    args = dict(checkout=root, python=Path(sys.executable).resolve(), model='openai/acm-9b',
                api_base='http://127.0.0.1:8000/v1', data=data, config=config, index=index, output=tmp_path/'out')
    return args


def test_preparation_is_explicit_and_does_not_launch_or_claim_weights(tmp_path, monkeypatch):
    args = prepared(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge.subprocess, 'Popen', lambda *a, **kw: pytest.fail('prepare launched a process'))
    plan = bridge.prepare(**args)
    assert '--use_memory_tool' in plan['command'] and '--limit' in plan['command']
    assert plan['checkpoint_identity_verified'] is plan['execution_verified'] is False
    assert (tmp_path/'out'/'launch.json').exists()
    with pytest.raises(ValueError, match='must be new'):
        bridge.prepare(**args)


def test_inline_credentials_and_missing_index_refused(tmp_path, monkeypatch):
    args = prepared(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='credentials'):
        bridge.prepare(**dict(args, api_base='http://user:secret@localhost/v1'))
    with pytest.raises(ValueError, match='index'):
        bridge.prepare(**dict(args, index=tmp_path/'missing'))


def test_author_bridge_cannot_launch_outside_shared_evaluator(tmp_path, monkeypatch):
    args = prepared(tmp_path, monkeypatch)
    plan = bridge.prepare(**args, timeout=1)
    monkeypatch.setattr(bridge.subprocess, 'Popen', lambda *a, **kw: pytest.fail('standalone execution launched'))
    assert plan['execution_supported'] is False
    assert plan['unified_method'] == {'class': 'ACM'}
    with pytest.raises(ValueError, match='standalone ACM execution is disabled'):
        bridge.execute(plan, args['output'])
    assert not (args['output']/'author.log').exists()


def test_retired_execute_cli_rejected_before_preparing_inputs(tmp_path, monkeypatch):
    args = prepared(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, 'prepare', lambda *a, **kw: pytest.fail('retired entry prepared inputs'))
    argv = ['--execute']
    for key, value in args.items():
        argv += ['--' + key.replace('_', '-'), str(value)]
    with pytest.raises(SystemExit) as exc:
        bridge.main(argv)
    assert exc.value.code == 2
