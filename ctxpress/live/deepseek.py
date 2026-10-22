"""Independent DeepSeek transport for infinite-context; never forward host credentials."""
import http.client
import json
import os
import time
import socket
from pathlib import Path

MODEL = 'deepseek-flash'


def relay_handler(base):
    """One fixed operation on the existing owned channel; credentials stay on host."""
    class Handler(base):
        def do_POST(self):
            if self.path != '/infinite-context/deepseek':
                self.send_error(404)
                return
            records = []
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024*1024:
                    raise ValueError('invalid request size')
                parts = []
                remaining = length
                while remaining:
                    part = self.rfile.read(remaining)
                    if not part:
                        raise ValueError('incomplete request')
                    parts.append(part)
                    remaining -= len(part)
                data = json.loads(b''.join(parts))
                if set(data) != {'system', 'user'} or any(not isinstance(v, str) for v in data.values()):
                    raise ValueError('invalid request')
                answer = summarize(data['system'], data['user'], record=records.append, _direct=True)
                result = dict(answer=answer, records=records)
                status = 200
            except Exception:
                result = dict(error='DeepSeek compression failed', records=records)
                status = 502
            raw = json.dumps(result).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    return Handler


def _relay(system, user, record, path):
    class UnixConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(str(path))
    # longer than the host's wait for DeepSeek (120 s per read), so a slow answer and its usage still arrive
    connection = UnixConnection('localhost', timeout=300)
    try:
        connection.request('POST', '/infinite-context/deepseek',
                           body=json.dumps(dict(system=system, user=user)).encode(),
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        data = json.loads(response.read())
        for row in data.get('records', []):
            record(row)
        if response.status != 200:
            raise ValueError('DeepSeek relay failed')
        return data['answer']
    finally:
        connection.close()


def key_available():
    """A DeepSeek key is on this host (DEEPSEEK_API_KEY or ~/.config/ctxpress/deepseek-api-key); never reads it."""
    return bool(os.environ.get('DEEPSEEK_API_KEY', '').strip()) or (Path.home()/'.config/ctxpress/deepseek-api-key').is_file()


def summarize(system, user, *, record, model=MODEL, _direct=False):
    if model != MODEL:
        raise ValueError('unsupported DeepSeek compression model')
    if not callable(record):
        raise ValueError('DeepSeek compression requires usage recording')
    check_available = getattr(record, 'check_available', None)
    if callable(check_available):
        check_available()
    channel = Path('/ctxpress-channel/model.sock')
    if not _direct and channel.exists():
        return _relay(system, user, record, channel)
    key = os.environ.get('DEEPSEEK_API_KEY', '').strip()
    if not key:
        credential = Path.home()/'.config/ctxpress/deepseek-api-key'
        if credential.is_file():
            key = credential.read_text().strip()
    if not key:
        raise ValueError('DEEPSEEK_API_KEY is required')
    if not callable(record):
        raise ValueError('DeepSeek compression requires usage recording')
    payload = dict(model=model, stream=False, thinking={'type': 'disabled'},
                   response_format={'type': 'json_object'}, max_tokens=8192,
                   messages=[dict(role='system', content=system), dict(role='user', content=user)])
    info = dict(type='summary', purpose='infinite_context_compression', model=model,
                provider='deepseek', dialect='chat', thinking='disabled', t=time.time())
    connection = http.client.HTTPSConnection('api.deepseek.com', timeout=120)
    try:
        connection.request('POST', '/chat/completions', body=json.dumps(payload).encode(),
                           headers={'Authorization': 'Bearer '+key, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        info['status'] = response.status
        raw = response.read()
        if response.status != 200:
            raise ValueError('DeepSeek request failed')
        reply = json.loads(raw)
        from ctxpress.live.chat import normalized_usage
        usage = reply.get('usage') or {}
        info['usage'] = normalized_usage(usage)
        if info['usage'] is not None:
            # DeepSeek charges cache-miss inputs at its input rate; there is
            # no separately billed cache-write token category on this endpoint.
            info['usage']['cache_write_tokens'] = 0
        if info['usage'] is not None and 'prompt_cache_hit_tokens' in usage:
            info['usage']['cached_tokens'] = usage['prompt_cache_hit_tokens']
        info['response_model'] = reply.get('model')
        choice = (reply.get('choices') or [{}])[0]
        text = (choice.get('message') or {}).get('content')
        if choice.get('finish_reason') != 'stop' or not isinstance(text, str) or not text.strip():
            raise ValueError('DeepSeek response did not complete')
        info['completed'] = True
        return text
    except Exception as error:
        info['completed'] = False
        # Provider bodies and exceptions can contain input or credentials.
        info['error'] = type(error).__name__
        raise ValueError('DeepSeek compression failed; original context retained') from None
    finally:
        connection.close()
        info['seconds'] = round(time.time()-info['t'], 3)
        record(info)
