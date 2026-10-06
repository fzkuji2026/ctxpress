import copy, json
from pathlib import Path
import pytest
from ctxpress import ContextManager
from ctxpress.core import engine, policy
from ctxpress.core.params import DEFAULT
from ctxpress.methods import AutoCostModel, Composed, CostModel, WithMemory, build
from ctxpress.replay.tune import tune


def history(name, length=8):
    before = [dict(seg='call', size=5, kind='read', res=['a.py'], text='cat a.py'),
              dict(seg='out', size=2500, kind='read', res=['a.py'], outpaths=[], sub='code', text='PRIVATE SOURCE\n'*700)]
    reqs = [dict(before=before, input=0, cached=0, t=1)]
    for i in range(1,length):
        reqs.append(dict(before=[dict(seg='call', size=5, kind='command', res=[], text='true'),
             dict(seg='out', size=200, kind='command', res=[], outpaths=[], text='ok\n'*200)], input=0,cached=0,t=i+1))
    return dict(name=name, prefix=50, alpha=1.0, reqs=reqs)


def test_offline_policy_roundtrip_matches_real_engine_and_preserves_parameters(tmp_path):
    params = DEFAULT.override(cached=0.2, out=3.5)
    path = tmp_path / 'policy.json'
    tuned = tune([history('train-a'), history('train-b')], path, [100,0,100], params=params,
                 method_args=dict(allow_summary=False, use_reexplore=False, lookahead=4))
    assert tuned['selected_lambda'] == 0 and not tuned['fallback']
    frozen = policy.load(path)
    assert 'PRIVATE SOURCE' not in path.read_text(encoding='utf-8') and 'cat a.py' not in path.read_text(encoding='utf-8')
    assert [row['lam'] for row in frozen['screening']['candidates']] == [0,100]
    method = AutoCostModel(path)
    direct = CostModel(profile=frozen['statistics'], lam=0, **frozen['method_args'])
    got = engine.run(history('unseen'), method)
    expected = engine.run(history('unseen'), direct, params)
    assert got == expected
    manager = ContextManager({'class':'AutoCostModel', 'args':{'policy':str(path)}})
    manager.apply([{'role':'user','content':'task'}])
    context = manager.rewriter.sessions['default'].ctx
    assert context.CACHED == 0.2 and context.OUT == 3.5
    assert manager.codex_config['model_auto_compact_token_limit'] == 230000
    with pytest.raises(ValueError, match='host parameters'):
        engine.run(history('unseen'), AutoCostModel(path), DEFAULT.override(cached=0.7))
    assert WithMemory(AutoCostModel(path)).parameters.get('cached') == 0.2


def test_candidate_evidence_uses_only_other_sessions_to_fit_and_falls_back(tmp_path, monkeypatch):
    calls = []
    def fake_run(held, method, params):
        if isinstance(method, CostModel):
            assert held['name'] not in method.provenance['sessions']
            assert len(method.provenance['sessions']) == 2
            calls.append((held['name'], tuple(method.provenance['sessions']), method.lam))
            return dict(cost=10, silent=1, spec_gap=0)
        return dict(cost=20, silent=0, spec_gap=0)
    monkeypatch.setattr(engine, 'run', fake_run)
    path = tmp_path / 'policy.json'
    result = tune([history('a'), history('b'), history('c')], path, [0,100], reference={'class':'CodexAutoCompact','args':{'t':128000}})
    assert len(calls) == 6 and result['fallback'] and result['selected_lambda'] is None
    method = build({'class':'AutoCostModel','args':{'policy':str(path)}})
    assert method.codex_config == {'model_auto_compact_token_limit':128000}
    assert not method.requires_summary and not isinstance(method.inner, CostModel)


def test_policy_rejects_tampering_constraint_changes_and_parameter_conflicts(tmp_path):
    path = tmp_path / 'policy.json'
    tune([history('a'), history('b')], path, [0,100])
    original = path.read_bytes()
    bundle = policy.load(path)
    changed = copy.deepcopy(bundle)
    changed['screening']['selected_lambda'] = 100
    with pytest.raises(ValueError, match='changed'):
        policy.validate(changed)
    with pytest.raises(ValueError, match='constraint evidence'):
        policy.seal(changed)
    assert path.read_bytes() == original
    changed = copy.deepcopy(bundle)
    changed['screening']['candidates'][0]['folds'][0]['ok'] = False
    with pytest.raises(ValueError, match='inconsistent'):
        policy.seal(changed)
    changed = copy.deepcopy(bundle)
    changed['parameters'][0]['value'] = float('nan')
    with pytest.raises(ValueError):
        policy.save(path, changed)
    assert path.read_bytes() == original
    second = tmp_path / 'second.json'
    tune([history('a'), history('b')], second, [0], params=DEFAULT.override(cached=0.3))
    with pytest.raises(ValueError, match='conflicting frozen'):
        Composed([AutoCostModel(path), AutoCostModel(second)])


def test_policy_is_fingerprinted_and_mounted_for_nested_live_evaluations(tmp_path):
    from ctxpress.harness.jobs.plan import compile_plan, verify
    from ctxpress.benchmarks.milestone.checkpoint_run import _container_entry
    path = tmp_path / 'policy.json'
    tune([history('a'), history('b')], path, [0], method_args=dict(allow_summary=False))
    entry = {'class':'WithMemory','args':{'inner':{'class':'AutoCostModel','args':{'policy':'policy.json'}}}}
    plan = compile_plan(dict(schema='ctxpress.eval', version=1, scope='mechanism', backend='codex_docker',
        model='fixture', environment={'bindir':str(tmp_path / 'missing-bin')},
        boundaries=[dict(id='point', n=3,j=14,context_tokens=47365)], methods=[entry]), base_dir=tmp_path)
    assert str(path) in plan['artifacts']
    home = tmp_path / 'home'; home.mkdir()
    mounted = _container_entry(plan['jobs'][0]['method'], home)
    source = mounted['args']['inner']['args']['policy']
    assert source.startswith('/cxhome/calibration/') and next((home/'calibration').glob('*.json')).read_bytes()==path.read_bytes()
    path.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='input changed'):
        verify(plan)


def test_parallel_screening_has_the_same_policy_as_serial(tmp_path):
    traces = [history('a'), history('b')]
    one = tune(traces, tmp_path/'one.json', [0,100], workers=1)
    two = tune(traces, tmp_path/'two.json', [0,100], workers=2)
    assert one['sha256'] == two['sha256']


def test_tune_cli_excludes_evaluation_session_and_preserves_portable_settings(tmp_path, monkeypatch, capsys):
    from ctxpress.__main__ import main
    from ctxpress.replay import corpus
    traces = [history('train-a'),history('train-b'),history('evaluation')]
    monkeypatch.setattr(corpus, 'load_manifest', lambda *a,names=None,**kw: [t for t in traces if not names or t['name'] in names])
    path = tmp_path/'policy.json'
    main(['tune','--manifest','fixture','--exclude','evaluation','--lambdas','0','100','--output',str(path),
          '--reference-limit','128000','--params','{"cached":0.3}'])
    output = json.loads(capsys.readouterr().out)
    bundle = policy.load(path)
    assert output['sessions'] == 2 and bundle['screening']['sessions'] == ['train-a','train-b']
    method = AutoCostModel(path)
    assert method.parameters.get('cached') == 0.3 and method.parameters.get('window') == 128000
    assert method.codex_config['model_auto_compact_token_limit'] == 128000
    assert all('evaluation' not in fold['session'] for row in bundle['screening']['candidates'] for fold in row['folds'])


def test_invalid_tuning_request_does_not_replace_an_existing_policy(tmp_path):
    path = tmp_path/'policy.json'
    tune([history('a'),history('b')], path, [0])
    original = path.read_bytes()
    with pytest.raises(ValueError, match='positive integer'):
        tune([history('a'),history('b')], path, [0], reference={'class':'CodexAutoCompact','args':{'t':0}})
    with pytest.raises(ValueError, match='uniquely named'):
        tune([history('a'),history('a')], path, [0])
    assert path.read_bytes() == original
