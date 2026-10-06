"""Native resource lifecycle faults; no Docker daemon, credentials or models."""
import io,json,tarfile
from pathlib import Path
import pytest
from ctxpress.benchmarks.milestone import resources

IMAGE='sha256:'+'a'*64
PROJECT='ctxp-ms-'+'1'*24


class Docker:
    def __init__(self):
        self.calls=[];self.containers={};self.networks={};self.daemon='fixture-daemon'
        self.lost_response=False;self.failed_remove=False
    def __call__(self,*args):
        self.calls.append(args)
        if args[0]=='info':return self.daemon
        if args[:2]==('image','inspect'):return json.dumps([dict(Id=IMAGE,Os='linux')])
        if args[0]=='ps':
            name=args[-1].split('=',1)[1].strip('^/$');return self.containers.get(name,{}).get('Id','')
        if args[:2]==('container','inspect'):return json.dumps([self.containers[args[2]]])
        if args[0]=='create':
            name=args[args.index('--name')+1];network=args[args.index('--network')+1]
            labels=dict(args[i+1].split('=',1) for i,value in enumerate(args) if value=='--label')
            self.containers[name]=dict(Id='container-'+name,Name='/'+name,Image=IMAGE,
                Config={'Labels':labels},HostConfig={'NetworkMode':network},State={'Running':False})
            if self.lost_response:raise RuntimeError('creation response lost')
            return self.containers[name]['Id']
        if args[0]=='start':self.containers[args[1]]['State']['Running']=True;return ''
        if args[0]=='exec':return ''
        if args[0]=='rm':
            if not self.failed_remove:self.containers.pop(args[-1])
            return ''
        if args[:2]==('network','ls'):
            name=args[-1].split('=',1)[1].strip('^$');return self.networks.get(name,{}).get('Id','')
        if args[:2]==('network','create'):
            name=args[-1];labels=dict(args[i+1].split('=',1) for i,value in enumerate(args) if value=='--label')
            self.networks[name]=dict(Id='network-'+name,Name=name,Internal='--internal' in args,Labels=labels)
            return self.networks[name]['Id']
        if args[:2]==('network','inspect'):
            row=dict(self.networks[args[2]],Containers={value['Id']:{} for value in self.containers.values()
                if value['HostConfig']['NetworkMode']==args[2]})
            return json.dumps([row])
        if args[:2]==('network','rm'):self.networks.pop(args[2]);return ''
        pytest.fail('unexpected Docker operation: '+str(args[:2]))


def fixture(tmp_path,monkeypatch):
    docker=Docker();monkeypatch.setattr(resources,'docker',docker)
    return resources.Registry(PROJECT,'fixture-run',tmp_path/'run'),docker


@pytest.mark.linux_only
def test_agent_mounts_frozen_catalog_as_read_only_file(tmp_path, monkeypatch):
    from ctxpress.harness.runtime.codex_catalog import CONTAINER_PATH
    registry,docker=fixture(tmp_path,monkeypatch)
    source=tmp_path/'models.json';source.write_text('{"models":[]}', encoding='utf-8')
    agent=registry.owner('agent',IMAGE)
    agent.create(mounts=[(str(source),CONTAINER_PATH,'ro')])
    command=next(call for call in docker.calls if call[0]=='create')
    assert str(source)+':'+CONTAINER_PATH+':ro' in command
    registry.cleanup()
    assert not docker.containers


@pytest.mark.parametrize('change',['verifier','writable','wrong-target','symlink','missing','credentials'])
def test_catalog_mount_exception_does_not_allow_other_files(tmp_path, monkeypatch, change):
    from ctxpress.harness.runtime.codex_catalog import CONTAINER_PATH
    registry,docker=fixture(tmp_path,monkeypatch)
    source=tmp_path/'models.json';source.write_text('synthetic fixture only', encoding='utf-8')
    target,mode=CONTAINER_PATH,'ro'
    if change=='writable':mode='rw'
    if change=='wrong-target':target='/other.json'
    if change=='symlink':
        link=tmp_path/'link.json';link.symlink_to(source);source=link
    if change=='missing':source=tmp_path/'missing.json'
    if change=='credentials':
        source=tmp_path/'auth.json';source.write_text('explicitly fake test credential', encoding='utf-8')
    owner=registry.owner('verifier' if change=='verifier' else 'agent',IMAGE)
    with pytest.raises(ValueError,match='native mounts'):
        owner.create(mounts=[(str(source),target,mode)])
    assert not any(call[0]=='create' for call in docker.calls)


def test_one_agent_and_parallel_graders_keep_distinct_owners_and_one_private_network(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch)
    agent=registry.owner('agent',IMAGE);agent.create()
    first=registry.owner('verifier',IMAGE);first.create(cpus=2)
    second=registry.owner('verifier',IMAGE);second.create(cpus=4)
    assert first.record['container']!=second.record['container']
    assert agent.record['network_mode']=='none' and first.record['network_mode']==second.record['network_mode']==PROJECT+'-grade'
    assert sum(call[:2]==('network','create') for call in docker.calls)==1
    with pytest.raises(ValueError,match='one Agent'):registry.owner('agent',IMAGE)
    registry.cleanup()
    assert not docker.containers and not docker.networks
    assert all(owner.record['cleaned'] for owner in registry.owners) and registry.network['cleaned']


def test_incomplete_native_trial_prevents_aggregate_container_removal(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch)
    agent=registry.owner('agent',IMAGE);agent.create()
    grader=registry.owner('verifier',IMAGE);grader.create()
    registry.protect_trial(agent.record['container'])
    with pytest.raises(RuntimeError,match='retain owned resources'):registry.cleanup()
    with pytest.raises(RuntimeError,match='retain owned resources'):agent.cleanup()
    assert len(docker.containers)==2 and not any(call[0]=='rm' for call in docker.calls)
    record=dict(schema='ctxpress.eval.milestone_drain',version=1,container=agent.record['container'],
        phase='drain-incomplete',agent_quiesced=True,watcher_joined=False,author_cleanup_returned=False)
    with pytest.raises(ValueError,match='drain evidence'):registry.release_trial(record)
    record.update(phase='author-cleanup-returned',watcher_joined=True,author_cleanup_returned=True)
    registry.release_trial(record);registry.cleanup()
    assert not docker.containers and not docker.networks


def test_native_trial_guard_rejects_foreign_or_duplicate_agent(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch)
    with pytest.raises(ValueError,match='registered Agent'):registry.protect_trial('foreign')
    agent=registry.owner('agent',IMAGE);registry.protect_trial(agent.record['container'])
    with pytest.raises(ValueError,match='registered Agent'):registry.protect_trial(agent.record['container'])


@pytest.mark.parametrize('change',['daemon','label','image','container-id','network'])
def test_cleanup_refuses_changed_or_foreign_resource_ownership(tmp_path,monkeypatch,change):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('agent',IMAGE);owner.create()
    row=docker.containers[owner.record['container']]
    if change=='daemon':docker.daemon='another-daemon'
    if change=='label':row['Config']['Labels']['ctxpress.run']='another-run'
    if change=='image':row['Image']='sha256:'+'b'*64
    if change=='container-id':row['Id']='another-container'
    if change=='network':row['HostConfig']['NetworkMode']='host'
    with pytest.raises(ValueError):owner.cleanup()
    assert not owner.record['cleaned'] and not any(call[0]=='rm' for call in docker.calls)


def test_lost_creation_response_is_recoverable_from_prior_owner_journal(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('agent',IMAGE);docker.lost_response=True
    with pytest.raises(RuntimeError,match='response lost'):owner.create()
    assert owner.record['phase']=='creating' and 'container_id' not in owner.record
    owner.cleanup();assert owner.record['cleaned'] and not docker.containers


def test_failed_removal_stays_unfinished_and_does_not_delete_grading_network(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('verifier',IMAGE);owner.create();docker.failed_remove=True
    with pytest.raises(RuntimeError,match='removal'):registry.cleanup()
    assert not owner.record['cleaned'] and not registry.network['cleaned'] and docker.networks


def test_cleanup_attempts_other_owned_containers_after_one_foreign_container_is_found(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch)
    first=registry.owner('verifier',IMAGE);first.create();second=registry.owner('verifier',IMAGE);second.create()
    docker.containers[first.record['container']]['Config']['Labels']['ctxpress.run']='foreign'
    with pytest.raises(ValueError,match='ownership'):registry.cleanup()
    assert not first.record['cleaned'] and second.record['cleaned'] and not registry.network['cleaned']


def test_private_credentials_are_uploaded_without_host_copy_and_removed_after_process_stop(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('agent',IMAGE);owner.create()
    owner.record['private_initialized']=True;owner.persist()
    source=tmp_path/'runtime-auth';source.write_text('{"tokens":"synthetic-only"}', encoding='utf-8')
    uploads=[]
    def upload(command,**options):uploads.append((command,options))
    monkeypatch.setattr(resources.subprocess,'run',upload)
    owner.credentials(source)
    assert owner.record['credentials_may_exist'] and not any(path.name=='auth.json' for path in registry.folder.rglob('*'))
    with tarfile.open(fileobj=io.BytesIO(uploads[0][1]['input'])) as archive:
        assert archive.getnames()==['auth.json'] and archive.extractfile('auth.json').read()==source.read_bytes()
    owner.cleanup()
    stop=next(i for i,call in enumerate(docker.calls) if '--stop' in call)
    removal=next(i for i,call in enumerate(docker.calls) if call[0]=='exec' and 'rm -f '+resources.PRIVATE in call[-1])
    delete=next(i for i,call in enumerate(docker.calls) if call[0]=='rm')
    assert stop<removal<delete and not owner.record['credentials_may_exist'] and owner.record['cleaned']


def test_failed_auth_upload_is_journaled_before_any_secret_can_reach_docker(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('agent',IMAGE);owner.create()
    owner.record['private_initialized']=True;owner.persist();source=tmp_path/'auth';source.write_text('synthetic', encoding='utf-8')
    def upload(*args,**kwargs):
        assert json.loads(owner.path.read_text(encoding='utf-8'))['credentials_may_exist']
        raise RuntimeError('upload interrupted')
    monkeypatch.setattr(resources.subprocess,'run',upload)
    with pytest.raises(RuntimeError):owner.credentials(source)
    assert owner.record['credentials_may_exist']
    owner.cleanup();assert owner.record['cleaned'] and not owner.record['credentials_may_exist']


def test_author_npm_offline_environment_reaches_the_owned_container(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('agent',IMAGE)
    owner.create(environment={'npm_config_offline':'true','GOPROXY':'off'})
    launch=next(call for call in docker.calls if call[0]=='create')
    values=[launch[index+1] for index,value in enumerate(launch) if value=='--env']
    assert 'npm_config_offline=true' in values and 'GOPROXY=off' in values
    registry.cleanup()
    assert owner.record['cleaned'] and not docker.containers


@pytest.mark.parametrize('change',['socket-mount','relative-mount','credential-environment','lowercase-credential'])
def test_resource_options_cannot_expose_a_daemon_or_provider_secret(tmp_path,monkeypatch,change):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('agent',IMAGE)
    options={'mounts':[(str(tmp_path),'/var/run/docker.sock','ro')]} if change=='socket-mount' else (
        {'mounts':[('relative','/runtime','ro')]} if change=='relative-mount' else
        {'environment':{'openai_api_key' if change=='lowercase-credential' else 'OPENAI_API_KEY':'synthetic'}})
    with pytest.raises(ValueError):owner.create(**options)
    assert not any(call[0]=='create' for call in docker.calls)


def test_recovery_refuses_a_live_worker_then_cleans_containers_before_network(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('verifier',IMAGE);owner.create()
    monkeypatch.setattr(resources.processes,'alive',lambda *args:True)
    with pytest.raises(ValueError,match='still alive'):resources.recover(owner.path,'fixture-run')
    monkeypatch.setattr(resources.processes,'alive',lambda *args:False)
    network=registry.folder/'resources-milestone-network.json'
    with pytest.raises(RuntimeError,match='attached'):resources.recover(network,'fixture-run')
    resources.recover(owner.path,'fixture-run');resources.recover(network,'fixture-run')
    assert not docker.containers and not docker.networks
    assert json.loads(owner.path.read_text(encoding='utf-8'))['cleaned'] and json.loads(network.read_text(encoding='utf-8'))['cleaned']


def test_recovery_does_not_accept_a_foreign_label_or_manually_renamed_journal(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('agent',IMAGE);owner.create()
    monkeypatch.setattr(resources.processes,'alive',lambda *args:False)
    with pytest.raises(ValueError,match='another run'):resources.recover(owner.path,'foreign')
    renamed=registry.folder/'unbound.json';renamed.write_bytes(owner.path.read_bytes())
    with pytest.raises(ValueError,match='container owner'):resources.recover(renamed,'fixture-run')
    assert docker.containers and not any(call[0]=='rm' for call in docker.calls)


def test_agent_can_stop_for_trace_collection_before_container_and_relay_removal(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('agent',IMAGE);owner.create()
    owner.record.update(private_initialized=True,credentials_may_exist=True);owner.persist()
    owner.stop_agent()
    assert docker.containers and not owner.record['cleaned'] and not owner.record['credentials_may_exist']
    assert any('/ctxpress-private/agent.json' in call for call in docker.calls)
    assert not any('/ctxpress-private/relay-process.json' in call for call in docker.calls)
    owner.cleanup()
    assert owner.record['cleaned'] and any('/ctxpress-private/relay-process.json' in call for call in docker.calls)


def test_auth_refresh_refuses_a_replaced_private_home_before_any_upload(tmp_path,monkeypatch):
    registry,docker=fixture(tmp_path,monkeypatch);owner=registry.owner('agent',IMAGE);owner.create()
    owner.record['private_initialized']=True;owner.persist();source=tmp_path/'auth';source.write_text('synthetic', encoding='utf-8')
    def checked(*args):
        if args[0]=='exec' and 'test -d /home/fakeroot/.codex' in args[-1]:raise RuntimeError('private home changed')
        return docker(*args)
    monkeypatch.setattr(resources,'docker',checked)
    monkeypatch.setattr(resources.subprocess,'run',lambda *args,**kwargs:pytest.fail('auth upload must not start'))
    with pytest.raises(RuntimeError,match='home changed'):owner.credentials(source)
    assert owner.record['credentials_may_exist']
    owner.cleanup();assert owner.record['cleaned']
