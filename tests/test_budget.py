"""Retention budgets share an entry point, while preserving each algorithm's scope."""
import copy
import json
import pytest
from ctxpress import ContextManager, apply, build, BudgetSpec
from ctxpress.methods import REGISTRY, method_table
from ctxpress.methods.budget import with_budget
from ctxpress.live.context import LiveContext


ALIASES = [
    ('ComplexityTrap', 'n', 1), ('KeepLastTokens', 'b', 500),
    ('ARC', 'n', 1), ('TokenPilot', 'entry_budget', 100),
    ('AgentFold', 'keep_segments', 1), ('ClawVMApprox', 'n', 1),
]


def items():
    result = [dict(type='message', role='user', content='fix the bug')]
    for i in range(4):
        result.extend([dict(type='function_call', name='shell', call_id=f'c{i}', arguments=f'cat file{i}.py'),
                       dict(type='function_call_output', call_id=f'c{i}', output='original line\n' * 200)])
    return result


@pytest.mark.parametrize('name,alias,value', ALIASES)
def test_budget_and_legacy_parameter_produce_identical_live_requests(name, alias, value):
    entry = {'class': name, 'args': {'budget': value}}
    original = copy.deepcopy(entry)
    legacy = build({'class': name, 'args': {alias: value}})
    canonical = build(entry)
    direct = REGISTRY[name](budget=value)
    assert canonical.budget == direct.budget == value
    options = {'summarizer': lambda history: 'continuation note'}
    assert apply(canonical, items(), **options) == apply(legacy, items(), **options) == apply(direct, items(), **options)
    assert entry == original
    assert REGISTRY[name]().budget == REGISTRY[name].budget_spec.default
    assert REGISTRY[name](**{alias: value}, budget=value).budget == value
    with pytest.raises(ValueError, match='conflicts'):
        REGISTRY[name](**{alias: value + 1}, budget=value)


@pytest.mark.parametrize('name', [name for name, cls in REGISTRY.items() if cls.budget_spec])
def test_negative_budget_validation_precedes_execution(name):
    args = {'budget': -1}
    if name == 'EntryTruncation':
        args['inner'] = {'class': 'NoCompaction'}
    with pytest.raises(ValueError, match='budget'):
        build({'class': name, 'args': args})


@pytest.mark.parametrize('value', [True, 1.5, '10', None])
def test_budget_types_cannot_silently_change_retention(value):
    with pytest.raises(ValueError, match='budget'):
        ContextManager({'class': 'ARC', 'args': {'budget': value}})


@pytest.mark.parametrize('name', ['Pichay', 'PichayApprox', 'CodexAutoCompact', 'ReSum', 'ACON', 'CostModel', 'AutoCostModel', 'Composed'])
def test_ages_thresholds_frozen_policy_and_compositions_are_not_retention_budgets(name):
    with pytest.raises(ValueError, match='no retention budget'):
        build({'class': name, 'args': {'budget': 10}})


def test_zero_arc_budget_archives_every_output_and_preserves_call_pairs():
    manager = ContextManager('ARC', budget=0)
    original = items()
    result = manager.apply(original)
    assert [s for s in result if s['type'] == 'function_call'] == [s for s in original if s['type'] == 'function_call']
    outputs = [s for s in result if s['type'] == 'function_call_output']
    assert len(outputs) == 4 and all('stored at' in s['output'] for s in outputs)
    for output in manager.rewriter.sessions['default'].ctx.outputs():
        assert manager.retrieve(output['id']) == 'original line\n' * 200


def test_transparent_wrappers_forward_budget_to_inner_and_protect_requirements():
    entry = {'class': 'Trigger', 'args': {'threshold': 0, 'inner': {'class': 'WithMemory', 'args': {
        'inner': {'class': 'PinRequirements', 'args': {'inner': {'class': 'ComplexityTrap'}}}}}}}
    original = copy.deepcopy(entry)
    manager = ContextManager(entry, budget=0)
    history = items()
    history[1]['arguments'] = 'cat SPEC.md'
    result = manager.apply(history)
    assert history[1] in result and history[2] in result
    assert all('stored at' in s['output'] for s in result if s['type'] == 'function_call_output' and s['call_id'] != 'c0')
    assert entry == original
    conflict = {'class': 'WithMemory', 'args': {'inner': {'class': 'ARC', 'args': {'n': 4}}}}
    with pytest.raises(ValueError, match='conflicts with n'):
        ContextManager(conflict, budget=3)
    with pytest.raises(ValueError, match='constructing'):
        build({'class': 'WithMemory', 'args': {'inner': REGISTRY['ARC'](), 'budget': 1}})


def test_entry_truncation_budget_belongs_to_wrapper_and_nested_budget_stays_independent():
    entry = {'class': 'EntryTruncation', 'args': {'budget': 100, 'inner': {'class': 'ARC', 'args': {'budget': 1}}}}
    method = build(entry)
    assert method.budget == 100 and method.inner.budget == 1
    ctx = LiveContext(method)
    ctx.add_call('a', 'cat a.py')
    ctx.add_output('a', 'line\n' * 1000)
    assert ctx.outputs()[0]['form'] == 'truncated'
    ctx.add_call('b', 'cat b.py')
    ctx.add_output('b', 'line\n' * 1000)
    ctx.before_request()
    assert [s['form'] for s in ctx.outputs()] == ['memid', 'truncated']


def test_python_budget_option_is_declarative_and_conflicts_never_overwrite_configuration():
    assert apply('ComplexityTrap', items(), budget=1) == apply(REGISTRY['ComplexityTrap'](1), items())
    with pytest.raises(TypeError, match='constructing'):
        ContextManager(REGISTRY['ARC'](), budget=1)
    with pytest.raises(TypeError, match='constructing'):
        ContextManager(lambda: REGISTRY['ARC'](), budget=1)
    with pytest.raises(ValueError, match='conflicts'):
        ContextManager({'class': 'ARC', 'args': {'budget': 2}}, budget=1)
    with pytest.raises(ValueError, match='conflicts'):
        with_budget({'class': 'ARC', 'args': {'budget': True}}, 1)


def test_metadata_states_units_and_exposes_unsupported_methods():
    assert REGISTRY['ARC'].budget_spec.unit == 'outputs'
    assert REGISTRY['ScoredMethod'].budget_spec.unit == 'tokens'
    assert REGISTRY['AgentFold'].budget_spec.unit == 'segments'
    assert isinstance(REGISTRY['ClawVM'].budget_spec, BudgetSpec)
    table = method_table()
    assert 'budget 范围与单位' in table and 'recent full tool outputs: outputs' in table
    assert '| Pichay |' in table and 'inner method' in table


def test_cli_routes_budget_to_codex_serve_and_use(monkeypatch, capsys):
    from ctxpress.__main__ import main
    from ctxpress.hosts.codex import launch, install
    from ctxpress.live import proxy
    calls = []
    def fake_launch(entry, args, *positional, **kw):
        calls.append((entry, args, kw))
        return ['fixture'], (0, {})
    monkeypatch.setattr(launch, 'run', fake_launch)
    with pytest.raises(SystemExit) as exit_info:
        main(['codex', '--method', 'ARC', '--budget', '1', '--', 'exec', 'prompt --budget 9'])
    assert exit_info.value.code == 0
    assert calls[-1] == ({'class': 'ARC', 'args': {}}, ['exec', 'prompt --budget 9'], {'budget': 1})
    monkeypatch.setattr(proxy, 'main', lambda argv: calls.append(argv))
    main(['serve', '--method', 'ARC', '--budget', '1'])
    assert json.loads(calls[-1][calls[-1].index('--args') + 1]) == {'budget': 1}
    monkeypatch.setattr(install, 'use', lambda name, args: calls.append((name,args)) or {'method':name,'args':args})
    main(['use', 'ComplexityTrap', '--budget', '2'])
    assert calls[-1] == ('ComplexityTrap', {'budget': 2})
    capsys.readouterr()


def test_invalid_install_never_writes_settings_or_shell_files(tmp_path, monkeypatch):
    from ctxpress.hosts.codex import install
    root = tmp_path / 'settings'
    shell = tmp_path / '.bashrc'
    shell.write_text('existing shell configuration\n', encoding='utf-8')
    monkeypatch.setenv('CTXPRESS_HOME', str(root))
    with pytest.raises(ValueError, match='budget'):
        install.install('ARC', {'budget': -1}, files=[(str(shell), install.POSIX)])
    assert not root.exists() and shell.read_text(encoding='utf-8') == 'existing shell configuration\n'


def test_launcher_budget_is_resolved_before_starting_proxy(tmp_path, monkeypatch):
    from ctxpress.hosts.codex import launch
    from ctxpress import settings
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex'))
    monkeypatch.setenv('CTXPRESS_HOME', str(tmp_path / 'ctxpress'))
    monkeypatch.setattr(settings, 'load', lambda: dict(settings.DEFAULTS, method='ARC', args={}))
    seen = []
    class Server:
        server_address = ('127.0.0.1', 12345)
        def server_close(self):
            seen.append('closed')
    def serve(factory, *args, **kw):
        seen.append(factory().budget)
        return Server(), None
    monkeypatch.setattr(launch, 'serve', serve)
    monkeypatch.setattr(launch, 'codex_command', lambda *a, **kw: ['fixture'])
    launch.run(codex_args=['exec', 'fixture'], codex_bin='fixture', dry_run=True, budget=2)
    assert seen == [2, 'closed']
    with pytest.raises(ValueError, match='budget'):
        launch.run(codex_args=['exec', 'fixture'], codex_bin='fixture', dry_run=True, budget=-1)
    assert seen == [2, 'closed']
