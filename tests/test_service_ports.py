import json
from types import SimpleNamespace
import pytest
from ctxpress.harness.runtime import service_ports


def fixture():
    owner=SimpleNamespace(record={'container':'verifier','role':'verifier'},inspect=lambda:{'HostConfig':{'NetworkMode':'host'}})
    def inspect(kind,name):
        if (kind,name)!=('networks','owned'):raise ValueError('foreign network')
    scope=SimpleNamespace(record={'verifier':'verifier'},registry=SimpleNamespace(owners=[owner]),inspect=inspect,persist=lambda:None)
    data={'Id':'service','State':{'Running':True},'HostConfig':{'PortBindings':{'8008/tcp':[{'HostPort':'0'}]}},
          'NetworkSettings':{'Ports':{'8008/tcp':None},'Networks':{'owned':{'IPAddress':'172.20.0.2'}}}}
    return scope,data


def test_missing_publication_creates_one_owned_loopback_relay(monkeypatch):
    scope,data=fixture();calls=[]
    def docker(*args):
        calls.append(args);return json.dumps({'port':41001,'pid':123,'token':args[6]})
    monkeypatch.setattr(service_ports,'docker',docker)
    actual=service_ports.publish(scope,data)
    assert actual['NetworkSettings']['Ports']['8008/tcp']==[{'HostIp':'127.0.0.1','HostPort':'41001'}]
    assert calls[0][:3]==('exec','verifier','python3') and calls[0][-3:]==('172.20.0.2','8008','0')
    data['NetworkSettings']['Ports']['8008/tcp']=None
    service_ports.publish(scope,data);assert len(calls)==1
    service_ports.stop(scope,'service');assert calls[-1][5]=='stop'
    assert not scope.record['port_relays']


@pytest.mark.parametrize('change',['foreign','public','ambiguous','udp','wrong-verifier'])
def test_invalid_relay_routes_cannot_start_process(monkeypatch,change):
    scope,data=fixture()
    if change=='foreign':data['NetworkSettings']['Networks']={'foreign':{'IPAddress':'172.20.0.2'}}
    if change=='public':data['NetworkSettings']['Networks']['owned']['IPAddress']='8.8.8.8'
    if change=='ambiguous':data['NetworkSettings']['Networks']={}
    if change=='udp':data['HostConfig']['PortBindings']={'8008/udp':[{'HostPort':'0'}]}
    if change=='wrong-verifier':scope.record['verifier']='other'
    monkeypatch.setattr(service_ports,'docker',lambda *a:pytest.fail('unexpected process'))
    with pytest.raises(ValueError):service_ports.publish(scope,data)


def test_existing_publication_is_unchanged(monkeypatch):
    scope,data=fixture();data['NetworkSettings']['Ports']['8008/tcp']=[{'HostIp':'127.0.0.1','HostPort':'12345'}]
    monkeypatch.setattr(service_ports,'docker',lambda *a:pytest.fail('unexpected process'))
    assert service_ports.publish(scope,data)==data


def test_two_owned_networks_use_declared_primary_route(monkeypatch):
    scope,data=fixture()
    scope.inspect=lambda kind,name: None
    data['HostConfig']['NetworkMode']='owned'
    data['NetworkSettings']['Networks']['secondary']={'IPAddress':'172.21.0.2'}
    calls=[]
    def docker(*args):
        calls.append(args)
        return json.dumps({'port':41001,'pid':123,'token':args[6]})
    monkeypatch.setattr(service_ports,'docker',docker)
    service_ports.publish(scope,data)
    assert calls[0][-3:]==('172.20.0.2','8008','0')


def test_multiple_routes_without_declared_primary_fail_closed(monkeypatch):
    scope,data=fixture();scope.inspect=lambda kind,name: None
    data['NetworkSettings']['Networks']['secondary']={'IPAddress':'172.21.0.2'}
    monkeypatch.setattr(service_ports,'docker',lambda *a:pytest.fail('unexpected process'))
    with pytest.raises(ValueError,match='unambiguous'):service_ports.publish(scope,data)
