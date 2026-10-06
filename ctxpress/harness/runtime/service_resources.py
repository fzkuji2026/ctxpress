"""One verifier's prepared Docker services, with durable ownership before mutation.

The API gateway calls this policy for every operation. Neither image pulls nor
host bindings are delegated to a test. Engine scoring and test commands stay
in the author's evaluator; this module only owns auxiliary resources.
"""
from __future__ import annotations
import copy,json,os,re,threading,uuid
from dataclasses import dataclass
from pathlib import Path,PurePosixPath
from urllib.parse import parse_qs,quote,unquote,urlsplit,urlencode
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.core import processes
from ctxpress.benchmarks.milestone.resources import identity, image_id, labels

SCHEMA='ctxpress.eval.milestone_services'
HEX=re.compile(r'[0-9a-f]{64}')


class Rejected(ValueError):
    def __init__(self,message,status=400):super().__init__(message);self.status=status


@dataclass
class Response:
    status:int
    headers:dict
    body:bytes

    @classmethod
    def json(cls,value,status=200):
        return cls(status,{'Content-Type':'application/json'},json.dumps(value).encode())

    def value(self):return json.loads(self.body)


@dataclass
class Forward:
    method:str
    path:str
    body:bytes


def image_reference(value):
    if not isinstance(value,str) or not value or any(char.isspace() for char in value) or '%' in value:
        raise Rejected('invalid prepared service image reference')
    if re.fullmatch(r'sha256:[0-9a-f]{64}',value):return value
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@+-]*',value):raise Rejected('invalid prepared service image reference')
    value=value.removeprefix('docker.io/').removeprefix('index.docker.io/').removeprefix('library/')
    if '@' not in value and ':' not in value.rsplit('/',1)[-1]:value+=':latest'
    return value


def selected_images(images):
    if not isinstance(images,dict) or not images:raise Rejected('declare prepared images for native testcontainers services')
    result={}
    for item in images.values():
        if not isinstance(item,dict) or not isinstance(item.get('reference'),str):raise Rejected('service images require captured references')
        ref=image_reference(item['reference']);identifier=image_id(item.get('id'))
        if ref in result and result[ref]!=identifier:raise Rejected('conflicting prepared service image references')
        result[ref]=identifier;result[identifier]=identifier
    return result


def request_target(raw):
    parsed=urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith('/') or parsed.path.startswith('//'):
        raise Rejected('invalid scoped Docker API target')
    path=unquote(parsed.path,errors='strict')
    if '%' in path or '\\' in path or '\x00' in path or any(part in ('.','..') for part in path.split('/')):
        raise Rejected('invalid scoped Docker API path')
    match=re.match(r'^(/v1\.\d+)(/.*)$',path)
    prefix,path=match.groups() if match else ('',path)
    query=parse_qs(parsed.query,keep_blank_values=True)
    if any(len(values)!=1 for values in query.values()):raise Rejected('duplicate Docker query parameter')
    return prefix,path,{key:values[0] for key,values in query.items()}


def url(path,query=None):return path+('?' + urlencode(query) if query else '')


class Scope:
    def __init__(self,registry,images,backend):
        self.registry,self.backend=registry,backend;self.lock=threading.RLock();self.execs={}
        token=uuid.uuid4().hex[:24]
        self.path=registry.folder/('resources-milestone-services-'+token+'.json')
        if self.path.exists():raise Rejected('native service scope already exists')
        self.record=dict(schema=SCHEMA,version=1,project=registry.project,label=registry.label,scope=token,
            daemon_id=registry.daemon,pid=os.getpid(),identity=processes.identity(os.getpid()),
            images=selected_images(images),objects={kind:{} for kind in ('containers','networks','volumes')},
            channel=None,verifier=None,cleaned=False,phase='prepared',rejections=0)
        self.closed=False;self.persist();self.verify_daemon()

    @property
    def prefix(self):return self.record['project']+'-svc-'+self.record['scope']+'-'

    @property
    def owner_labels(self):
        return dict(labels(self.record['project'],self.record['label'],'service'),
            **{'ctxpress.milestone.service_scope':self.record['scope']})

    def persist(self):eval_plan.atomic_json(self.path,self.record)

    def verify_daemon(self):
        if self.backend.request('GET','/info').value().get('ID')!=self.record['daemon_id']:
            raise Rejected('native service Docker daemon changed')

    def json(self,method,path,value=None):
        response=self.backend.request(method,path,b'' if value is None else json.dumps(value).encode())
        if response.status>=400:raise Rejected('owned Docker operation failed',response.status)
        return response

    def labelled(self,value):
        value=dict(value or {})
        if any(not isinstance(key,str) or not isinstance(item,str) or key.startswith('ctxpress.') for key,item in value.items()):
            raise Rejected('service labels must not replace resource ownership')
        value.update(self.owner_labels);return value

    def plan_object(self,kind,client_name='',image=None):
        if client_name and (not isinstance(client_name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',client_name)):
            raise Rejected('invalid service resource name')
        objects=self.record['objects'][kind]
        if len(objects)>=256:raise Rejected('service scope resource limit reached',429)
        if client_name and any(row['client_name']==client_name and not row.get('removed') for row in objects.values()):
            raise Rejected('service resource name already exists',409)
        name=self.prefix+kind[0]+'-'+uuid.uuid4().hex[:16]
        objects[name]=dict(client_name=client_name,id=None,phase='creating',removed=False)
        if image is not None:objects[name]['image']=image
        self.persist();return name

    def inspect(self,kind,reference):
        candidates=[(name,row) for name,row in self.record['objects'][kind].items()
            if reference in (name,row['client_name'],row.get('id')) and not row['removed']]
        if len(candidates)!=1:raise Rejected('resource does not belong to this verifier',404)
        name,row=candidates[0]
        suffix='/json' if kind=='containers' else ''
        response=self.json('GET','/'+kind+'/'+quote(row.get('id') or name,safe='')+suffix)
        actual=response.value();config=actual.get('Config',{}) if kind=='containers' else actual
        if (any((config.get('Labels') or {}).get(key)!=value for key,value in self.owner_labels.items()) or
                (actual.get('Name','').lstrip('/') if kind=='containers' else actual.get('Name'))!=name or
                row.get('id') and (actual.get('Id') if kind!='volumes' else actual.get('Name'))!=row['id'] or
                kind=='containers' and actual.get('Image')!=row['image'] or
                kind=='networks' and (actual.get('Internal') is not True or actual.get('Driver')!='bridge')):
            raise Rejected('service resource identity changed')
        return name,row,response

    def create_network(self,value,client_name=None):
        if not isinstance(value,dict):raise Rejected('invalid service network declaration')
        value=copy.deepcopy(value)
        allowed={'Name','CheckDuplicate','Driver','Internal','Attachable','Ingress','IPAM','EnableIPv6','Options','Labels','Scope','ConfigOnly','ConfigFrom'}
        if set(value)-allowed or value.get('Driver') not in (None,'','bridge') or any(value.get(key) for key in ('Ingress','IPAM','Options','ConfigOnly','ConfigFrom','EnableIPv6')):
            raise Rejected('service networks require the owned internal bridge provider')
        if value.get('Scope') not in (None,'','local'):raise Rejected('service network scope must be local')
        value['Labels']=self.labelled(value.get('Labels'))
        name=self.plan_object('networks',value.get('Name','') if client_name is None else client_name)
        value.update(Name=name,Driver='bridge',Internal=True)
        response=self.json('POST','/networks/create',value);identifier=response.value().get('Id')
        if not isinstance(identifier,str) or not HEX.fullmatch(identifier):raise Rejected('service network returned an invalid identity')
        self.record['objects']['networks'][name].update(id=identifier,phase='created');self.persist()
        self.inspect('networks',name);return response

    def default_network(self):
        for name,row in self.record['objects']['networks'].items():
            if row['client_name']=='bridge' and not row['removed']:
                self.inspect('networks',name);return name
        self.create_network({},'bridge')
        return next(name for name,row in self.record['objects']['networks'].items() if row['client_name']=='bridge' and not row['removed'])

    def create_volume(self,value):
        if not isinstance(value,dict) or set(value)-{'Name','Driver','DriverOpts','Labels'} or value.get('Driver') not in (None,'','local') or value.get('DriverOpts'):
            raise Rejected('service volumes require private local storage')
        value=copy.deepcopy(value);value['Labels']=self.labelled(value.get('Labels'))
        name=self.plan_object('volumes',value.get('Name',''));value.update(Name=name,Driver='local')
        response=self.json('POST','/volumes/create',value)
        if response.value().get('Name')!=name:raise Rejected('service volume returned another name')
        self.record['objects']['volumes'][name].update(id=name,phase='created');self.persist();self.inspect('volumes',name)
        return response

    def prepared_image(self,reference):
        identifier=self.record['images'].get(image_reference(reference))
        if identifier is None:raise Rejected('service image was not declared and captured locally',404)
        image=self.json('GET','/images/'+quote(identifier,safe='')+'/json').value()
        if image.get('Id')!=identifier or image.get('Os')!='linux':raise Rejected('prepared Linux service image is unavailable or changed')
        return identifier

    def endpoint_config(self,value):
        if value is None:value={}
        # Docker CLI serializes inspect-only endpoint fields with their zero
        # values on create. Discard those defaults; supplied addresses/IDs must
        # still never select a foreign endpoint or change bridge routing.
        defaults={'NetworkID','EndpointID','Gateway','IPAddress','IPPrefixLen','IPv6Gateway',
                  'GlobalIPv6Address','GlobalIPv6PrefixLen','DNSNames','GwPriority'}
        if not isinstance(value,dict) or set(value)-{'Aliases','Links','IPAMConfig','DriverOpts','MacAddress'}-defaults:
            raise Rejected('unsupported service network endpoint declaration')
        if any(value.get(key) for key in defaults):
            raise Rejected('service endpoint IDs, addresses and routing must be assigned by their owned bridge')
        if any(value.get(key) for key in ('IPAMConfig','DriverOpts','MacAddress')):
            raise Rejected('service endpoints require addresses assigned by their owned bridge')
        value={key:copy.deepcopy(item) for key,item in value.items() if key not in defaults}
        aliases=value.get('Aliases') or []
        if not isinstance(aliases,list) or any(not isinstance(alias,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',alias) for alias in aliases):
            raise Rejected('invalid service network aliases')
        links=value.get('Links') or []
        if not isinstance(links,list):raise Rejected('invalid service network links')
        mapped=[]
        for link in links:
            if not isinstance(link,str):raise Rejected('invalid service network link')
            reference,separator,alias=link.partition(':')
            if separator and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',alias):raise Rejected('invalid service link alias')
            name=self.inspect('containers',reference)[0]
            mapped.append(name+(':'+alias if separator else ''))
        value.update(Aliases=aliases,Links=mapped)
        return value

    def container_config(self,value):
        if not isinstance(value,dict):raise Rejected('invalid service container configuration')
        value=copy.deepcopy(value);identifier=self.prepared_image(value.get('Image'));value['Image']=identifier
        value['Labels']=self.labelled(value.get('Labels'))
        config_keys={'Hostname','Domainname','User','AttachStdin','AttachStdout','AttachStderr','ExposedPorts','Tty','OpenStdin','StdinOnce',
            'Env','Cmd','Healthcheck','ArgsEscaped','Image','Volumes','WorkingDir','Entrypoint','NetworkDisabled','MacAddress',
            'OnBuild','Labels','StopSignal','StopTimeout','Shell','HostConfig','NetworkingConfig'}
        if set(value)-config_keys:raise Rejected('unsupported service container configuration')
        host=value.get('HostConfig') or {}
        if not isinstance(host,dict):raise Rejected('invalid service host configuration')
        host_keys={'NetworkMode','PortBindings','PublishAllPorts','Mounts','VolumesFrom','AutoRemove','RestartPolicy',
            'Init','Memory','MemoryReservation','MemorySwap','NanoCpus','CpuPeriod','CpuQuota','CpuShares','ShmSize','OomKillDisable',
            'LogConfig','ReadonlyRootfs','Tmpfs','Ulimits','Dns','DnsOptions','DnsSearch','CapDrop','ConsoleSize','MemorySwappiness'}
        console=host.get('ConsoleSize')
        if console is not None and (not isinstance(console,list) or len(console)!=2 or any(type(value) is not int or value<0 for value in console)):
            raise Rejected('invalid service console dimensions')
        swappiness=host.get('MemorySwappiness')
        if swappiness is not None and (type(swappiness) is not int or not -1<=swappiness<=100):raise Rejected('invalid service memory swappiness')
        blocked=sorted(key for key,item in host.items() if item and key not in host_keys)
        if blocked:
            raise Rejected('service containers cannot request host bindings, privileges or foreign namespaces: '+', '.join(blocked))
        host={key:item for key,item in host.items() if key in host_keys}
        host['AutoRemove']=False;host['RestartPolicy']={'Name':'no'}
        networking=value.get('NetworkingConfig') or {}
        if not isinstance(networking,dict) or set(networking)-{'EndpointsConfig'}:
            raise Rejected('invalid service networking configuration')
        networks=networking.get('EndpointsConfig') or {}
        if not isinstance(networks,dict) or any(not isinstance(name,str) or not name for name in networks):
            raise Rejected('invalid service network endpoints')
        networks={name:self.endpoint_config(endpoint) for name,endpoint in networks.items()}
        mode=host.get('NetworkMode') or 'bridge'
        if mode in ('bridge','default'):mode=self.default_network()
        elif mode!='none':mode=self.inspect('networks',mode)[0]
        host['NetworkMode']=mode
        ports=host.get('PortBindings') or {}
        if not isinstance(ports,dict):raise Rejected('invalid service port bindings')
        if host.get('PublishAllPorts'):
            for port in value.get('ExposedPorts') or {}:ports.setdefault(port,[{'HostPort':''}])
        for port,bindings in ports.items():
            if not re.fullmatch(r'\d+/(tcp|udp)',port) or not 1<=int(port.split('/')[0])<=65535 or not isinstance(bindings,list):
                raise Rejected('invalid service port declaration')
            for binding in bindings:
                if (not isinstance(binding,dict) or set(binding)-{'HostIp','HostPort'} or binding.get('HostIp') not in (None,'','0.0.0.0','127.0.0.1') or
                        not isinstance(binding.get('HostPort',''),str) or binding.get('HostPort','') and
                        (not binding['HostPort'].isdigit() or not 1<=int(binding['HostPort'])<=65535)):
                    raise Rejected('invalid service host port')
                binding['HostIp']='127.0.0.1'
        host.update(PublishAllPorts=False,PortBindings=ports)
        for mount in host.get('Mounts') or []:
            if (not isinstance(mount,dict) or set(mount)-{'Type','Source','Target','ReadOnly','VolumeOptions','Consistency'} or
                    mount.get('Type')!='volume' or mount.get('VolumeOptions') or mount.get('Consistency') not in (None,'','default')):
                raise Rejected('service mounts require previously owned volumes')
            path=PurePosixPath(mount.get('Target',''))
            if not path.is_absolute() or '..' in path.parts:raise Rejected('invalid service volume target')
            mount['Source']=self.inspect('volumes',mount.get('Source'))[0]
        host['VolumesFrom']=[self.inspect('containers',ref.split(':',1)[0])[0]+(':ro' if ref.endswith(':ro') else ':rw')
            for ref in host.get('VolumesFrom') or []]
        if networks:
            value['NetworkingConfig']={'EndpointsConfig':{
                (self.default_network() if name in ('bridge','default') else self.inspect('networks',name)[0]):endpoint for name,endpoint in networks.items()}}
        # Image VOLUME instructions also create storage. Give every otherwise
        # anonymous mount a journaled, labelled volume before container create,
        # so a failed create cannot leave unattributed storage behind.
        image=self.json('GET','/images/'+quote(identifier,safe='')+'/json').value()
        volumes=dict((image.get('Config') or {}).get('Volumes') or {})
        volumes.update(value.get('Volumes') or {})
        mounts=host.get('Mounts') or [];host['Mounts']=mounts
        occupied={mount['Target'] for mount in mounts}
        for target in volumes:
            path=PurePosixPath(target)
            if not path.is_absolute() or '..' in path.parts:raise Rejected('invalid image service volume target')
            if target in occupied:continue
            created=self.create_volume({}).value()['Name']
            mounts.append({'Type':'volume','Source':created,'Target':target,'ReadOnly':False})
        value['HostConfig']=host;return value

    def create_container(self,value,query):
        if set(query)-{'name','platform'}:raise Rejected('unsupported service create query')
        value=self.container_config(value);name=self.plan_object('containers',query.get('name',''),value['Image'])
        response=self.json('POST',url('/containers/create',dict(query,name=name)),value)
        identifier=response.value().get('Id')
        if not isinstance(identifier,str) or not HEX.fullmatch(identifier):raise Rejected('service container returned an invalid identity')
        self.record['objects']['containers'][name].update(id=identifier,phase='created');self.persist()
        self.inspect('containers',identifier);return response

    def filters(self,query):
        query=dict(query)
        try:filters=json.loads(query.get('filters','{}'))
        except ValueError:raise Rejected('invalid service resource filters') from None
        if not isinstance(filters,dict):raise Rejected('invalid service resource filters')
        chosen=filters.get('label') or []
        if isinstance(chosen,dict):chosen=[key for key,value in chosen.items() if value]
        if not isinstance(chosen,list) or any(not isinstance(value,str) for value in chosen):raise Rejected('invalid service label filters')
        filters['label']=chosen+[key+'='+value for key,value in self.owner_labels.items()]
        query['filters']=json.dumps(filters,separators=(',',':'));return query

    def dispatch(self,method,raw,body=b'',forward=None):
        """Authorize a request, then buffer control replies or forward owned streams."""
        with self.lock:
            if self.closed or self.record['cleaned']:raise Rejected('service scope is closed',503)
            self.verify_daemon()
            try:result=self._dispatch(method,raw,body,Forward)
            except Rejected:
                self.record['rejections']+=1;self.persist();raise
        # Streaming log/exec/event connections cannot hold the control lock:
        # clients need parallel creates/starts while following readiness logs.
        if isinstance(result,Forward):return (forward or self.backend.request)(result.method,result.path,result.body)
        return result

    def _dispatch(self,method,raw,body,forward):
        prefix,path,query=request_target(raw)
        def value():
            try:return json.loads(body or b'{}')
            except ValueError:raise Rejected('invalid Docker JSON request') from None
        if path in ('/_ping','/version') and method in ('GET','HEAD'):return self.backend.request(method,path)
        if path=='/info' and method=='GET':
            data=self.json('GET','/info').value()
            return Response.json({key:data[key] for key in ('ID','OSType','Architecture','OperatingSystem','ServerVersion','NCPU','MemTotal') if key in data})
        if path=='/containers/create' and method=='POST':return self.create_container(value(),query)
        if path=='/networks/create' and method=='POST':return self.create_network(value())
        if path=='/volumes/create' and method=='POST':return self.create_volume(value())
        if path in ('/containers/json','/networks','/volumes','/events') and method=='GET':
            return forward('GET',url(prefix+path,self.filters(query)),b'')
        if path=='/images/create' and method=='POST':
            if set(query)-{'fromImage','tag','platform'}:raise Rejected('service image builds/imports are unavailable')
            reference=query.get('fromImage','')
            if query.get('tag'):reference+=':'+query['tag']
            identifier=self.prepared_image(reference)
            return Response(200,{'Content-Type':'application/json'},json.dumps({'status':'Already exists','id':identifier}).encode()+b'\n')
        if path=='/images/json' and method=='GET':
            data=self.json('GET',url(prefix+path,query)).value()
            return Response.json([row for row in data if row.get('Id') in self.record['images'].values()])
        match=re.fullmatch(r'/images/(.+)/json',path)
        if match and method=='GET':return self.backend.request('GET',prefix+'/images/'+quote(self.prepared_image(match[1]),safe='')+'/json')
        match=re.fullmatch(r'/(containers|networks|volumes)/([^/]+)(/.*)?',path)
        if match:
            kind,reference,action=match.groups();action=action or ''
            name,row,inspection=self.inspect(kind,reference)
            target=prefix+'/'+kind+'/'+quote(row.get('id') or name,safe='')+action
            if method=='GET' and action==('/json' if kind=='containers' else ''):return inspection
            if method=='DELETE' and not action:
                response=self.backend.request('DELETE',url(target,{'force':'1','v':'1'} if kind=='containers' else {}))
                if response.status<400:row.update(removed=True,phase='removed');self.persist()
                return response
            if kind=='containers':
                if method=='POST' and action=='/exec':
                    payload=value()
                    if not isinstance(payload,dict) or payload.get('Privileged') or set(payload)-{'AttachStdin','AttachStdout','AttachStderr','DetachKeys','Tty','Env','Cmd','Privileged','User','WorkingDir','ConsoleSize'}:
                        raise Rejected('invalid owned service exec')
                    response=self.json('POST',target,payload);identifier=response.value().get('Id')
                    if not isinstance(identifier,str) or not HEX.fullmatch(identifier):raise Rejected('invalid service exec identity')
                    self.execs[identifier]=row['id'];return response
                allowed={'POST':{'/start','/stop','/restart','/kill','/wait','/resize','/attach'},
                    'GET':{'/logs','/archive','/stats','/top','/attach/ws'},'HEAD':{'/archive'},'PUT':{'/archive'}}
                if action in allowed.get(method,set()):return forward(method,url(target,query),body)
            if kind=='networks' and method=='POST' and action in ('/connect','/disconnect'):
                payload=value()
                if not isinstance(payload,dict) or set(payload)-{'Container','EndpointConfig','Force'}:raise Rejected('invalid owned service network operation')
                payload['Container']=self.inspect('containers',payload.get('Container'))[1]['id']
                if 'EndpointConfig' in payload:payload['EndpointConfig']=self.endpoint_config(payload['EndpointConfig'])
                return self.json('POST',target,payload)
        match=re.fullmatch(r'/exec/([0-9a-f]{64})/(json|start|resize)',path)
        if match:
            identifier,action=match.groups()
            if identifier not in self.execs:raise Rejected('exec does not belong to this verifier',404)
            self.inspect('containers',self.execs[identifier])
            if (method,action) in (('GET','json'),('POST','start'),('POST','resize')):return forward(method,url(prefix+path,query),body)
        raise Rejected('Docker operation is unavailable in the prepared service scope',403)

    def cleanup(self):
        """Stop accepting work, inspect every object, then remove owned resources."""
        with self.lock:
            self.closed=True;self.verify_daemon()
            self.verifier_removed()
            if self.record['cleaned']:return
            for kind,objects in self.record['objects'].items():
                for name,row in objects.items():
                    if row['removed']:continue
                    try:self.inspect(kind,name)
                    except Rejected as error:
                        if error.status!=404:raise
            # Inspect declarations independently of the filtered lists so an
            # altered ownership label cannot make a live container disappear.
            # Lists find a successful create whose response/journal update was
            # interrupted. Unknown names/IDs or altered labels block mutation.
            observed={}
            for kind,endpoint in (('containers','/containers/json'),('networks','/networks'),('volumes','/volumes')):
                result=self.json('GET',url(endpoint,self.filters({'all':'1'} if kind=='containers' else {}))).value()
                rows=result.get('Volumes') or [] if kind=='volumes' else result
                observed[kind]=[]
                for actual in rows:
                    ref=actual.get('Id') if kind!='volumes' else actual.get('Name')
                    if kind!='volumes' and (not isinstance(ref,str) or not HEX.fullmatch(ref)):
                        raise Rejected('invalid service resource response identity')
                    if kind=='containers':
                        names=actual.get('Names') or []
                        if len(names)!=1:raise Rejected('ambiguous owned service name')
                        name=names[0].lstrip('/')
                    else:name=actual.get('Name')
                    if name not in self.record['objects'][kind]:raise Rejected('unknown service resource in this scope')
                    row=self.record['objects'][kind][name]
                    if row['removed'] or row.get('id') and row['id']!=ref:raise Rejected('owned service resource identity changed')
                    if row.get('id') is None:row['id']=ref
                    self.inspect(kind,name);observed[kind].append((name,row))
            self.persist()
            for kind in ('containers','networks','volumes'):
                for name,row in observed[kind]:
                    self.inspect(kind,name)
                    query={'force':'1','v':'1'} if kind=='containers' else {}
                    self.json('DELETE',url('/'+kind+'/'+quote(row['id'] or name,safe=''),query))
                    row.update(removed=True,phase='removed');self.persist()
            for kind,endpoint in (('containers','/containers/json'),('networks','/networks'),('volumes','/volumes')):
                result=self.json('GET',url(endpoint,self.filters({'all':'1'} if kind=='containers' else {}))).value()
                if (result.get('Volumes') or [] if kind=='volumes' else result):raise Rejected('service resource removal did not complete')
            for rows in self.record['objects'].values():
                for row in rows.values():row.update(removed=True,phase='removed')
            self.record.update(cleaned=True,phase='stopped');self.persist()

    def verifier_removed(self):
        name=self.record.get('verifier')
        if name is None:return
        if not isinstance(name,str) or not re.fullmatch(re.escape(self.record['project'])+r'-eval-[0-9a-f]{16}',name):
            raise Rejected('invalid service scope verifier identity')
        response=self.json('GET',url('/containers/json',{'all':'1','filters':json.dumps({'name':['^/'+name+'$']})}))
        if response.value():raise Rejected('service verifier must be removed before its services/channel')


def recover(path,label,backend):
    path=Path(path)
    if path.is_symlink():raise Rejected('service recovery journal cannot be a symbolic link')
    record=json.loads(path.read_text(encoding='utf-8'));identity(record.get('project',''),record.get('label'))
    if (record.get('schema')!=SCHEMA or type(record.get('version')) is not int or record['version']!=1 or record.get('label')!=label or
            not isinstance(record.get('scope'),str) or not re.fullmatch(r'[0-9a-f]{24}',record['scope']) or
            path.name!='resources-milestone-services-'+record['scope']+'.json' or not isinstance(record.get('daemon_id'),str) or not record['daemon_id'] or
            type(record.get('cleaned')) is not bool or type(record.get('pid')) is not int or record['pid']<=0 or
            not isinstance(record.get('objects'),dict) or set(record['objects'])!={'containers','networks','volumes'} or
            not isinstance(record.get('images'),dict) or not record['images']):raise Rejected('invalid service recovery declaration')
    if not record['cleaned'] and processes.alive(record['pid'],record.get('identity')):raise Rejected('service worker is still alive')
    for ref,identifier in record['images'].items():image_reference(ref);image_id(identifier)
    prefix=record['project']+'-svc-'+record['scope']+'-'
    for kind,rows in record['objects'].items():
        if not isinstance(rows,dict):raise Rejected('invalid service recovery objects')
        for name,row in rows.items():
            if (not re.fullmatch(re.escape(prefix)+kind[0]+r'-[0-9a-f]{16}',name) or not isinstance(row,dict) or
                    type(row.get('removed')) is not bool or not isinstance(row.get('client_name'),str) or
                    row.get('id') is not None and (row['id']!=name if kind=='volumes' else not isinstance(row['id'],str) or not HEX.fullmatch(row['id']))):
                raise Rejected('invalid service recovery object identity')
            if kind=='containers' and row.get('image') not in record['images'].values():raise Rejected('undeclared recovery service image')
    scope=object.__new__(Scope);scope.path=path;scope.record=record;scope.backend=backend;scope.closed=True
    scope.lock=threading.RLock();scope.execs={};scope.cleanup();return scope
