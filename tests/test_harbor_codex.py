"""Official Agent lifecycle contracts; these fixtures never call a model or Docker."""
import asyncio, json, shlex
from types import SimpleNamespace
import pytest
from ctxpress.harness.runtime.codex_agent import CallProgress, framework, HOME


class OfficialFixture:
    _OUTPUT_FILENAME = 'codex.txt'

    def __init__(self, model_name='fixture-model', **kwargs):
        self.model_name = model_name
        self.rendered = None

    async def setup(self, environment):
        pytest.fail('invoked official installer')

    def _build_register_skills_command(self):
        return 'register fixture skills'

    def _build_register_mcp_servers_command(self):
        return 'register fixture MCP'

    async def run(self, instruction, environment, context):
        self.rendered = 'official-rendered: ' + instruction
        self.commands = self.create_run_agent_commands(self.rendered)
        if environment.run_error:
            raise environment.run_error
        context['official'] = True


def settings():
    return dict(method={'class':'NoCompaction'}, model='fixture-model', reasoning='medium',
        binary_version='0.159.0-alpha.12.1', compact_limit=230000,
        upstream='https://provider.invalid/v1', auth_file='/runtime-only/auth.json')


def agent(cfg=None):
    return framework(OfficialFixture, SimpleNamespace, cfg or settings())()


class EnvironmentFixture:
    def __init__(self, version='codex-cli 0.159.0-alpha.12.1\n', run_error=None, cleanup_fails=False):
        self.commands = []
        self.uploads = []
        self.version = version
        self.run_error = run_error
        self.cleanup_fails = cleanup_fails

    async def exec(self, command, **kwargs):
        self.commands.append((command, kwargs))
        value = ''
        if command == '/cxbin/codex --version':
            value = self.version
        elif 'model relay did not become ready' in command:
            value = json.dumps(dict(pid=42, url='http://127.0.0.1:3456'))
        code = int(self.cleanup_fails and command.startswith('rm -f '))
        return SimpleNamespace(stdout=value, return_code=code)

    async def upload_file(self, source, target):
        self.uploads.append((source, target))


def test_frozen_cli_uses_private_credentials_and_official_session_location():
    env = EnvironmentFixture(); instance = agent()
    asyncio.run(instance.setup(env))
    assert instance._version == settings()['binary_version']
    assert env.uploads == [('/runtime-only/auth.json', HOME + '/auth.json')]
    assert all('/logs/' not in target for _, target in env.uploads)
    assert not any('npm' in command or 'pip install' in command for command, _ in env.commands)
    assert any('ln -s /logs/agent/sessions ' in command for command, _ in env.commands)
    assert instance.create_cleanup_commands() == []


def test_official_rendering_and_context_are_preserved_through_the_wrapper():
    env = EnvironmentFixture(); instance = agent(); context = {}
    asyncio.run(instance.setup(env))
    asyncio.run(instance.run('repair $(literal) `text`', env, context))
    assert context == {'official':True} and instance.ctxpress_credentials_cleaned
    commands = instance.commands
    assert [item.command for item in commands[:-1]] == ['register fixture skills', 'register fixture MCP']
    args = shlex.split(commands[-1].command)
    assert args[:3] == ['python3','-m','ctxpress.harness.runtime.agent_process']
    assert ['python3','-m','ctxpress','codex','--codex-bin'] == args[6:11]
    assert args[args.index('--via')+1] == 'http://127.0.0.1:3456'
    assert 'official-rendered: repair $(literal) `text`' in args
    assert 'model_reasoning_effort=medium' in args
    assert commands[-1].env['CODEX_HOME'] == HOME
    assert 'OPENAI_API_KEY' not in commands[-1].env
    assert '/logs/agent/codex.txt' in args


@pytest.mark.parametrize('error', [RuntimeError('agent failed'), asyncio.CancelledError()])
def test_credentials_are_checked_after_agent_failure_and_cancellation(error):
    env = EnvironmentFixture(run_error=error); instance = agent()
    asyncio.run(instance.setup(env))
    with pytest.raises(type(error)):
        asyncio.run(instance.run('task', env, {}))
    assert instance.ctxpress_credentials_cleaned
    assert env.commands[-1][0].startswith('rm -f ' + HOME + '/auth.json')


def test_setup_failure_also_removes_and_verifies_uploaded_credentials():
    env = EnvironmentFixture(version='codex-cli wrong'); instance = agent()
    with pytest.raises(ValueError, match='pinned binary'):
        asyncio.run(instance.setup(env))
    assert env.uploads and instance.ctxpress_credentials_cleaned


def test_cleanup_failure_is_an_error_and_cannot_be_treated_as_success():
    env = EnvironmentFixture(cleanup_fails=True); instance = agent()
    asyncio.run(instance.setup(env))
    with pytest.raises(RuntimeError, match='cleanup command failed'):
        asyncio.run(instance.run('task', env, {}))
    assert not instance.ctxpress_credentials_cleaned


def test_preexisting_private_home_is_never_adopted_or_cleaned():
    class ExistingEnvironment(EnvironmentFixture):
        async def exec(self, command, **kwargs):
            self.commands.append((command, kwargs))
            return SimpleNamespace(stdout='', return_code=1)
    env=ExistingEnvironment();instance=agent()
    with pytest.raises(RuntimeError):asyncio.run(instance.setup(env))
    assert not env.uploads and len(env.commands)==1
    assert not instance._ctxpress_home_owned and not instance.ctxpress_credentials_cleaned


def test_cancellation_during_cleanup_waits_for_credential_deletion():
    async def check():
        entered=asyncio.Event();release=asyncio.Event();cleaned=[]
        env=EnvironmentFixture();instance=agent();await instance.setup(env)
        original=instance.cleanup_credentials
        async def cleanup(environment):
            entered.set();await release.wait();await original(environment);cleaned.append(True)
        instance.cleanup_credentials=cleanup
        task=asyncio.create_task(instance.run('task',env,{}))
        await entered.wait();task.cancel();release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert cleaned and instance.ctxpress_credentials_cleaned
    asyncio.run(check())


def test_commands_cannot_be_created_before_setup_or_for_another_model():
    instance = agent()
    with pytest.raises(RuntimeError, match='setup'):
        instance.create_run_agent_commands('task')
    asyncio.run(instance.setup(EnvironmentFixture()))
    instance.model_name = 'another-model'
    with pytest.raises(ValueError, match='declared model'):
        instance.create_run_agent_commands('task')


def test_method_parameters_and_prompt_are_quoted_without_shell_interpretation():
    cfg = settings()
    text = 'preserve $SECRETS and `commands` and $(substitution)'
    cfg['method'] = {'class':'ReSum','args':{'summary_guidance':text}}
    instance = agent(cfg); asyncio.run(instance.setup(EnvironmentFixture()))
    command = instance.create_run_agent_commands(text)[-1].command
    args = shlex.split(command)
    assert json.loads(args[args.index('--args')+1])['summary_guidance'] == text
    assert shlex.quote(text) in command


@pytest.mark.parametrize('field,value',[('compact_limit',True), ('model',''), ('reasoning',None), ('binary_version','')])
def test_incomplete_settings_are_rejected_before_any_container_work(field, value):
    cfg=settings(); cfg[field]=value
    with pytest.raises(ValueError):
        agent(cfg)


def test_partial_sessions_and_inflight_parallel_calls_do_not_end_budget_early(tmp_path):
    path=tmp_path/'rollout.jsonl';progress=CallProgress(tmp_path)
    def row(kind,identifier):return json.dumps(dict(type='response_item',payload={'type':kind,'call_id':identifier}))
    partial=row('function_call','a');path.write_text(partial[:-3], encoding='utf-8')
    assert progress.update()==(0,False)
    with path.open('a', encoding='utf-8') as output:output.write(partial[-3:]+'\n'+row('function_call','b')+'\n'+row('function_call_output','a')+'\n')
    assert progress.update()==(2,False)
    with path.open('a', encoding='utf-8') as output:output.write(row('function_call_output','b')+'\n')
    assert progress.update()==(2,True) and progress.update()==(2,True)


def test_call_budget_cancels_official_execution_and_stops_agent_before_auth_cleanup(tmp_path):
    stopped=[]
    class LimitReached(RuntimeError):pass
    class WaitingAgent(OfficialFixture):
        def __init__(self,**kwargs):super().__init__(**kwargs);self.logs_dir=tmp_path
        async def run(self,instruction,environment,context):
            directory=tmp_path/'sessions';directory.mkdir()
            (directory/'session.jsonl').write_text('\n'.join(json.dumps(dict(type='response_item',payload={'type':kind,'call_id':'fixture'}))
                for kind in ('function_call','function_call_output'))+'\n', encoding='utf-8')
            try:await asyncio.Event().wait()
            finally:stopped.append(True)
    cfg=settings();cfg['max_calls']=1
    instance=framework(WaitingAgent,SimpleNamespace,cfg,limit_error=LimitReached)();env=EnvironmentFixture()
    asyncio.run(instance.setup(env))
    with pytest.raises(LimitReached):asyncio.run(instance.run('task',env,{}))
    assert stopped and instance.ctxpress_stop=='max_calls' and instance.ctxpress_credentials_cleaned
    assert '--stop' in env.commands[-2][0] and env.commands[-1][0].startswith('rm -f ')


def test_credential_journal_is_set_before_upload_and_cleared_only_after_verified_delete():
    states=[]
    class UploadFailure(EnvironmentFixture):
        async def upload_file(self,source,target):
            assert states==[True]
            raise RuntimeError('synthetic upload failed')
    instance=framework(OfficialFixture,SimpleNamespace,settings(),credential_state=states.append)()
    with pytest.raises(RuntimeError,match='upload failed'):asyncio.run(instance.setup(UploadFailure()))
    assert states==[True,False] and instance.ctxpress_credentials_cleaned
    states.clear();instance=framework(OfficialFixture,SimpleNamespace,settings(),credential_state=states.append)()
    env=EnvironmentFixture(cleanup_fails=True);asyncio.run(instance.setup(env))
    with pytest.raises(RuntimeError):asyncio.run(instance.cleanup_credentials(env))
    assert states==[True] and not instance.ctxpress_credentials_cleaned
