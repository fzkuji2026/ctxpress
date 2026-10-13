"""Loopback TCP publication for owned services on Docker internal bridges.

Relays live inside the verifier PID namespace, so verifier cleanup/recovery
also destroys their sockets. Never proxy a client-supplied host address.
"""
import ipaddress,json,uuid
from ctxpress.harness.runtime.docker import docker

PROGRAM = r'''
import json,os,select,signal,socket,sys,threading
mode,token=sys.argv[1:3]
if mode=='stop':
 pid=int(sys.argv[3])
 try:
  command=open('/proc/%d/cmdline'%pid,'rb').read().split(b'\0')
  if token.encode() not in command:raise RuntimeError('relay identity changed')
  os.kill(pid,signal.SIGTERM)
 except FileNotFoundError:pass
 sys.exit(0)
ip,remote,requested=sys.argv[3],int(sys.argv[4]),int(sys.argv[5])
listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
listener.bind(('127.0.0.1',requested));listener.listen(32)
port=listener.getsockname()[1];pid=os.fork()
if pid:
 print(json.dumps(dict(pid=pid,port=port,token=token)),flush=True);sys.exit(0)
os.setsid()
fd=os.open('/dev/null',os.O_RDWR)
for dest in (0,1,2):os.dup2(fd,dest)
if fd>2:os.close(fd)
def forward(client):
 peer=None
 try:
  peer=socket.create_connection((ip,remote),timeout=10)
  client.settimeout(30);peer.settimeout(30)
  while True:
   ready,_,_=select.select([client,peer],[],[],60)
   if not ready:break
   for source in ready:
    data=source.recv(65536)
    if not data:return
    (peer if source is client else client).sendall(data)
 except OSError:pass
 finally:
  client.close()
  if peer:peer.close()
while True:
 client,_=listener.accept()
 threading.Thread(target=forward,args=(client,),daemon=True).start()
'''


def verifier(scope):
    name=scope.record.get('verifier')
    owners=[o for o in getattr(scope.registry,'owners',[]) if o.record['container']==name and o.record['role']=='verifier']
    if len(owners)!=1:raise ValueError('port relay needs its owned verifier')
    row=owners[0].inspect()
    if row.get('HostConfig',{}).get('NetworkMode')!='host':raise ValueError('port relay verifier namespace changed')
    return name


def publish(scope,inspection):
    """Fill only missing Docker port mappings, preserving real publications."""
    if not scope.record.get('verifier') or not inspection.get('State',{}).get('Running'):return inspection
    cid=inspection['Id'];bindings=inspection.get('HostConfig',{}).get('PortBindings') or {}
    ports=inspection.setdefault('NetworkSettings',{}).setdefault('Ports',{})
    needed={key:value for key,value in bindings.items() if value and not ports.get(key)}
    if not needed:return inspection
    networks=inspection['NetworkSettings'].get('Networks') or {}
    addresses={}
    for name,endpoint in networks.items():
        scope.inspect('networks',name)
        ip=endpoint.get('IPAddress')
        if ip:
            address=ipaddress.ip_address(ip)
            if address.version!=4 or not address.is_private:raise ValueError('invalid owned service address')
            addresses[name]=str(address)
    # Testcontainers may connect a custom network after container start.
    # All endpoints must still be owned; use Docker's explicit primary route.
    target=addresses.get(inspection.get('HostConfig',{}).get('NetworkMode'))
    if target is None:
        if len(set(addresses.values()))!=1:raise ValueError('service relay needs one unambiguous owned IPv4 endpoint')
        target=next(iter(addresses.values()))
    owner=verifier(scope);relays=scope.record.setdefault('port_relays',{}).setdefault(cid,{})
    for port,requested in needed.items():
        number,protocol=port.split('/')
        if protocol!='tcp' or len(requested)!=1:raise ValueError('internal service relay supports one TCP binding per port')
        if port not in relays:
            token='ctxp-relay-'+uuid.uuid4().hex
            result=json.loads(docker('exec',owner,'python3','-c',PROGRAM,'start',token,target,number,requested[0].get('HostPort') or '0'))
            if result.get('token')!=token or not 1<=result['port']<=65535:raise ValueError('invalid owned relay identity')
            relays[port]=result;scope.persist()
        ports[port]=[{'HostIp':'127.0.0.1','HostPort':str(relays[port]['port'])}]
    return inspection


def stop(scope,cid):
    relays=scope.record.get('port_relays',{}).get(cid,{})
    if not relays:return
    owner=verifier(scope)
    for row in relays.values():docker('exec',owner,'python3','-c',PROGRAM,'stop',row['token'],str(row['pid']))
    del scope.record['port_relays'][cid];scope.persist()
