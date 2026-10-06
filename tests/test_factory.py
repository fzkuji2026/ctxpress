"""A running method must retain resolved configuration across later sessions."""
from ctxpress import ContextManager
from ctxpress.hosts.codex import launch
from ctxpress.live.factory import frozen_factory
from ctxpress.methods import Pichay
from ctxpress.replay.calibrate import fit


def calibration(path):
    before = [dict(seg='call', size=5, kind='read', res=['a.py'], text='cat a.py'),
              dict(seg='out', size=100, kind='read', res=['a.py'], sub='code', text='x' * 400)]
    trace = dict(name='training', prefix=0, alpha=1, reqs=[dict(before=before, t=i+1) for i in range(4)])
    fit([trace], path)
    return {'class':'WithMemory', 'args':{'inner':{'class':'CostModel',
            'args':{'profile':str(path), 'allow_summary':False}}}}


def test_manager_resolves_nested_external_profile_once(tmp_path):
    path = tmp_path / 'profile.json'
    manager = ContextManager(calibration(path))
    path.unlink()
    for key in ('one', 'two'):
        manager.apply([{'role':'user','content':'task'}], session=key)
    contexts = [session.ctx for session in manager.rewriter.sessions.values()]
    assert contexts[0].method.inner is not contexts[1].method.inner
    assert all(ctx.method.inner.provenance['sessions'] == ['training'] for ctx in contexts)


def test_launch_resolves_profile_before_native_host_starts(tmp_path, monkeypatch):
    path = tmp_path / 'profile.json'
    entry = calibration(path)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex'))
    monkeypatch.setenv('CTXPRESS_HOME', str(tmp_path / 'ctxpress'))
    real_serve = launch.serve
    active = []
    def serve(*args, **kwargs):
        server, rw = real_serve(*args, **kwargs)
        active.append(rw)
        return server, rw
    def native_host(command, env):
        path.unlink()
        entry['args']['inner']['args']['allow_summary'] = True
        for key in ('one', 'two'):
            active[0].rewrite_body({'input':[{'role':'user','content':'task'}]}, key)
        assert all(not session.ctx.method.requires_summary for session in active[0].sessions.values())
        return 0
    monkeypatch.setattr(launch, 'serve', serve)
    monkeypatch.setattr(launch.subprocess, 'call', native_host)
    assert launch.run(entry, codex_bin='fixture')[1][0] == 0
    assert not list((tmp_path / 'codex').glob('ctxpress-*.config.toml'))


def test_method_template_and_session_state_are_independent():
    original = Pichay(age=4)
    factory = frozen_factory(original)
    original.age = 100
    first, second = factory(), factory()
    assert first.age == second.age == 4
    first.age = 7
    assert second.age == factory().age == 4


def test_clawvm_recency_is_cleared_when_host_supersedes_history():
    manager = ContextManager('ClawVM')
    first = [{'role':'user','content':'task'},
             dict(type='function_call', call_id='a', name='shell', arguments='cat a.py'),
             dict(type='function_call_output', call_id='a', output='x' * 400)]
    manager.apply(first)
    context = manager.rewriter.sessions['default'].ctx
    assert context.method.recency
    manager.apply([{'role':'user','content':'new history'}])
    assert context.method.recency == {}


def test_cli_proxy_resolves_external_profile_once(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from ctxpress.live import proxy
    path = tmp_path / 'profile.json'
    entry = calibration(path)
    def serve(factory, *args, **kwargs):
        def run():
            path.unlink()
            first, second = factory(), factory()
            assert first.inner is not second.inner
            assert first.inner.provenance['sessions'] == second.inner.provenance['sessions'] == ['training']
        return SimpleNamespace(serve_forever=run), None
    monkeypatch.setattr(proxy, 'serve', serve)
    proxy.main(['--method',entry['class'],'--args',json.dumps(entry['args'])])
