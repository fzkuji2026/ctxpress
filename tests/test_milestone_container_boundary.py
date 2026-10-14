"""Native command projection cannot loosen ownership or introduce preparation."""
import ast,sys
from pathlib import Path
import pytest
from ctxpress.benchmarks.milestone.containers import Boundary, prepared_initialization
from test_milestone_resources import fixture,IMAGE


@pytest.mark.parametrize('missing', ['', 'python3\n', 'python3\nsudo\n'])
def test_prepared_tools_report_missing_dependencies_without_installing(monkeypatch, missing):
    from ctxpress.benchmarks.milestone import containers
    calls = []
    def checked(*args):
        calls.append(args)
        return missing
    monkeypatch.setattr(containers, 'docker', checked)
    if missing:
        with pytest.raises(RuntimeError, match='lacks required tools: ' + ', '.join(missing.split())):
            containers.check_prepared_tools('owned-agent')
    else:
        containers.check_prepared_tools('owned-agent')
    assert len(calls) == 1 and calls[0][:4] == ('exec', 'owned-agent', 'sh', '-c')
    assert 'apt-get' not in calls[0][-1] and 'apk' not in calls[0][-1]


@pytest.mark.skipif(sys.platform!='linux',reason='native Unix service socket')
def test_author_testcontainers_socket_is_replaced_by_owned_api_and_cleaned_after_verifier(tmp_path,monkeypatch):
    from ctxpress.harness.runtime import service_gateway, service_resources
    from test_service_resources import Engine
    registry,docker=fixture(tmp_path,monkeypatch)
    registry.service_images={'database':{'id':IMAGE,'reference':'redis:7'}}
    engine=Engine();original=engine.request
    def request(method,path,body=b''):
        if method=='GET' and path.startswith('/containers/json') and 'name' in path:
            return service_resources.Response.json(list(docker.containers.values()))
        return original(method,path,body)
    engine.request=request;monkeypatch.setattr(service_gateway,'Backend',lambda:engine)
    owner=registry.owner('verifier',IMAGE);gateway=registry.service_gateway(owner)
    command=launch(owner,tmp_path)
    command[2:2]=['--network','host','-v','/var/run/docker.sock:/var/run/docker.sock']
    Boundary(owner,'verifier',gateway).run(command)
    created=next(call for call in docker.calls if call[0]=='create')
    assert created[created.index('--network')+1]=='host'
    assert owner.record['service_scope']==gateway.record['scope']
    assert not any('/var/run/docker.sock' in value for value in created)
    assert 'DOCKER_HOST=unix:///ctxpress-services-api/docker.sock' in created
    assert gateway.record['channel']+':/ctxpress-services-api:ro' in created
    with pytest.raises(ValueError,match='verifier must be removed'):gateway.cleanup()
    assert Path(gateway.record['channel']).is_dir()
    registry.cleanup()
    assert not docker.containers and gateway.record['cleaned']
    assert not Path(gateway.record['channel']).exists()


def launch(owner,output):
    return ['docker','run','--pull=never','-d','--init','--name',owner.record['container'],'--cpus','2',
        '--ulimit','nofile=65535:65535','-v',str(output)+':/output',IMAGE,'tail','-f','/dev/null']


@pytest.mark.skipif(sys.platform!='linux',reason='native mounts require Linux paths')
@pytest.mark.parametrize('offline', [False, True])
def test_cargo_offline_policy_reaches_verifier_without_opening_network(tmp_path,monkeypatch,offline):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('verifier',IMAGE)
    Boundary(owner,'verifier',cargo_offline=offline).run(launch(owner,tmp_path))
    create=next(call for call in docker.calls if call[0]=='create')
    assert ('CARGO_NET_OFFLINE=true' in create) is offline
    assert create[create.index('--network')+1]==registry.project+'-grade'


@pytest.mark.skipif(sys.platform!='linux',reason='native mount projection requires Linux paths')
def test_author_launch_projects_to_fresh_owned_internal_network_and_output(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('verifier',IMAGE)
    Boundary(owner,'verifier').run(launch(owner,tmp_path),capture_output=True,text=True)
    assert owner.record['phase']=='running' and owner.record['network_mode']==registry.project+'-grade'
    create=next(call for call in docker.calls if call[0]=='create')
    assert create[create.index('--volume')+1]==str(tmp_path)+':/output:rw'
    assert create[create.index('--cpus')+1]=='2.0'


@pytest.mark.parametrize('change',['name','image','host-network','socket','host-auth','build','unknown-option'])
def test_native_start_cannot_change_resources_or_call_an_installer(tmp_path,monkeypatch,change):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('verifier',IMAGE);command=launch(owner,tmp_path)
    if change=='name':command[command.index('--name')+1]='foreign'
    if change=='image':command[-4]='sha256:'+'b'*64
    if change=='host-network':command[2:2]=['--network','host']
    if change=='socket':command[command.index('-v')+1]=str(tmp_path)+':/var/run/docker.sock'
    if change=='host-auth':command[command.index('-v')+1]=str(tmp_path)+':/tmp/host-codex:ro'
    if change=='build':command=['docker','build','--network=none','.']
    if change=='unknown-option':command[2:2]=['--privileged']
    with pytest.raises(ValueError):Boundary(owner,'verifier').run(command)
    assert not any(call[0]=='create' for call in docker.calls)


@pytest.mark.skipif(sys.platform!='linux',reason='native mount projection requires Linux paths')
def test_native_parity_failure_cleanup_uses_checked_owner_instead_of_best_effort_removal(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('verifier',IMAGE);boundary=Boundary(owner,'verifier')
    boundary.run(['docker','stop',owner.record['container']]);boundary.run(['docker','rm',owner.record['container']])
    assert not any(call[0] in ('stop','rm') for call in docker.calls)
    boundary.run(launch(owner,tmp_path))
    boundary.run(['docker','rm','-f',owner.record['container']])
    assert owner.record['cleaned'] and not docker.containers


def test_prepared_initialization_keeps_native_user_and_cache_logic_without_sudo_installation():
    source='''import shutil, subprocess
try:
    subprocess.run(['which', 'sudo'])
    subprocess.run(['apt-get', 'update'])
    subprocess.run(['apk', 'add', 'sudo'])
except Exception:
    pass
try:
    print('native fakeroot and cache setup')
    subprocess.run(['chown', '-R', f'{uid}:{gid}', '/testbed'], capture_output=True, text=True)
except Exception:
    pass
'''
    actual=prepared_initialization(source);ast.parse(actual)
    assert 'native fakeroot and cache setup' in actual and 'apt-get' not in actual and 'apk' not in actual
    assert "_ctxpress_chown_changed(['chown', '-R', f'{uid}:{gid}', '/testbed']" in actual
    with pytest.raises(ValueError,match='contract changed'):prepared_initialization('print("new installer shape")')
    no_chown=source.replace("    subprocess.run(['chown', '-R', f'{uid}:{gid}', '/testbed'], capture_output=True, text=True)\n",'')
    with pytest.raises(ValueError,match='recursive chown contract changed'):prepared_initialization(no_chown)


def test_recursive_chown_touches_only_entries_not_in_their_final_state(tmp_path,monkeypatch):
    import os,stat,subprocess
    from ctxpress.benchmarks.milestone.containers import CHOWN_CHANGED
    space={};exec(CHOWN_CHANGED,space);chown=space['_ctxpress_chown_changed']
    (tmp_path/'tree/sub').mkdir(parents=True)
    for name in ('a','sub/b','setid'):(tmp_path/'tree'/name).write_text('x')
    os.chmod(tmp_path/'tree/setid',0o2755)
    os.symlink('a',tmp_path/'tree/link')
    me=f'{os.getuid()}:{os.getgid()}';touched=[]
    real=os.lchown
    monkeypatch.setattr(os,'lchown',lambda path,uid,gid:touched.append(os.path.relpath(path,tmp_path/'tree')) or real(path,uid,gid))
    result=chown(['chown','-R',me,str(tmp_path/'tree')],capture_output=True,text=True)
    assert result.returncode==0 and touched==['setid']  # only the set-id file, which chown would change
    stats=[os.lstat(tmp_path/'tree'/n) for n in ('a','sub','sub/b','link')]
    assert all((s.st_uid,s.st_gid)==(os.getuid(),os.getgid()) for s in stats)
    failed=chown(['chown','-R','0:0',str(tmp_path/'tree')]) if os.getuid() else None
    if failed is not None:
        assert failed.returncode==1 and failed.stderr
        with pytest.raises(subprocess.CalledProcessError):chown(['chown','-R','0:0',str(tmp_path/'tree')],check=True)


def test_native_combined_host_gateway_is_removed_only_from_agent_launch(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('agent',IMAGE)
    command=['docker','run','--add-host=host.docker.internal:host-gateway','--name',owner.record['container'],
        IMAGE,'tail','-f','/dev/null']
    Boundary(owner,'agent').run(command)
    assert owner.record['network_mode']=='none'
    assert not any('host.docker.internal' in str(value) for call in docker.calls for value in call)
    verifier=registry.owner('verifier',IMAGE);command[command.index('--name')+1]=verifier.record['container']
    with pytest.raises(ValueError,match='host gateway'):Boundary(verifier,'verifier').run(command)


@pytest.mark.parametrize('stage,offline,vendor,expected',[
    ('verifier',True,True,True),('verifier',True,False,False),
    ('verifier',False,True,False),('agent',True,True,False),
])
def test_vendor_grader_uses_copied_rustup_default_only(tmp_path,monkeypatch,stage,offline,vendor,expected):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner(stage,IMAGE)
    command=launch(owner,tmp_path)
    command[2:2]=['-e','RUSTUP_TOOLCHAIN=1.85.0']
    Boundary(owner,stage,cargo_offline=offline,cargo_toolchain_default=vendor).run(command)
    create=next(call for call in docker.calls if call[0]=='create')
    assert ('RUSTUP_TOOLCHAIN=' in create) is expected
    assert ('RUSTUP_TOOLCHAIN=1.85.0' in create) is (not expected)
