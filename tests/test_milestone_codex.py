"""Official agent hook contract; no Docker, credentials or model calls."""
import ast, json, shlex
from pathlib import Path
import pytest
from ctxpress.benchmarks.milestone.codex_hook import framework


class OfficialFixture:
    def __init__(self, **kwargs): self.kwargs=kwargs
    def get_container_mounts(self): return ['-v', '/fixture-wheelhouse:/wheelhouse:ro']
    def get_container_init_script(self, agent_name): pytest.fail('original installer was invoked')
    def build_run_command(self, model, session_id, prompt_path):
        return 'codex exec --model fixture --json "$(cat /tmp/prompt)"'
    def build_resume_command(self, model, session_id, message_path):
        return 'codex exec resume fixture-id --json "$(cat /tmp/message)"'


def settings():
    return dict(runtime='/frozen/runtime',bindir='/frozen/bin',logs='/owned/logs',store='/owned/store',
                method={'class':'ComplexityTrap','args':{'budget':10}},binary_version='0.159.0-alpha.12.1',compact_limit=230000)


def test_new_and_resumed_invocations_both_use_frozen_codex_through_ctxpress():
    agent=framework(OfficialFixture,settings())()
    first=agent.build_run_command('fixture',None,'/tmp/prompt')
    second=agent.build_resume_command('fixture','fixture-id','/tmp/message')
    for command in (first,second):
        args=shlex.split(command)
        assert args[:6]==['env','PYTHONPATH=/ctxpress-runtime','python3','-m','ctxpress','codex']
        assert args[args.index('--codex-bin')+1]=='/cxbin/codex'
        assert json.loads(args[args.index('--args')+1])=={'budget':10}
        assert args[args.index('--store-dir')+1]=='/ctxpress-store'
        assert 'model_auto_compact_token_limit=230000' in args
    assert first.endswith('exec --model fixture --json "$(cat /tmp/prompt)"')
    assert second.endswith('exec resume fixture-id --json "$(cat /tmp/message)"')
    assert shlex.split(first)[shlex.split(first).index('--log')+1]!=shlex.split(second)[shlex.split(second).index('--log')+1]


def test_mounts_only_include_declared_runtime_agent_and_output_paths():
    mounts=framework(OfficialFixture,settings())().get_container_mounts()
    assert mounts==['-v','/fixture-wheelhouse:/wheelhouse:ro',
        '-v','/frozen/runtime:/ctxpress-runtime:ro','-v','/frozen/bin:/cxbin:ro',
        '-v','/owned/logs:/ctxpress-logs:rw','-v','/owned/store:/ctxpress-store:rw']


def test_initialization_is_valid_python_without_an_installer():
    source=framework(OfficialFixture,settings())().get_container_init_script('codex')
    ast.parse(source)
    assert 'npm' not in source and 'pip' not in source and 'subprocess' not in source
    assert '/tmp/host-codex/auth.json' in source and 'destination.chmod(0o600)' in source


def test_exact_alpha_version_is_preserved_and_compared():
    agent=framework(OfficialFixture,settings())()
    version=agent.parse_version_output('codex-cli 0.159.0-alpha.12.1\n')
    assert version==agent.get_requested_version() and agent.version_matches_request(version)
    assert not agent.version_matches_request('0.159.0')
    assert agent.get_version_command()==['/cxbin/codex','--version']


def test_frozen_prerelease_binary_does_not_use_the_author_npm_version_selector():
    class StableInstaller(OfficialFixture):
        def __init__(self,agent_version=None,**kwargs):
            assert agent_version is None,'frozen CLI must not use the npm selector'
            super().__init__(**kwargs)
    cls=framework(StableInstaller,settings())
    agent=cls(agent_version=settings()['binary_version'])
    assert agent.get_requested_version()=='0.159.0-alpha.12.1'
    assert not agent.version_matches_request('0.159.0')
    with pytest.raises(ValueError,match='differs from the frozen binary'):
        cls(agent_version='0.159.0')


def test_unknown_official_command_shape_cannot_silently_bypass_the_method():
    agent=framework(OfficialFixture,settings())()
    with pytest.raises(ValueError,match='shape changed'):
        agent._wrap('OTHER_BASE_URL=fixture codex exec')


def test_method_arguments_are_shell_quoted_as_data():
    cfg=settings();guidance='keep `$SECRET` and $(echo never) as literal data'
    cfg['method']={'class':'ReSum','args':{'summary_guidance':guidance}}
    command=framework(OfficialFixture,cfg)().build_run_command('fixture',None,'/tmp/prompt')
    args=shlex.split(command)
    assert json.loads(args[args.index('--args')+1])['summary_guidance']==guidance
    assert shlex.quote(json.dumps(cfg['method']['args'])) in command


@pytest.mark.parametrize('path',['relative/path','C:/windows/path','/path:with:colon'])
def test_invalid_mount_paths_are_rejected_before_execution(path):
    cfg=settings();cfg['runtime']=path
    with pytest.raises(ValueError,match='mount path'):
        framework(OfficialFixture,cfg)


def private_settings():
    return dict(settings(),private_runtime=True,channel='/owned/channel',profiles='/owned/profiles',
        upstream='https://fixture.invalid/responses')


def test_private_native_framework_never_mounts_host_auth_and_keeps_real_session_directory():
    cls=framework(OfficialFixture,private_settings());agent=cls()
    mounts=agent.get_container_mounts()
    assert agent.ctxpress_private_runtime and agent.api_key is None and agent.base_url is None
    assert mounts[:6]==['-v','/frozen/runtime/ctxpress:/ctxpress-runtime/ctxpress:ro',
        '-v','/owned/logs/sessions:/home/fakeroot/.codex/sessions:rw','-v','/owned/channel:/ctxpress-channel:ro']
    assert '/fixture-wheelhouse:/wheelhouse:ro' not in mounts and not any('/tmp/host-codex' in value for value in mounts)
    assert agent.get_container_env_vars()==[]
    source=agent.get_container_init_script('codex');ast.parse(source)
    assert 'pre-existing Agent authentication' in source and 'shutil.copy' not in source


def test_private_framework_requires_a_stable_loopback_relay_for_new_and_resumed_commands():
    cls=framework(OfficialFixture,private_settings());agent=cls()
    with pytest.raises(ValueError,match='not been prepared'):agent.build_run_command('fixture',None,'/prompt')
    cls.set_model_relay('http://127.0.0.1:32123')
    for command in (agent.build_run_command('fixture',None,'/prompt'),cls().build_resume_command('fixture','thread','/resume')):
        args=shlex.split(command)
        assert 'CODEX_HOME=/home/fakeroot/.codex' in args and 'ctxpress.harness.runtime.agent_process' in args
        assert args[args.index('--pid-file')+1]=='/ctxpress-private/agent.json'
        assert args[args.index('--via')+1]=='http://127.0.0.1:32123'
    with pytest.raises(ValueError,match='switch'):cls.set_model_relay('http://127.0.0.1:32124')


@pytest.mark.parametrize('change',['auth-mount','host-proxy','missing-channel','credential-url','http-upstream','remote-relay','invalid-relay-port'])
def test_private_native_model_settings_cannot_bypass_the_owned_channel(change):
    cfg=private_settings()
    if change=='auth-mount':cfg['auth_file']='/host/auth.json'
    if change=='host-proxy':cfg['via']='http://host.invalid:80'
    if change=='missing-channel':cfg.pop('channel')
    if change=='credential-url':cfg['upstream']='https://key:secret@fixture.invalid/responses'
    if change=='http-upstream':cfg['upstream']='http://fixture.invalid/responses'
    with pytest.raises(ValueError):
        cls=framework(OfficialFixture,cfg)
        cls.set_model_relay('http://outside.invalid:1234' if change=='remote-relay' else 'http://127.0.0.1:70000')
