"""Prepared service ownership, interruption recovery and API isolation without Docker."""
import copy,json,threading
from types import SimpleNamespace
from urllib.parse import parse_qs,unquote,urlsplit
import pytest
from ctxpress.harness import service_resources as services

IMAGE='sha256:'+'a'*64
PROJECT='ctxp-ms-'+'1'*24


from milestone_mock_engine import Engine


def scope(tmp_path):
    engine=Engine();registry=SimpleNamespace(project=PROJECT,label='fixture-label',daemon=engine.daemon,folder=tmp_path)
    current=services.Scope(registry,{'database':{'id':IMAGE,'reference':'redis:7'}},engine)
    return current,engine


def create(current,**host):
    payload={'Image':'redis:7','ExposedPorts':{'6379/tcp':{}},'HostConfig':host}
    return current.dispatch('POST','/v1.47/containers/create?name=database',json.dumps(payload).encode()).value()['Id']


def test_created_services_use_pinned_images_owned_labels_and_loopback_ports(tmp_path):
    current,engine=scope(tmp_path);identifier=create(current,PortBindings={'6379/tcp':[{'HostIp':'0.0.0.0','HostPort':''}]})
    row=current.dispatch('GET','/containers/'+identifier+'/json').value()
    assert row['Image']==IMAGE and row['Config']['Labels']==current.owner_labels
    assert row['HostConfig']['NetworkMode'].startswith(current.prefix)
    assert row['HostConfig']['PortBindings']['6379/tcp'][0]['HostIp']=='127.0.0.1'
    network=next(iter(engine.objects['networks'].values()))
    assert network['Internal'] and network['Driver']=='bridge'
    journal=json.loads(current.path.read_text(encoding='utf-8'))
    assert next(iter(journal['objects']['containers'].values()))['id']==identifier
    assert journal['images']['redis:7']==IMAGE and 'Env' not in current.path.read_text(encoding='utf-8')


@pytest.mark.parametrize('config',[{'Privileged':True},{'Binds':['/home:/host']},{'PidMode':'host'},
    {'IpcMode':'host'},{'Devices':[{'PathOnHost':'/dev/gpu'}]},{'CapAdd':['SYS_ADMIN']},
    {'NetworkMode':'host'},{'SecurityOpt':['seccomp=unconfined']}])
def test_host_access_and_foreign_namespaces_are_rejected_before_create(tmp_path,config):
    current,engine=scope(tmp_path)
    with pytest.raises(services.Rejected):create(current,**config)
    assert not engine.objects['containers']


def test_pull_emulation_never_pulls_or_imports_undeclared_images(tmp_path):
    current,engine=scope(tmp_path)
    response=current.dispatch('POST','/images/create?fromImage=docker.io%2Flibrary%2Fredis&tag=7')
    assert response.status==200 and response.value()['id']==IMAGE
    with pytest.raises(services.Rejected,match='not declared'):current.dispatch('POST','/images/create?fromImage=redis&tag=8')
    assert not any(method=='POST' and path=='/images/create' for method,path,_,_ in engine.calls)
    assert current.dispatch('GET','/images/json').value()==[{'Id':IMAGE}]
    assert 'DockerRootDir' not in current.dispatch('GET','/info').value()


@pytest.mark.parametrize('method,path',[('POST','/build'),('POST','/images/load'),('DELETE','/images/redis:7'),
    ('POST','/containers/'+'b'*64+'/start'),('GET','/containers/'+'b'*64+'/json'),
    ('POST','/exec/'+'e'*64+'/start'),('POST','/networks/prune'),('POST','/volumes/prune'),
    ('POST','/plugins/create'),('POST','/auth'),('GET','//containers/json'),('GET','/v1.47/../info'),('GET','/%252finfo')])
def test_api_cannot_reach_foreign_objects_or_host_management(tmp_path,method,path):
    current,engine=scope(tmp_path);before=len(engine.calls)
    with pytest.raises(services.Rejected):current.dispatch(method,path)
    assert not any(call[0] in ('POST','DELETE') for call in engine.calls[before:])


def test_volumes_and_custom_networks_are_owned_and_client_aliases_are_resolved(tmp_path):
    current,engine=scope(tmp_path)
    volume=current.dispatch('POST','/volumes/create',b'{"Name":"data"}').value()['Name']
    network=current.dispatch('POST','/networks/create',b'{"Name":"test-network"}').value()['Id']
    identifier=create(current,NetworkMode='test-network',Mounts=[{'Type':'volume','Source':'data','Target':'/data'}])
    host=current.dispatch('GET','/containers/'+identifier+'/json').value()['HostConfig']
    assert host['Mounts'][0]['Source']==volume
    assert current.inspect('networks',network)[0]==host['NetworkMode']
    current.dispatch('POST','/networks/'+network+'/connect',json.dumps({'Container':identifier}).encode())
    current.cleanup();assert all(not rows for rows in engine.objects.values())
    assert json.loads(current.path.read_text(encoding='utf-8'))['cleaned']


def test_endpoint_links_are_resolved_to_owned_containers_on_create_and_connect(tmp_path):
    current,engine=scope(tmp_path);database=create(current)
    payload={'Image':'redis:7','NetworkingConfig':{'EndpointsConfig':{'bridge':{
        'Aliases':['application'],'Links':['database:store']}}}}
    identifier=current.dispatch('POST','/containers/create',json.dumps(payload).encode()).value()['Id']
    row=current.inspect('containers',identifier)[2].value()
    endpoint=next(iter(row['Config']['NetworkingConfig']['EndpointsConfig'].values()))
    assert endpoint['Links']==[current.inspect('containers',database)[0]+':store'] and endpoint['Aliases']==['application']
    network=current.default_network()
    current.dispatch('POST','/networks/'+network+'/connect',json.dumps({'Container':identifier,
        'EndpointConfig':{'Links':['database:store'],'Aliases':['other-name']}}).encode())
    assert engine.calls[-1][3]['EndpointConfig']['Links']==endpoint['Links']


@pytest.mark.parametrize('endpoint',[{'Links':['foreign:store']},{'Aliases':'store'},
    {'NetworkID':'foreign'},{'GwPriority':1},{'DriverOpts':{'com.docker.network.endpoint.sysctls':'net.ipv4.conf.IFNAME.forwarding=1'}},
    {'IPAMConfig':{'IPv4Address':'192.0.2.1'}},{'Links':['foreign:bad/alias']}])
def test_foreign_or_admin_endpoint_declarations_never_reach_create(tmp_path,endpoint):
    current,engine=scope(tmp_path)
    payload={'Image':'redis:7','NetworkingConfig':{'EndpointsConfig':{'bridge':endpoint}}}
    with pytest.raises(services.Rejected):current.dispatch('POST','/containers/create',json.dumps(payload).encode())
    assert not any(engine.objects.values())


def test_image_anonymous_volumes_are_journaled_even_if_container_create_is_interrupted(tmp_path,monkeypatch):
    current,engine=scope(tmp_path);engine.image_config={'Volumes':{'/data':{}}};engine.interrupt_create=True
    with pytest.raises(OSError):create(current)
    volume=next(iter(engine.objects['volumes'].values()))
    assert volume['Labels']==current.owner_labels and len(current.record['objects']['volumes'])==1
    container=next(iter(engine.objects['containers'].values()))
    assert container['HostConfig']['Mounts'][0]['Source']==volume['Name']
    monkeypatch.setattr(services.processes,'alive',lambda *args:False)
    services.recover(current.path,'fixture-label',engine)
    assert all(not rows for rows in engine.objects.values())


def test_long_log_connection_does_not_hold_the_resource_control_lock(tmp_path):
    current,_=scope(tmp_path);identifier=create(current);entered=threading.Event();release=threading.Event();done=threading.Event()
    def forward(*args):entered.set();assert release.wait(3)
    thread=threading.Thread(target=lambda:current.dispatch('GET','/containers/'+identifier+'/logs?follow=1',forward=forward))
    thread.start()
    try:
        assert entered.wait(1)
        control=threading.Thread(target=lambda:(current.dispatch('GET','/version'),done.set()))
        control.start();assert done.wait(1);control.join(1)
    finally:release.set();thread.join(3)
    assert not thread.is_alive()


def test_exec_is_bound_to_the_owned_service_not_the_native_agent(tmp_path):
    current,engine=scope(tmp_path);identifier=create(current)
    response=current.dispatch('POST','/containers/'+identifier+'/exec',b'{"Cmd":["redis-cli","PING"],"AttachStdout":true}')
    assert response.value()['Id']=='e'*64
    assert current.dispatch('POST','/exec/'+'e'*64+'/start',b'{}').status==200
    with pytest.raises(services.Rejected):current.dispatch('POST','/containers/'+identifier+'/exec',b'{"Privileged":true}')
    current.dispatch('DELETE','/containers/'+identifier)
    with pytest.raises(services.Rejected):current.dispatch('POST','/exec/'+'e'*64+'/start',b'{}')


def test_interrupted_create_is_recovered_by_name_labels_and_image_identity(tmp_path,monkeypatch):
    current,engine=scope(tmp_path);engine.interrupt_create=True
    with pytest.raises(OSError):create(current)
    record=json.loads(current.path.read_text(encoding='utf-8'));assert next(iter(record['objects']['containers'].values()))['id'] is None
    with pytest.raises(services.Rejected,match='still alive'):services.recover(current.path,'fixture-label',engine)
    monkeypatch.setattr(services.processes,'alive',lambda *args:False)
    recovered=services.recover(current.path,'fixture-label',engine)
    assert recovered.record['cleaned'] and all(not rows for rows in engine.objects.values())


@pytest.mark.parametrize('change',['daemon','image','label','name','id','unknown-object'])
def test_changed_ownership_blocks_all_cleanup_mutations(tmp_path,change):
    current,engine=scope(tmp_path);create(current)
    name,row=next(iter(engine.objects['containers'].items()))
    if change=='daemon':engine.daemon='another-daemon'
    if change=='image':row['Image']='sha256:'+'b'*64
    if change=='label':row['Config']['Labels']['ctxpress.run']='another-run'
    if change=='name':row['Name']='/foreign-name'
    if change=='id':row['Id']='b'*64
    if change=='unknown-object':
        clone=copy.deepcopy(row);clone['Name']='/'+current.prefix+'c-'+'b'*16;clone['Id']='b'*64
        engine.objects['containers']['unknown']=clone
    before=len(engine.calls)
    # A changed label removes the object from the label-filtered list; the
    # declared record must still be checked by ID before considering it absent.
    with pytest.raises(services.Rejected):current.cleanup()
    assert not any(method=='DELETE' for method,_,_,_ in engine.calls[before:])


def test_live_verifier_keeps_services_and_mounted_channel_available(tmp_path):
    current,engine=scope(tmp_path);create(current)
    name=PROJECT+'-eval-'+'a'*16;current.record['verifier']=name
    engine.objects['containers'][name]={'Id':'f'*64,'Name':'/'+name,'Config':{'Labels':{}}}
    before=len(engine.calls)
    with pytest.raises(services.Rejected,match='verifier must be removed'):current.cleanup()
    assert not any(method=='DELETE' for method,_,_,_ in engine.calls[before:])
