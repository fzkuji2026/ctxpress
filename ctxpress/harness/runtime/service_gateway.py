"""Scoped Unix Docker API transport for prepared verifier services.

Only service_resources authorizes requests. Raw host API sockets are never
mounted into verifiers. Stream/exec upgrades use the same checked object IDs.
"""
from __future__ import annotations
import http.client,json,os,re,socket,tempfile,threading,uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote
from ctxpress.harness.runtime import connect_proxy, socket_bridge
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.harness.runtime.docker import docker
from ctxpress.harness.runtime.service_resources import Rejected, Response, Scope, recover as recover_scope

MAX_BODY=64*1024*1024


def endpoint():
    value=os.environ.get('DOCKER_HOST') or docker('context','inspect','--format','{{.Endpoints.docker.Host}}').strip()
    if not value.startswith('unix:///') or '?' in value or '\x00' in value:
        raise Rejected('native service executor requires the selected local Unix Docker endpoint')
    return value[7:]


class Connection(http.client.HTTPConnection):
    def __init__(self,path,timeout=30):super().__init__('localhost',timeout=timeout);self.path=str(path)
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(self.timeout)
        try:self.sock.connect(self.path)
        except BaseException:self.sock.close();raise


class Backend:
    def __init__(self,path=None):self.path=str(path or endpoint())
    def request(self,method,path,body=b''):
        connection=Connection(self.path)
        try:
            connection.request(method,path,body,{'Content-Type':'application/json'})
            response=connection.getresponse();content=response.read(8*1024*1024+1)
            if len(content)>8*1024*1024:raise Rejected('Docker control response exceeds the bounded transport')
            return Response(response.status,dict(response.getheaders()),content)
        finally:connection.close()


def read_body(handler):
    """Decode Docker SDK chunked tar uploads with an explicit memory ceiling."""
    def exact(size):
        pieces=[];remaining=size
        while remaining:
            value=handler.rfile.read(remaining)
            if not value:raise Rejected('incomplete Docker request body')
            pieces.append(value);remaining-=len(value)
        return b''.join(pieces)
    transfer=handler.headers.get_all('Transfer-Encoding') or []
    lengths=handler.headers.get_all('Content-Length') or []
    if len(transfer)>1 or len(lengths)>1 or transfer and lengths:raise Rejected('ambiguous Docker request framing')
    if transfer:
        if transfer[0].lower()!='chunked':raise Rejected('unsupported Docker transfer encoding')
        chunks=[];total=0
        while True:
            line=handler.rfile.readline(128)
            if not line.endswith(b'\r\n') or not re.fullmatch(b'[0-9A-Fa-f]+(?:;[^\r\n]*)?\r\n',line):
                raise Rejected('invalid Docker upload chunk')
            size=int(line.split(b';',1)[0].strip(),16)
            if not size:
                # SDKs do not need trailers; reject rather than leaving bytes
                # that might be interpreted as a second request on the socket.
                if handler.rfile.readline(8192)!=b'\r\n':raise Rejected('Docker upload trailers are unavailable')
                return b''.join(chunks)
            total+=size
            if total>MAX_BODY:raise Rejected('Docker upload exceeds service transport limit',413)
            chunk=exact(size)
            if exact(2)!=b'\r\n':raise Rejected('incomplete Docker upload chunk')
            chunks.append(chunk)
    value=lengths[0] if lengths else '0'
    if not value.isdigit() or int(value)>MAX_BODY:raise Rejected('invalid or oversized Docker request body',413)
    body=exact(int(value))
    return body


def handler(scope,connections):
    capacity=threading.BoundedSemaphore(32)
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1';rbufsize=0
        def log_message(self,*args):pass
        def setup(self):
            self.request.settimeout(30);super().setup()
        def handle(self):
            if not capacity.acquire(blocking=False):return
            connections.add(self.request)
            try:super().handle()
            finally:connections.discard(self.request);capacity.release()
        def send(self,response):
            self.send_response(response.status)
            for key,value in response.headers.items():
                if key.lower() not in ('connection','content-length','transfer-encoding','server','date'):
                    self.send_header(key,value)
            self.send_header('Content-Length',str(0 if self.command=='HEAD' else len(response.body)))
            self.send_header('Connection','close');self.end_headers()
            if self.command!='HEAD':self.wfile.write(response.body)
            self.close_connection=True
        def forward(self,method,path,body):
            headers={key:self.headers[key] for key in ('Content-Type','Connection','Upgrade','Sec-WebSocket-Key','Sec-WebSocket-Version','Sec-WebSocket-Protocol') if key in self.headers}
            if self.headers.get('Upgrade'):
                return self.upgrade(method,path,body,headers)
            connection=Connection(scope.backend.path,timeout=600);connections.add(connection)
            try:
                connection.request(method,path,body,headers)
                response=connection.getresponse();self.send_response(response.status)
                for key,value in response.getheaders():
                    if key.lower() not in ('connection','content-length','transfer-encoding','server','date'):self.send_header(key,value)
                self.send_header('Connection','close');self.end_headers();self.close_connection=True
                if method!='HEAD':
                    while True:
                        content=response.read1(65536)
                        if not content:break
                        self.wfile.write(content);self.wfile.flush()
            finally:connections.discard(connection);connection.close()
            return None
        def upgrade(self,method,path,body,headers):
            upstream=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);upstream.settimeout(30);connections.add(upstream)
            try:
                upstream.connect(scope.backend.path)
                lines=[method+' '+path+' HTTP/1.1','Host: localhost','Content-Length: '+str(len(body))]
                lines.extend(key+': '+value for key,value in headers.items())
                upstream.sendall(('\r\n'.join(lines)+'\r\n\r\n').encode('latin-1')+body)
                header=bytearray()
                while not header.endswith(b'\r\n\r\n'):
                    byte=upstream.recv(1)
                    if not byte or len(header)>=65536:raise Rejected('invalid Docker stream upgrade')
                    header.extend(byte)
                self.wfile.write(header);self.wfile.flush();self.close_connection=True
                upstream.settimeout(600);self.request.settimeout(600)
                connect_proxy.relay(self.request,upstream,600)
            finally:connections.discard(upstream);upstream.close()
            return None
        def dispatch(self):
            try:
                body=read_body(self)
                response=scope.dispatch(self.command,self.path,body,self.forward)
                if response is not None:self.send(response)
            except Rejected as error:self.send(Response.json({'message':str(error)},error.status))
            except (OSError,ValueError,http.client.HTTPException):
                # No request body, auth or test environment appears in logs.
                if not self.close_connection:self.send(Response.json({'message':'owned Docker transport failed'},502))
        do_GET=do_HEAD=do_POST=do_PUT=do_DELETE=dispatch
    return Handler


class Connections:
    def __init__(self):self.lock=threading.RLock();self.items=set();self.closed=False
    def add(self,value):
        with self.lock:
            if self.closed:
                value.close();raise Rejected('service transport is closed',503)
            self.items.add(value)
    def discard(self,value):
        with self.lock:self.items.discard(value)
    def close(self):
        with self.lock:
            self.closed=True
            for value in list(self.items):
                try:
                    stream=getattr(value,'sock',value)
                    if stream is not None:stream.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                value.close()
            self.items.clear()


class Gateway:
    def __init__(self,registry,images,backend=None):
        self.scope=Scope(registry,images,backend or Backend());self.connections=Connections()
        self.server=self.thread=None
    @property
    def record(self):return self.scope.record
    def bind_verifier(self,owner):
        if owner.registry is not self.scope.registry or owner.record['role']!='verifier' or self.record['verifier'] is not None:
            raise Rejected('service gateway requires its unique owned verifier')
        self.record['verifier']=owner.record['container'];self.scope.persist()
    def open(self):
        if self.record['phase']!='prepared':raise Rejected('service gateway can open only once')
        root=Path(tempfile.gettempdir()).resolve()/('ctxp-svc-'+self.record['scope']+'-'+uuid.uuid4().hex[:12])
        if root.exists() or root.is_symlink():raise Rejected('service transport name is already occupied')
        self.record.update(channel=str(root),phase='creating');self.scope.persist()
        root.mkdir(mode=0o755);root.chmod(0o755)
        eval_plan.atomic_json(root/'owner.json',{'project':self.record['project'],'label':self.record['label'],'scope':self.record['scope']})
        self.server=socket_bridge.unix_server(root/'docker.sock',handler(self.scope,self.connections));(root/'docker.sock').chmod(0o666)
        self.thread=threading.Thread(target=self.server.serve_forever,name='ctxpress-services-'+self.record['scope'],daemon=True)
        self.thread.start();self.record['phase']='serving';self.scope.persist();return root
    def stop(self):
        # No new control operation may enter after the server closes. Active
        # log/exec connections are interrupted before resource deletion.
        with self.scope.lock:self.scope.closed=True
        if self.server:
            self.server.shutdown();self.connections.close();self.server.server_close()
            if self.thread:self.thread.join(timeout=5)
            if self.thread and self.thread.is_alive():raise Rejected('service gateway did not stop')
            self.server=self.thread=None
    def cleanup(self):
        self.stop();self.scope.cleanup();remove_channel(self.scope)


def remove_channel(scope):
    record=scope.record
    if record.get('channel') is None:return
    root=Path(record['channel'])
    if (root.is_symlink() or root.parent!=Path(tempfile.gettempdir()).resolve() or
            not re.fullmatch(r'ctxp-svc-'+re.escape(record['scope'])+r'-[0-9a-f]{12}',root.name)):
        raise Rejected('invalid service channel recovery path')
    # A retained verifier can need a restart during owner cleanup. Keep its
    # mounted directory until its checked container has been removed.
    scope.verifier_removed()
    names=scope.backend.request('GET','/containers/json?all=1&filters='+
        quote(json.dumps({'label':['ctxpress.milestone.service_scope='+record['scope']]}),safe='')).value()
    if names:raise Rejected('service scope still has containers')
    if not root.exists():return
    if not root.is_dir():raise Rejected('service channel is not a directory')
    owner=root/'owner.json'
    if owner.is_symlink() or owner.exists() and json.loads(owner.read_text(encoding='utf-8'))!={'project':record['project'],'label':record['label'],'scope':record['scope']}:
        raise Rejected('service channel ownership changed')
    if any(path.name not in ('owner.json','docker.sock') for path in root.iterdir()):raise Rejected('service channel contains undeclared files')
    socket_path=root/'docker.sock'
    if socket_path.is_symlink() or socket_path.exists() and not socket_path.is_socket():raise Rejected('service channel socket changed')
    socket_path.unlink(missing_ok=True);owner.unlink(missing_ok=True);root.rmdir()


def recover(path,label):
    scope=recover_scope(path,label,Backend());remove_channel(scope)
