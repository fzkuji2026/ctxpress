"""Standard-library fake Engine used only by offline composition checks."""
import contextlib,copy,json,threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs,unquote,urlsplit
from ctxpress.harness.runtime import service_resources as services, socket_bridge

IMAGE='sha256:'+'a'*64
PROJECT='ctxp-ms-'+'1'*24

class Engine:
    def __init__(self):
        self.calls=[];self.objects={kind:{} for kind in ('containers','networks','volumes')}
        self.daemon='fixture-daemon';self.serial=10;self.interrupt_create=False;self.image_config={}
    def request(self,method,raw,body=b''):
        parsed=urlsplit(raw);path=unquote(parsed.path);query={key:value[0] for key,value in parse_qs(parsed.query).items()}
        if path.startswith('/v1.'):path=path[path.index('/',1):]
        value=json.loads(body) if body else None;self.calls.append((method,path,query,value))
        response=services.Response.json
        if path=='/info':return response({'ID':self.daemon,'OSType':'linux','DockerRootDir':'must-not-expose'})
        if path=='/version':return response({'ApiVersion':'1.47'})
        if path=='/_ping':return services.Response(200,{'API-Version':'1.47'},b'OK')
        if path.startswith('/images/'):
            if path=='/images/json':return response([{'Id':IMAGE},{'Id':'sha256:'+'b'*64}])
            return response({'Id':IMAGE,'Os':'linux','Config':copy.deepcopy(self.image_config)})
        if method=='POST' and path in ('/containers/create','/networks/create','/volumes/create'):
            kind=path.split('/')[1];self.serial+=1;identifier=f'{self.serial:064x}'
            name=query['name'] if kind=='containers' else value['Name']
            row={'Id':identifier,'Name':'/'+name if kind=='containers' else name}
            if kind=='containers':row.update(Image=value['Image'],Config=copy.deepcopy(value),HostConfig=copy.deepcopy(value['HostConfig']))
            else:row.update(copy.deepcopy(value))
            self.objects[kind][name]=row
            if self.interrupt_create and kind=='containers':raise OSError('created but reply was interrupted')
            return response({'Name':name} if kind=='volumes' else {'Id':identifier},201)
        if path in ('/containers/json','/networks','/volumes','/events'):
            kind={'/containers/json':'containers','/networks':'networks','/volumes':'volumes','/events':'containers'}[path]
            filters=json.loads(query.get('filters','{}'));rows=[]
            filters={key:[item for item,enabled in value.items() if enabled] if isinstance(value,dict) else value
                for key,value in filters.items()}
            for row in self.objects[kind].values():
                labels=row.get('Config',row).get('Labels') or {}
                if any(labels.get(key)!=value for key,value in (item.split('=',1) for item in filters.get('label',[]))):continue
                if filters.get('name') and filters['name'][0]!='^'+row['Name']+'$':continue
                output=copy.deepcopy(row)
                if kind=='containers':output['Names']=[row['Name']]
                rows.append(output)
            return response({'Volumes':rows} if kind=='volumes' else rows)
        parts=path.strip('/').split('/')
        if parts[0] in self.objects and len(parts)>=2:
            kind,reference=parts[:2]
            matching=[(name,row) for name,row in self.objects[kind].items() if reference in (name,row['Id'])]
            if not matching:return response({'message':'missing'},404)
            name,row=matching[0]
            if method=='DELETE':del self.objects[kind][name];return services.Response(204,{},b'')
            if method=='GET' and (len(parts)==2 or parts[2]=='json'):return response(copy.deepcopy(row))
            if len(parts)==3 and parts[2]=='exec':return response({'Id':'e'*64},201)
            return response({'forwarded':path})
        if path.startswith('/exec/'):return response({'forwarded':path})
        raise AssertionError((method,path))

class Daemon(Engine):
    def __init__(self):super().__init__();self.archives=[];self.operations=[]
    def request(self,method,raw,body=b''):
        path=unquote(urlsplit(raw).path)
        if path.startswith('/v1.'):path=path[path.index('/',1):]
        self.operations.append((method,path))
        if path=='/info':
            return services.Response.json({'ID':self.daemon,'OSType':'linux','Architecture':'x86_64',
                'NCPU':4,'MemTotal':1000000000,'Containers':len(self.objects['containers']),'Images':1})
        if path=='/version':return services.Response.json({'ApiVersion':'1.47','MinAPIVersion':'1.24','Version':'27.5.1','Os':'linux','Arch':'amd64'})
        if method=='PUT' and path.endswith('/archive'):
            self.archives.append(body);return services.Response(200,{},b'')
        if path.endswith('/logs'):return services.Response(200,{'Content-Type':'application/vnd.docker.raw-stream'},b'ready\n')
        response=super().request(method,raw,body)
        if path=='/containers/json':
            rows=response.value()
            for row in rows:
                row['State']='running' if row.get('State',{}).get('Running') else 'created'
                row['Labels']=row['Config'].get('Labels',{})
            return services.Response.json(rows)
        if path.startswith('/containers/') and path.endswith('/start'):
            identifier=path.split('/')[2]
            next(row for row in self.objects['containers'].values() if identifier in (row['Id'],row['Name'].lstrip('/')))['State']['Running']=True
            return services.Response(204,{},b'')
        if path=='/containers/create':
            identifier=response.value()['Id'];row=next(row for row in self.objects['containers'].values() if row['Id']==identifier)
            row.update(State={'Running':False},NetworkSettings={'Networks':{},'Ports':row['HostConfig'].get('PortBindings',{})})
        if path=='/networks/create':
            row=next(row for row in self.objects['networks'].values() if row['Id']==response.value()['Id'])
            row.setdefault('Driver','bridge');row['Containers']={}
        return response

@contextlib.contextmanager
def serving(engine,path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def respond(self):
            size=int(self.headers.get('Content-Length','0'));body=self.rfile.read(size)
            response=engine.request(self.command,self.path,body)
            self.send_response(response.status)
            for key,value in response.headers.items():self.send_header(key,value)
            self.send_header('Content-Length',str(len(response.body)));self.send_header('Connection','close');self.end_headers()
            if self.command!='HEAD':self.wfile.write(response.body)
            self.close_connection=True
        do_GET=do_HEAD=do_POST=do_PUT=do_DELETE=respond
    server=socket_bridge.unix_server(path,Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield path
    finally:
        server.shutdown();server.server_close();thread.join(3)
        if thread.is_alive():raise RuntimeError('fake Engine server did not stop')
