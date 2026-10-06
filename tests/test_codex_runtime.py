from pathlib import Path
import pytest
from ctxpress.hosts.codex import launch, doctor


@pytest.mark.parametrize('args', [['login'],['logout'],['features','list'],['mcp','list','--json'],['exec','--help'],['--version'],['--model','login','features','list']])
def test_native_management_commands_pass_through_without_method_or_proxy(args,tmp_path,monkeypatch):
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'codex'))
    monkeypatch.setenv('CTXPRESS_HOME',str(tmp_path/'ctxpress'))
    monkeypatch.setattr(launch,'serve',lambda *a,**k: pytest.fail('utility started a proxy'))
    monkeypatch.setattr(launch,'build',lambda *a,**k: pytest.fail('utility instantiated a method'))
    called=[]
    monkeypatch.setattr(launch.subprocess,'call',lambda cmd,env: called.append(cmd) or 0)
    command,result=launch.run({'class':'invalid-on-purpose'},codex_args=args,codex_bin='fixture')
    assert command==['fixture',*args] and called==[command] and result[1]['passthrough']
    assert not (tmp_path/'codex').exists() and not (tmp_path/'ctxpress').exists()


def test_standalone_runtime_flag_is_injected_once_and_arguments_are_preserved():
    args=['exec','--model','fixture','prompt']
    cmd=launch.codex_command('fixture',9,args,write=False)
    assert cmd.count('--no-daemon')==1 and cmd[-len(args):]==args
    cmd=launch.codex_command('fixture',9,['--no-daemon',*args],write=False)
    assert cmd.count('--no-daemon')==1
    assert not launch.management_command(['--model','login','exec','prompt'])
    assert not launch.management_command(['--','--help'])


@pytest.mark.parametrize('flag',['--ignore-user-config','--remote','--remote=ws://fixture'])
def test_configuration_bypass_is_rejected_before_starting_a_proxy(flag,monkeypatch):
    monkeypatch.setattr(launch,'serve',lambda *a,**k: pytest.fail('started a bypassed runtime'))
    with pytest.raises(ValueError,match='bypasses'):
        launch.run(codex_args=['exec',flag],codex_bin='fixture')


def test_binary_inspection_uses_empty_home_and_reports_only_capabilities(tmp_path,monkeypatch):
    from types import SimpleNamespace
    binary=tmp_path/'codex'; binary.write_text('offline executable fixture', encoding='utf-8')
    commands=[]
    def run(command,**kw):
        home=Path(kw['env']['CODEX_HOME'])
        assert home.is_dir() and not (home/'auth.json').exists()
        commands.append(command)
        output='fixture version' if command[-1]=='--version' else '--profile --no-daemon --strict-config'
        return SimpleNamespace(stdout=output,returncode=0)
    monkeypatch.setattr(doctor.subprocess,'run',run)
    result=doctor.inspect_binary(binary)
    assert result['supported'] and len(commands)==2 and result['version']=='fixture version'
    assert len(result['sha256'])==64


def test_offline_check_timeout_terminates_the_owned_fixture_group(tmp_path,monkeypatch):
    import subprocess,sys,time
    from ctxpress.core import processes
    monkeypatch.setattr(doctor,'inspect_binary',lambda *a: dict(binary='fixture',supported=True))
    real_popen=subprocess.Popen
    started=[]
    def slow_fixture(command,**kwargs):
        if command[1:4]!=['-m','ctxpress.hosts.codex.doctor','fixture-launch']:
            return real_popen(command,**kwargs)
        process=real_popen([sys.executable,'-c','import time; time.sleep(30)'],**kwargs)
        started.append(process)
        return process
    monkeypatch.setattr(doctor.subprocess,'Popen',slow_fixture)
    beginning=time.monotonic()
    result=doctor.offline_check(directory=tmp_path,timeout=0.1)
    assert time.monotonic()-beginning < 10
    assert result['timed_out'] and not result['passed'] and result['test_only']
    assert not processes.alive(started[0].pid)
    assert not list(tmp_path.glob('*.method.json')) and not list(tmp_path.glob('codex-offline-*'))
