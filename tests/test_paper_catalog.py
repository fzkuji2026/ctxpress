import json

import pytest

from ctxpress.methods.paper_catalog import describe, configure, validate_entries, method_entries


def test_fixed_cohort_has_only_registered_paper_methods():
    report = describe()
    assert report['paper_count'] == 16 and report['method_count'] == 18
    assert all(p['interface_registered'] for p in report['papers'])
    assert all(p['repository'] and p['paper_url'] and p['limits'] for p in report['papers'])
    assert validate_entries(method_entries('http://localhost:8000/prune'))


@pytest.mark.parametrize('name', ['AutoCostModel', 'InfiniteContext', 'CliffCompaction',
                                  'NoCompaction', 'ReSum', 'AgentFoldTools', 'WithMemory'])
def test_noncohort_and_legacy_methods_are_rejected(name):
    with pytest.raises(ValueError, match='outside'):
        validate_entries([{'class': name}])


def test_missing_pruner_cannot_silently_become_heuristic():
    with pytest.raises(ValueError):
        configure({}, None)


def test_preparation_keeps_benchmark_budget_and_copies_input():
    original = dict(methods=[{'class': 'NoCompaction'}], benchmark='swe-milestone', workers=3,
                    run={'timeout': 3600}, comparison={'reference': 'old', 'quality': 'per_task'})
    out = configure(original, 'http://localhost:8000/prune')
    assert len(out['methods']) == 18 and out['workers'] == 1
    assert out['run'] == original['run'] and out['benchmark'] == original['benchmark']
    assert out['comparison'] == {'reference': 'ComplexityTrap', 'quality': 'per_task'}
    assert original['workers'] == 3 and original['comparison']['reference'] == 'old'


def test_cli_generates_same_directory_config_without_execution(tmp_path, capsys):
    from ctxpress.harness.cli import main
    source, target = tmp_path/'base.json', tmp_path/'paper.json'
    source.write_text(json.dumps({'benchmark': 'swe-milestone', 'environment': {'data': '../data'}}))
    main(['paper-methods', '--config', str(source), '--output', str(target), '--pruner-url', 'http://localhost:8000/prune'])
    assert len(json.loads(target.read_text())['methods']) == 18
    assert json.loads(capsys.readouterr().out)['started'] is False
    with pytest.raises(SystemExit):
        main(['paper-methods', '--config', str(source), '--output', str(target)])


@pytest.mark.parametrize('phase', ['method-pilot', 'comparison'])
def test_common_protocol_compiles_papers_without_native_or_project_candidate(tmp_path, phase):
    from ctxpress.harness.jobs import protocol
    from ctxpress.methods.paper_catalog import VERSION
    from test_eval_protocol import inputs
    source, root, binary, family = inputs(tmp_path)
    document = json.loads(source.read_text())
    document['paper_cohort'] = VERSION
    methods = method_entries('http://localhost:8000/prune')
    next(e for e in methods if e['class'] == 'AgentDiet')['args']['reflect_model'] = document['models']['agentdiet_reflection']
    document['pilot'] = dict(methods=[methods[0]], workers=1, repeats=1)
    document['comparison'] = dict(methods=methods, workers=1, repeats=1)
    document['method_pilot'] = dict(method_classes=[e['class'] for e in methods], workers=1, repeats=1)
    source.write_text(json.dumps(document))
    config, receipt, plan = protocol.configure(source, family=family, phase=phase, data=root, bindir=binary)
    assert len(config['methods']) == 18 and config['workers'] == 1
    assert validate_entries(config['methods'])
    assert receipt['experiments_started'] == 0 and plan['jobs']


def test_protocol_rejects_nonpaper_method_before_preparing_anything():
    from ctxpress.harness.jobs.protocol import _protocol
    from pathlib import Path
    document = json.loads((Path(__file__).resolve().parents[1]/'configs/paper-context.protocol.json').read_text())
    document['comparison']['methods'].append({'class': 'CliffCompaction'})
    with pytest.raises(ValueError, match='outside'):
        _protocol(document)
