"""Missing runtime bundles fail before credentials/model work; old fixtures survive."""
import json, sys
from pathlib import Path
import pytest
from ctxpress.harness import codex_binary, eval_inputs, eval_plan, evaluation

posix_fixture = pytest.mark.skipif(sys.platform == 'win32', reason='probe fixture uses POSIX shebang executables')


def cli(root, version='0.159.0-alpha.12.1'):
    root.mkdir(parents=True, exist_ok=True)
    path = root/'codex'
    path.write_text('#!/bin/sh\nprintf "codex-cli ' + version + '\\n"\n', encoding='utf-8')
    path.chmod(0o755)
    return root


def host(root, *, output='42', broken=False, slow=False):
    path = root/codex_binary.COMPANION
    source = '''import json, os, struct, sys, time
assert 'OPENAI_API_KEY' not in os.environ and 'CTXPRESS_CODEX_AUTH_FILE' not in os.environ
assert os.environ['CODEX_HOME'] == os.environ['HOME'] == os.getcwd()
assert not os.path.exists(os.path.join(os.environ['HOME'], 'auth.json'))
def read():
    header = sys.stdin.buffer.read(4)
    return json.loads(sys.stdin.buffer.read(struct.unpack('<I', header)[0]))
def send(obj):
    raw = json.dumps(obj).encode()
    sys.stdout.buffer.write(struct.pack('<I', len(raw)) + raw); sys.stdout.buffer.flush()
hello = read()
assert hello['type'] == 'connection/hello'
send(dict(type='connection/ready', selectedVersion=1, capabilities=[]))
opened = read(); sid = opened['request']['sessionId']
send(dict(type='operation/response', id=1, result=dict(status='ok', value=dict(type='session/ready', sessionId=sid))))
executed = read(); request = executed['request']['request']
assert request['enabled_tools'] == [] and request['source'] == 'text(6 * 7)'
send(dict(type='operation/response', id=2, result=dict(status='ok', value=dict(type='execution/started', cellId='1'))))
send({'type':'execute/initialResponse','id':2,'result':{'status':'ok','value':{'Result':{
    'cell_id':'1','content_items':[{'type':'input_text','text':OUTPUT}],'error_text':None}}}})
sys.stdin.buffer.read()
'''.replace('OUTPUT', repr(output))
    if broken: source = 'raise SystemExit(1)\n'
    if slow: source = 'import time; time.sleep(30)\n'
    path.write_text('#!' + sys.executable + '\n' + source, encoding='utf-8')
    path.chmod(0o755)
    return path


@pytest.mark.parametrize('version', ['0.159.0-alpha.12.1', 'codex-cli 0.159.0-alpha.12.1', '0.159.1'])
def test_relevant_release_requires_companion(version):
    assert codex_binary.requires_companion(version)


@pytest.mark.parametrize('version', ['0.158.0', '0.114.0', 'fixture-version', 'standalone-version', None])
def test_other_releases_do_not_invent_companion_requirements(version):
    assert not codex_binary.requires_companion(version)


@posix_fixture
def test_capture_marks_missing_helper_and_preflight_blocks_it(tmp_path):
    root=cli(tmp_path/'bin')
    artifacts, missing=codex_binary.capture(root)
    assert set(artifacts)=={str(root/'codex')} and missing==[str(root/codex_binary.COMPANION)]
    with pytest.raises(ValueError, match='bundle is incomplete'):
        codex_binary.preflight(root)


@posix_fixture
def test_non_executable_cli_cannot_pass_as_an_opaque_fixture(tmp_path):
    root=cli(tmp_path/'bin');host(root);(root/'codex').chmod(0o644)
    with pytest.raises(ValueError, match='CLI is not executable'):
        codex_binary.preflight(root)


@posix_fixture
def test_older_executable_and_opaque_fixtures_remain_hashable(tmp_path):
    root=cli(tmp_path/'old', '0.114.0')
    assert codex_binary.preflight(root)['health'] is None
    opaque=tmp_path/'opaque'; opaque.mkdir(); (opaque/'codex').write_bytes(b'non executable fixture')
    assert codex_binary.capture(opaque)[1]==[]
    helper=host(opaque)
    assert str(helper) in codex_binary.capture(opaque)[0]


@posix_fixture
def test_actual_ipc_probe_executes_javascript_in_isolated_home(tmp_path, monkeypatch):
    root=cli(tmp_path/'bin'); helper=host(root)
    monkeypatch.setenv('OPENAI_API_KEY','must-not-be-inherited')
    monkeypatch.setenv('CTXPRESS_CODEX_AUTH_FILE',str(tmp_path/'must-not-be-read-auth.json'))
    assert codex_binary.preflight(root)['health']==dict(protocol_version=1,javascript_executed=True,output='42',model_calls=0)
    helper.chmod(0o644)
    with pytest.raises(ValueError, match='not executable'):
        codex_binary.preflight(root)


@pytest.mark.parametrize('kwargs', [dict(output='wrong'), dict(broken=True), dict(slow=True)])
@posix_fixture
def test_help_or_presence_cannot_substitute_for_runtime_health(tmp_path, kwargs):
    root=cli(tmp_path/'bin'); helper=host(root,**kwargs)
    with pytest.raises(ValueError, match='health|JavaScript'):
        codex_binary.probe_companion(helper,timeout=0.2)


@pytest.mark.parametrize('mode', ['checkpoint','task_start'])
@posix_fixture
def test_planners_bind_mandatory_companion_and_frozen_copies_are_probed(tmp_path, mode):
    if mode=='checkpoint':
        from test_eval_inputs import inputs
        config=inputs(tmp_path)
    else:
        from test_task_start_plan import config as fresh_config
        config=fresh_config(tmp_path)
    root=cli(Path(config['environment']['bindir']))
    (root/codex_binary.COMPANION).unlink(missing_ok=True)
    incomplete=eval_plan.compile_plan(config)
    assert str(root/codex_binary.COMPANION) in incomplete['missing_environment_files']
    helper=host(root)
    plan=eval_plan.compile_plan(config)
    assert str(helper) in plan['artifacts']
    directory=evaluation.prepare(plan,tmp_path/'run')
    helper.unlink(); (root/'codex').unlink()
    frozen,_=eval_inputs.execution(plan,directory/'inputs')
    assert frozen['environment']['bindir']==str(directory/'inputs/bin')
    # Hash validation cannot be relaxed to accept a different runtime helper.
    (directory/'inputs/bin'/codex_binary.COMPANION).write_text('replacement', encoding='utf-8')
    with pytest.raises(ValueError, match='snapshot changed'):
        eval_inputs.execution(plan,directory/'inputs')


@posix_fixture
def test_unreviewed_helper_in_snapshot_does_not_repair_an_incomplete_plan(tmp_path):
    from test_task_start_plan import config
    cfg=config(tmp_path); root=cli(Path(cfg['environment']['bindir']))
    plan=eval_plan.compile_plan(cfg); directory=evaluation.prepare(plan,tmp_path/'run')
    host(directory/'inputs/bin')
    with pytest.raises(ValueError, match='not bound'):
        eval_inputs.execution(plan,directory/'inputs')


@pytest.mark.parametrize('platform', ['win32', 'linux'])
def test_opaque_fixture_with_x_ok_is_never_launched(tmp_path,monkeypatch,platform):
    (tmp_path/'codex').write_bytes(b'opaque fixture executable')
    monkeypatch.setattr(codex_binary,'_host_platform',lambda:platform)
    monkeypatch.setattr(codex_binary.os,'access',lambda *a:True)
    monkeypatch.setattr(codex_binary,'version',lambda *a:pytest.fail('launched an opaque fixture'))
    artifacts,missing=codex_binary.capture(tmp_path)
    assert str(tmp_path/'codex') in artifacts and missing==[]
    assert codex_binary.inspect(tmp_path)['binary_format']=='opaque'


@pytest.mark.parametrize('platform,header', [('win32',b'\x7fELF'),('win32',b'#!/bin/sh\n'),
    ('linux',b'MZ\x00\x00'),('linux',b'\xcf\xfa\xed\xfe'),('darwin',b'\x7fELF')])
def test_foreign_executable_is_hashable_but_actual_preflight_is_unavailable(tmp_path,monkeypatch,platform,header):
    (tmp_path/'codex').write_bytes(header)
    monkeypatch.setattr(codex_binary,'_host_platform',lambda:platform)
    monkeypatch.setattr(codex_binary.os,'access',lambda *a:True)
    monkeypatch.setattr(codex_binary,'version',lambda *a:pytest.fail('launched a foreign executable'))
    artifacts,missing=codex_binary.capture(tmp_path)
    assert str(tmp_path/'codex') in artifacts and missing==[]
    assert codex_binary.inspect(tmp_path)['preflight_unavailable']
    with pytest.raises(ValueError,match='preflight unavailable'):
        codex_binary.preflight(tmp_path)


@pytest.mark.parametrize('platform,header', [('win32',b'MZ\x00\x00'),('linux',b'\x7fELF'),
    ('linux',b'#!/bin/sh\n'),('darwin',b'\xcf\xfa\xed\xfe')])
def test_broken_native_executable_is_not_silently_skipped(tmp_path,monkeypatch,platform,header):
    (tmp_path/'codex').write_bytes(header)
    monkeypatch.setattr(codex_binary,'_host_platform',lambda:platform)
    monkeypatch.setattr(codex_binary.os,'access',lambda *a:True)
    def broken(*args):raise OSError('native loader failed')
    monkeypatch.setattr(codex_binary,'version',broken)
    with pytest.raises(OSError,match='native loader failed'):
        codex_binary.capture(tmp_path)


@pytest.mark.parametrize('mode',['checkpoint','task_start'])
@pytest.mark.parametrize('header',[b'opaque fixture',b'\x7fELF'])
def test_windows_data_only_planners_keep_hash_only_capture(tmp_path,monkeypatch,mode,header):
    if mode=='checkpoint':
        from test_eval_inputs import inputs
        config=inputs(tmp_path)
    else:
        from test_task_start_plan import config as fresh_config
        config=fresh_config(tmp_path)
    binary=Path(config['environment']['bindir'])/'codex';binary.write_bytes(header)
    monkeypatch.setattr(codex_binary,'_host_platform',lambda:'win32')
    monkeypatch.setattr(codex_binary.os,'access',lambda *a:True)
    monkeypatch.setattr(codex_binary,'version',lambda *a:pytest.fail('Windows planning launched its data input'))
    plan=eval_plan.compile_plan(config)
    assert str(binary) in plan['artifacts']
    bundle_paths = {str(binary), str(binary.with_name(codex_binary.COMPANION))}
    assert not any(value == path or value.startswith(path + ':')
                   for value in plan['missing_environment_files'] for path in bundle_paths)
