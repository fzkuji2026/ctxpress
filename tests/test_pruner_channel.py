import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from ctxpress.methods.pruner_channel import relay_handler, request, validate_endpoint
from ctxpress.harness.runtime.socket_bridge import unix_server

@pytest.mark.parametrize('url', ['https://example.com/prune', 'http://127.0.0.1:80/other', 'http://user:secret@127.0.0.1:80/prune', 'http://127.0.0.1:80/prune?url=x'])
def test_reject_endpoint(url):
    with pytest.raises(ValueError): validate_endpoint(url)

def test_owned_channel_prunes_without_forwarding_credentials(tmp_path):
    observed=[]
    class Local(BaseHTTPRequestHandler):
        def do_POST(self):
            observed.append((self.path,dict(self.headers),json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            raw=b'{"pruned_code":"kept", "origin_token_cnt":50, "left_token_cnt":1}'
            self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def log_message(self,*args): pass
    local=ThreadingHTTPServer(('127.0.0.1',0),Local)
    channel=unix_server(tmp_path/'model.sock',relay_handler(Local,f'http://127.0.0.1:{local.server_port}/prune'))
    threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (local,channel)]
    for t in threads:t.start()
    try:
        body=json.dumps(dict(query='question',code='long output',threshold=.5,always_keep_first_frags=False,chunk_overlap_tokens=50)).encode()
        assert request(body,5,tmp_path/'model.sock')['pruned_code']=='kept'
        assert observed[0][0]=='/prune'
        assert observed[0][2]['code']=='long output'
        assert 'Authorization' not in observed[0][1]
        with pytest.raises(ValueError,match='channel request failed'):
            request(b'{"url":"http://other/"}',5,tmp_path/'model.sock')
        assert len(observed)==1
    finally:
        for s in (channel,local):s.shutdown();s.server_close()
        for t in threads:t.join()
