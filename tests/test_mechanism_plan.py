"""The M1 helper declares a reviewable scope without executing experiments."""
import copy
import json
import subprocess
import pytest
from ctxpress.harness.checks import mechanism as mechanism_check
from ctxpress.harness.jobs import queue as evaluation, plan as eval_plan
from ctxpress.methods import METHODS, REGISTRY


def arguments(tmp_path):
    return ['--model', 'fixture-model', '--bindir', str(tmp_path/'bin'),
            '--scripts', str(tmp_path/'scripts'), '--out', str(tmp_path/'plan.json'),
            '--boundary', '0:435:198018']


def test_plan_covers_live_methods_and_reports_missing_resources_without_execution(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('plan launched a process'))
    monkeypatch.setattr(evaluation, 'start', lambda *a, **kw: pytest.fail('plan launched experiments'))
    mechanism_check.main(arguments(tmp_path))
    result = json.loads(capsys.readouterr().out)
    plan = json.loads((tmp_path/'plan.json').read_text(encoding='utf-8'))
    expected = {name for name in REGISTRY if METHODS[name][2]}
    selected = {entry['class'] for entry in plan['config']['methods']}
    missing = {row.split(':')[0] for row in result['omitted']}
    assert selected | missing == expected
    assert not selected & missing
    assert missing == {'SWEPruner', 'CostModel', 'AutoCostModel'}
    assert {'AgentFold', 'ACON', 'ReSum', 'Composed', 'NoCompaction'} <= selected
    assert plan['run_count'] == len(selected) and plan['max_parallel'] == 2
    assert plan['config']['boundaries'] == [dict(id='n0-j435', n=0, j=435, context_tokens=198018)]
    assert plan['config']['scope'] == 'mechanism' and plan['config']['run']['submit'] is False
    assert 'does not prove every operation' in result['evidence_scope']
    eval_plan.verify(plan, check_inputs=False)


def test_multiple_declared_boundaries_and_transport_settings_survive_compilation(tmp_path, capsys):
    mechanism_check.main(arguments(tmp_path) + ['--boundary', '1:23:128999',
        '--only', 'CodexAutoCompact', 'ACON', '--compact-limit', '128000', '--timeout', '321',
        '--via', 'http://fixture-proxy:7890', '--upstream', 'http://fixture-upstream/v1'])
    capsys.readouterr()
    plan = json.loads((tmp_path/'plan.json').read_text(encoding='utf-8'))
    assert plan['run_count'] == 4 and plan['agent_timeout_seconds_upper_bound'] == 1284
    assert all(job['compact_limit'] == 128000 for job in plan['jobs'])
    assert plan['config']['environment']['via'] == 'http://fixture-proxy:7890'
    assert plan['config']['environment']['upstream'] == 'http://fixture-upstream/v1'
    assert plan['config']['boundaries'][1]['context_tokens'] == 128999


@pytest.mark.parametrize('name,option', [('SWEPruner', '--pruner-url'),
    ('CostModel', '--cost-profile'), ('AutoCostModel', '--cost-policy')])
def test_explicit_selection_with_missing_resources_is_not_silently_omitted(tmp_path, capsys, name, option):
    with pytest.raises(SystemExit) as error:
        mechanism_check.main(arguments(tmp_path) + ['--only', name])
    assert error.value.code == 2
    assert f'{name} needs {option}' in capsys.readouterr().err
    assert not (tmp_path/'plan.json').exists()


def test_model_service_is_opted_in_and_summary_defaults_are_not_forced_to_trigger(tmp_path, capsys):
    before = copy.deepcopy(mechanism_check.ENTRIES)
    mechanism_check.main(arguments(tmp_path) + ['--only', 'SWEPruner', 'ReSum', 'CliffCompaction',
        'SlidingWindow', '--pruner-url', 'http://fixture-pruner:8080'])
    result = json.loads(capsys.readouterr().out)
    plan = json.loads((tmp_path/'plan.json').read_text(encoding='utf-8'))
    methods = {entry['class']: entry['args'] for entry in plan['config']['methods']}
    assert methods['SWEPruner']['url'] == 'http://fixture-pruner:8080'
    assert methods['ReSum']['k'] == 40 and plan['config']['run']['max_calls'] == 6
    assert methods['CliffCompaction']['t'] == 200000 and methods['SlidingWindow']['t'] == 230000
    assert not result['omitted'] and mechanism_check.ENTRIES == before


@pytest.mark.parametrize('value', ['-1:2:128000', '0:0:128000', '0:1:0', '0:1', '0:1:word'])
def test_invalid_boundary_cannot_publish_a_plan(tmp_path, value):
    args = arguments(tmp_path)
    args[-1] = value
    with pytest.raises(SystemExit):
        mechanism_check.main(args)
    assert not (tmp_path/'plan.json').exists()


def test_boundary_choice_is_required(tmp_path):
    with pytest.raises(SystemExit):
        mechanism_check.main(arguments(tmp_path)[:-2])
    assert not (tmp_path/'plan.json').exists()
