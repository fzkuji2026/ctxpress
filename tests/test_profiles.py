"""Native profile composition must preserve user configuration and method routing."""
import copy, datetime, os, stat, sys, tempfile
from pathlib import Path
import pytest
from ctxpress.hosts.codex import launch, profiles
from ctxpress.core import toml
from ctxpress._vendor import tomllib as bundled_toml


@pytest.mark.parametrize('flag', [['-p','research'],['--profile','research'],['--profile=research'],['-presearch'],['-p=research']])
def test_named_profile_is_consumed_once_and_opaque_arguments_stay_opaque(flag):
    name, overrides, remaining = profiles.arguments(['exec',*flag,'-c','model="fixture"','--','--profile','literal'])
    assert name == 'research' and overrides == ['model="fixture"']
    assert remaining == ['exec','--','--profile','literal']
    assert profiles.arguments(['--model','--help','exec','prompt'])[2] == ['--model','--help','exec','prompt']
    assert not launch.management_command(['--model','--help','exec','prompt'])


@pytest.mark.parametrize('args', [['-p','../private'],['--profile',''],['--profile'],['-p','one','-p','two'],['-c','model'],['--remote=ws://fixture']])
def test_invalid_profile_arguments_fail_before_binding_a_proxy(args, tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    monkeypatch.setattr(launch,'serve',lambda *a, **k: pytest.fail('started an invalid runtime'))
    with pytest.raises(ValueError):
        launch.run(codex_args=args,codex_bin='fixture')


def test_named_profile_keeps_user_options_and_sources_unchanged(tmp_path, monkeypatch):
    home = tmp_path / 'home'; home.mkdir()
    source = home / 'research.config.toml'
    original = '''model = "fixture-model"
model_reasoning_effort = "medium"
approval_policy = "on-request"
openai_base_url = "http://original.invalid/v1"
[features]
enable_request_compression = true
[permissions.research]
description = "custom permissions"
[mcp_servers.files]
command = "fixture-files"
args = ["--local"]
'''
    source.write_text(original, encoding='utf-8')
    base = home / 'config.toml'; base.write_text('model_verbosity="low"\n', encoding='utf-8')
    monkeypatch.setenv('CODEX_HOME', str(home))
    command = launch.codex_command('fixture',1234,['exec','-p','research','prompt'], profile='ctxpress-test', tools=False,
                                   codex_config={'model_auto_compact_token_limit':128000})
    generated = toml.load(home / 'ctxpress-test.config.toml')
    assert command.count('--profile') == 1 and '-p' not in command
    assert command[-2:] == ['exec','prompt']
    assert generated['model'] == 'fixture-model' and generated['model_reasoning_effort'] == 'medium'
    assert generated['approval_policy'] == 'on-request'
    assert generated['mcp_servers']['files']['args'] == ['--local']
    assert generated['permissions']['research']['description'] == 'custom permissions'
    assert generated['model_auto_compact_token_limit'] == 128000
    assert generated['openai_base_url'] == 'http://127.0.0.1:1234'
    assert not generated['features']['enable_request_compression']
    assert not generated['mcp_servers']['ctxpress']['enabled']
    assert source.read_text(encoding='utf-8') == original and base.read_text(encoding='utf-8') == 'model_verbosity="low"\n'
    if os.name != 'nt':
        # WSL's Windows mount can ignore chmod; exercise the file privacy
        # contract on a native POSIX filesystem without skipping composition.
        with tempfile.TemporaryDirectory(prefix='ctxpress-profile-mode-', dir='/tmp' if sys.platform == 'linux' else None) as private_home:
            monkeypatch.setenv('CODEX_HOME', private_home)
            launch.codex_command('fixture', 1234, ['exec', 'prompt'], profile='mode-test', tools=False)
            assert stat.S_IMODE((Path(private_home)/'mode-test.config.toml').stat().st_mode) == 0o600


def test_custom_provider_routing_inherits_base_profile_and_cli_in_order(tmp_path):
    (tmp_path/'config.toml').write_text('''model_provider = "gateway"
[model_providers.gateway]
name = "original provider"
base_url = "http://base.invalid/v1"
env_key = "FIXTURE_KEY"
''', encoding='utf-8')
    (tmp_path/'research.config.toml').write_text('''[model_providers.gateway]
base_url = "http://profile.invalid/v1"
request_max_retries = 2
''', encoding='utf-8')
    prepared = profiles.prepare(['exec','-p','research','--config','model_providers.gateway.base_url="http://cli.invalid/v1"',
        '--enable','enable_request_compression','-c','model_auto_compact_token_limit=1','-c','mcp_servers.ctxpress.enabled=false','prompt'],tmp_path)
    assert prepared.provider == 'gateway' and prepared.upstream == 'http://cli.invalid/v1'
    data, args = prepared.runtime(9, {'model_auto_compact_token_limit':128000, 'mcp_servers.ctxpress.enabled':True})
    assert data['model_providers']['gateway']['request_max_retries'] == 2
    effective = profiles.merge(toml.load(tmp_path/'config.toml'),data)
    flags = []
    for i, value in enumerate(args):
        if value == '-c':
            flags.append(args[i+1])
    for value in flags:
        effective = profiles.merge(effective, profiles._override(value))
    assert effective['model_providers']['gateway']['base_url'] == 'http://127.0.0.1:9'
    assert effective['model_providers']['gateway']['env_key'] == 'FIXTURE_KEY'
    assert effective['model_auto_compact_token_limit'] == 128000
    assert effective['mcp_servers']['ctxpress']['enabled']
    assert not effective['features']['enable_request_compression']


def test_launch_infers_custom_upstream_without_loading_auth(tmp_path, monkeypatch):
    home = tmp_path/'home'; home.mkdir()
    (home/'config.toml').write_text('''model_provider = "gateway"
[model_providers.gateway]
name = "fixture gateway"
base_url = "http://original.invalid/v1"
''', encoding='utf-8')
    (home/'auth.json').write_text('this fixture must never be parsed', encoding='utf-8')
    monkeypatch.setenv('CODEX_HOME',str(home)); monkeypatch.setenv('CTXPRESS_HOME',str(tmp_path/'ctxpress'))
    original = (home/'config.toml').read_bytes()
    real_serve, seen = launch.serve, []
    def serve(factory, port, upstream, *args, **kwargs):
        seen.append(upstream)
        return real_serve(factory, port, upstream, *args, **kwargs)
    monkeypatch.setattr(launch,'serve',serve)
    monkeypatch.setattr(launch.subprocess,'call',lambda *a, **k: 0)
    assert launch.run({'class':'NoCompaction'},codex_bin='fixture')[1][0] == 0
    assert seen == ['http://original.invalid/v1']
    assert (home/'config.toml').read_bytes() == original
    assert (home/'auth.json').read_text(encoding='utf-8') == 'this fixture must never be parsed'
    assert not list(home.glob('ctxpress-*.config.toml'))


def test_standard_and_bundled_parsers_roundtrip_complex_native_configuration():
    text = '''model = "fixture"
model_reasoning_effort = "medium"
model_instructions_file = "C:\\\\fixture\\\\rules.txt"
[permissions."name.with.dots"]
description = """multi\nline \\\"text\\\""""
allow = ["a", "b"]
[[hooks.start]]
command = ["fixture", "--name=quoted"]
[features]
enable_request_compression = true
'''
    expected = toml.loads(text)
    expected.update(date=datetime.date(2026,10,3),time=datetime.time(1,2,3),timestamp=datetime.datetime(2026,10,3,1,2,3,tzinfo=datetime.timezone.utc),
                    control='nul\x00 tab\t del\x7f emoji 🦊')
    encoded = toml.dumps(expected)
    assert toml.loads(encoded) == expected == bundled_toml.loads(encoded)


@pytest.mark.parametrize('suffix', ['missing.config.toml','bad.config.toml'])
def test_missing_or_broken_selected_profile_does_not_create_runtime_files(suffix, tmp_path, monkeypatch):
    if suffix.startswith('bad'):
        (tmp_path/suffix).write_text('model = "unterminated', encoding='utf-8')
    monkeypatch.setenv('CODEX_HOME',str(tmp_path))
    monkeypatch.setattr(launch,'serve',lambda *a, **k: pytest.fail('bound a proxy'))
    with pytest.raises((ValueError,FileNotFoundError)):
        launch.run(codex_args=['exec','-p',suffix.split('.')[0]],codex_bin='fixture')
    assert not list(tmp_path.glob('ctxpress-*.config.toml'))


def test_proxy_preserves_base_and_provider_query_params_and_rewrites_request(tmp_path):
    import json, threading, urllib.request
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from ctxpress.live.proxy import serve
    from ctxpress.methods import ComplexityTrap
    received = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append((self.path, body))
            raw = b'{"usage":{"input_tokens":10,"output_tokens":1}}'
            self.send_response(200); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    upstream = HTTPServer(('127.0.0.1',0),Upstream)
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    proxy, _ = serve(lambda: ComplexityTrap(n=1),0,f'http://127.0.0.1:{upstream.server_port}/v1?api-version=fixture',host='127.0.0.1')
    threading.Thread(target=proxy.serve_forever,daemon=True).start()
    history = [{'role':'user','content':'task'}]
    for call in ('a','b'):
        history += [dict(type='function_call',call_id=call,name='shell',arguments='cat a.py'),
                    dict(type='function_call_output',call_id=call,output='x' * 100)]
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{proxy.server_port}/responses?tenant=fixture',
            json.dumps({'input':history}).encode(),{'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=5) as response:
            assert response.status == 200
            response.read()
        assert received[0][0] == '/v1/responses?api-version=fixture&tenant=fixture'
        assert received[0][1]['input'][2]['output'] != history[2]['output']
    finally:
        proxy.shutdown(); proxy.server_close(); upstream.shutdown(); upstream.server_close()


def test_custom_provider_with_dotted_id_keeps_auth_in_private_config(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME',str(tmp_path))
    provider = 'gateway.with.dots'
    original = {'model_provider':provider, 'model_providers':{provider:{'name':'fixture gateway',
        'base_url':'http://fixture.invalid/v1', 'http_headers':{'X-Test':'fixture-static-header'}}}}
    (tmp_path/'config.toml').write_text(toml.dumps(original), encoding='utf-8')
    args = ['exec','-c','model_providers."gateway.with.dots".request_max_retries=2','prompt']
    prepared = profiles.prepare(args,tmp_path)
    cmd = launch.codex_command('fixture',9,args,profile='ctxpress-fixture',tools=False,prepared=prepared)
    data = toml.load(tmp_path/'ctxpress-fixture.config.toml')
    definition = data['model_providers'][provider]
    assert definition['base_url'] == 'http://127.0.0.1:9'
    assert definition['request_max_retries'] == 2 and definition['name'] == 'fixture gateway'
    assert definition['http_headers']['X-Test'] == 'fixture-static-header'
    assert 'fixture-static-header' not in ' '.join(cmd)
    assert toml.load(tmp_path/'config.toml') == original


def test_top_level_cli_removes_only_its_own_separator(monkeypatch):
    from ctxpress.__main__ import main
    received = []
    def run(entry, args, *others):
        received.append(args)
        return ['fixture',*args], (0, {'requests':0})
    monkeypatch.setattr(launch,'run',run)
    with pytest.raises(SystemExit) as done:
        main(['codex','--','exec','--','--profile','literal','--'])
    assert done.value.code == 0
    assert received == [['exec','--','--profile','literal','--']]
