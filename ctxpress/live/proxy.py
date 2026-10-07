"""A local proxy between an agent harness and its model API that applies a context-management method to every
request. Standard library only.

    python -m ctxpress.live.proxy --method ComplexityTrap --args '{"n": 10}' --port 8899 \
        --upstream https://chatgpt.com/backend-api/codex --via http://<gateway>:7890 --log runs/x.jsonl

Codex: point the built-in OpenAI provider at the proxy and turn off request compression, e.g. in config.toml
    openai_base_url = "http://172.17.0.1:8899"
    [features]
    enable_request_compression = false
(WebSocket upgrades are refused, so Codex falls back to HTTP.) `POST .../responses`, `.../v1/messages` (Anthropic)
and `.../chat/completions` bodies with tools are rewritten;
native compaction requests are forwarded unchanged unless the method forbids them.
Codex identifies both local and v2 remote compaction on /responses with turn metadata.
Responses are streamed back as they
arrive. The log has, per request, what the method changed and the real token usage the API reported; request
headers (which carry the login token) are never logged or stored.
Methods may opt into bounded retries after an explicit HTTP 400 context-length
rejection. Each wire attempt is recorded; missing usage is never assumed free.
"""
from __future__ import annotations
import argparse, http.client, json, os, re, socketserver, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from ctxpress.live.rewrite import Rewriter
from ctxpress.live.contract import HostContractError
from ctxpress.core.params import DEFAULT

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding",
       "upgrade", "host", "content-length", "accept-encoding"}
ERROR_BODY_LIMIT = 1024 * 1024
_CONTEXT_ERROR = re.compile(r"context_length_exceeded|prompt (?:is )?too long|too many tokens|"
    r"(?:exceed\w*|maximum).{0,80}context|context.{0,30}(?:length|window).{0,80}(?:exceed|limit)|"
    r"input tokens.{0,80}exceed", re.IGNORECASE | re.DOTALL)


def context_length_error(raw):
    """Recognize a provider's explicit length rejection, not arbitrary request text."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return bool(_CONTEXT_ERROR.search(raw.decode('utf-8', 'replace')))
    if not isinstance(data, dict) or not isinstance(data.get('error'), dict):
        return False
    error = data['error']
    if error.get('code') in ('context_length_exceeded', 'prompt_too_long', 'input_too_long'):
        return True
    return isinstance(error.get('message'), str) and bool(_CONTEXT_ERROR.search(error['message']))


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


def is_native_compaction(body, turn_metadata=None, compact_prompt=None):
    """Use native dispatch metadata, never a keyword in the user's history.

    Current Codex sends the same JSON in a compatibility header and the
    client_metadata field. Only the request kind is inspected or retained.
    A private exact prompt additionally covers older local-compaction hosts.
    """
    body = body if isinstance(body, dict) else {}
    client = body.get('client_metadata')
    embedded = client.get('x-codex-turn-metadata') if isinstance(client, dict) else None
    for raw in (turn_metadata, embedded):
        try:
            metadata = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        if isinstance(metadata, dict) and metadata.get('request_kind') == 'compaction':
            return True
    if compact_prompt and isinstance(body.get('input'), list):
        from ctxpress.live.rewrite import content_text
        users = [item for item in body['input'] if isinstance(item, dict) and item.get('role') == 'user']
        if users and content_text(users[-1].get('content')) == compact_prompt:
            return True
    return False


def make_handler(rewriter, upstream, via, log, summaries=False, max_overflow_retries=0,
                 allow_native_compaction=True, compact_prompt=None, method_tools=None, upstream_key=None,
                 chat_session=None, chat_volatile=None):
    up = urllib.parse.urlsplit(upstream)
    vp = urllib.parse.urlsplit(via) if via else None
    lock = threading.Lock()

    def outgoing(headers):
        """Request headers for the upstream; with upstream_key the proxy, not the client, holds the credential."""
        kept = {k: v for k, v in headers.items() if k.lower() not in HOP}
        if upstream_key:
            kept = {k: v for k, v in kept.items() if k.lower() not in ("authorization", "api-key", "x-api-key")}
            kept["Authorization"] = "Bearer " + upstream_key
        return kept

    def connect():
        if up.scheme == "https":
            if vp:
                c = http.client.HTTPSConnection(vp.hostname, vp.port or 80, timeout=600)
                c.set_tunnel(up.hostname, up.port or 443)
            else:
                c = http.client.HTTPSConnection(up.hostname, up.port or 443, timeout=600)
        else:
            c = http.client.HTTPConnection(up.hostname, up.port or 80, timeout=600)
        return c

    def record(row):
        if log:
            from ctxpress.live.logging import append_record
            with lock:
                append_record(log, row)

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _forward(self, method):
            if self.headers.get("Upgrade"):
                self.send_response(426); self.send_header("Content-Length", "0"); self.end_headers(); return
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            incoming = urllib.parse.urlsplit(self.path)
            path = up.path.rstrip("/") + incoming.path
            query = "&".join(part for part in (up.query, incoming.query) if part)
            if query:
                path += "?" + query
            compact = method == "POST" and incoming.path.rstrip("/").endswith("/responses/compact")
            responses = method == 'POST' and incoming.path.rstrip('/').endswith('/responses')
            parsed = None
            if (compact or responses) and body:
                if (self.headers.get('Content-Encoding') or '').lower() not in ('', 'identity'):
                    self.send_error(415, 'request compression must be off (features.enable_request_compression = false)'); return
                try:
                    parsed = json.loads(body)
                except (ValueError, UnicodeError):
                    pass
            if responses:
                compact = is_native_compaction(parsed, self.headers.get('x-codex-turn-metadata'), compact_prompt)
            if compact and not allow_native_compaction:
                record(dict(type='native_compaction_blocked', t=time.time(), status=400,
                            reason='method_disallows_native_compaction', upstream_sent=False))
                error = json.dumps({'error': {'type': 'invalid_request_error',
                    'code': 'ctxpress_compaction_disabled',
                    'message': 'This method forbids native compaction. Stop with the existing history and task artifacts.'}}).encode()
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(error)))
                self.end_headers(); self.wfile.write(error); return
            info = dict(type="native_compaction", t=time.time()) if compact else None
            if compact and body:
                try:
                    info['model'] = json.loads(body).get('model')
                except (ValueError, AttributeError):
                    info['model'] = None
            original, j, summarizer, dialect, key = None, None, None, "responses", None
            if method == "POST" and incoming.path.rstrip("/").endswith("/chat/completions") and body:
                # Chat Completions (vLLM / LiteLLM agents): same method on flattened messages (ctxpress.live.chat)
                dialect = "chat"
                if (self.headers.get("Content-Encoding") or "").lower() not in ("", "identity"):
                    self.send_error(415, "request compression must be off"); return
                try:
                    from ctxpress.live import chat
                    j = json.loads(body)
                    original = j
                    key = chat_session or chat.session_key(j)     # a dedicated proxy serves one conversation
                    if summaries and chat.rewritable(j):
                        summary_headers = outgoing(self.headers)
                        summarizer = chat.ChatSummarizer(connect, path, summary_headers, j,
                            lambda row: record(dict(row, session=key)))
                    j, info = chat.rewrite_chat(rewriter, j, key, summarizer=summarizer, volatile=chat_volatile)
                    if info is not None:
                        info.update(model=j.get('model'), dialect=dialect)
                        body = json.dumps(j, ensure_ascii=False).encode("utf-8")
                except HostContractError as e:
                    record(dict(type='host_contract_failed', t=time.time(), status=422,
                                reason=str(e), upstream_sent=False))
                    self.send_error(422, 'ctxpress host contract failed'); return
                except Exception as e:
                    info = None
                    record(dict(t=time.time(), error=f"rewrite failed: {e!r}"))
            elif method == "POST" and incoming.path.rstrip("/").endswith("/v1/messages") and body:
                # Anthropic Messages (Claude Code): same method, flattened blocks (ctxpress.live.anthropic)
                dialect = "anthropic"
                try:
                    from ctxpress.live import anthropic
                    j = json.loads(body)
                    original = j
                    key = anthropic.session_key(j)
                    if summaries and anthropic.rewritable(j):
                        summary_headers = outgoing(self.headers)
                        summarizer = anthropic.MessagesSummarizer(connect, path, summary_headers, j,
                            lambda row: record(dict(row, session=key)))
                    j, info = anthropic.rewrite_messages(rewriter, j, key, summarizer=summarizer)
                    if info is not None:
                        info.update(model=j.get('model'), dialect=dialect)
                        body = json.dumps(j, ensure_ascii=False).encode("utf-8")
                except HostContractError as e:
                    record(dict(type='host_contract_failed', t=time.time(), status=422,
                                reason=str(e), upstream_sent=False))
                    self.send_error(422, 'ctxpress host contract failed'); return
                except Exception as e:                       # other rewrite errors retain legacy fallback
                    info = None
                    record(dict(t=time.time(), error=f"rewrite failed: {e!r}"))
            elif responses and not compact and body:
                if (self.headers.get("Content-Encoding") or "").lower() not in ("", "identity"):
                    self.send_error(415, "request compression must be off (features.enable_request_compression = false)"); return
                try:
                    j = json.loads(body)
                    key = j.get("prompt_cache_key") or self.headers.get("session_id") or self.headers.get("conversation_id") or "default"
                    route_observation = {}
                    if method_tools is not None:
                        j = method_tools.adapt(j, session_key=key, observation=route_observation)
                    original = j
                    summarizer = None
                    if summaries:
                        from ctxpress.live.summarize import ResponsesSummarizer
                        summary_headers = outgoing(self.headers)
                        summarizer = ResponsesSummarizer(connect, path, summary_headers, j,
                            lambda row: record(dict(row, session=key)))
                    j, info = rewriter.rewrite_body(j, key, summarizer=summarizer)
                    if info is not None:                     # a string `input` (one-shot call) is not a history
                        info.update(route_observation)
                        info['model'] = j.get('model')
                    body = json.dumps(j, ensure_ascii=False).encode("utf-8")
                except HostContractError as e:
                    record(dict(type='host_contract_failed', t=time.time(), status=422,
                                reason=str(e), upstream_sent=False))
                    self.send_error(422, 'ctxpress host contract failed'); return
                except Exception as e:
                    from ctxpress.live.method_tools import MethodToolRouteError
                    if isinstance(e, MethodToolRouteError):
                        record(dict(type='method_tool_route_failed', t=time.time(), status=400,
                                    reason=str(e), upstream_sent=False))
                        error = json.dumps({'error': {'type': 'invalid_request_error',
                            'code': 'ctxpress_method_tool_route_failed', 'message': str(e)}}).encode()
                        self.send_response(400)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(error)))
                        self.end_headers(); self.wfile.write(error); return
                    record(dict(t=time.time(), error=f"rewrite failed: {e!r}"))
            if info is not None:
                info['native_compaction_detection'] = 'endpoint_and_turn_metadata_v1'
            passthrough = None
            if info is None and body and (dialect in ("anthropic", "chat") or responses):
                # A model call forwarded unchanged (side calls, bodies the method does not rewrite): logged and billed
                # as its own role ("passthrough") so cost covers every model call the proxy carried.
                try:
                    model = json.loads(body).get("model")
                except (ValueError, UnicodeError, AttributeError):
                    model = None
                passthrough = dict(type="passthrough", t=time.time(), dialect=dialect, model=model, session=key)
            headers = outgoing(self.headers)
            headers["Content-Length"] = str(len(body))
            headers["Accept-Encoding"] = "identity"
            t0 = time.time()
            attempt, buffered = 0, b""
            while True:
                c = connect()
                try:
                    headers['Content-Length'] = str(len(body))
                    c.request(method, path, body=body if body else None, headers=headers)
                    r = c.getresponse()
                except Exception as e:
                    c.close()
                    if info is not None or passthrough is not None:
                        record(dict(info if info is not None else passthrough, status=None, seconds=round(time.time()-t0,2),
                                    usage=None, http_attempt=attempt, upstream_error=type(e).__name__))
                    self.send_error(502, f"upstream: {e!r}"); return
                buffered = b""
                if r.status == 400 and info is not None and "request" in info and dialect == "responses" and attempt < max_overflow_retries:
                    # Large error bodies retain streaming behavior and are not
                    # classified or retained in diagnostics.
                    try:
                        buffered = r.read(ERROR_BODY_LIMIT + 1)
                    except (http.client.HTTPException, TimeoutError, OSError) as error:
                        buffered = getattr(error, 'partial', b'')
                        info['stream_error'] = type(error).__name__
                        r.close(); c.close()
                        break
                    if len(buffered) < ERROR_BODY_LIMIT + 1 and r.length not in (None, 0):
                        # read(amt) can return short without raising even when
                        # Content-Length proves the response was interrupted.
                        info['stream_error'] = 'IncompleteRead'
                        r.close(); c.close()
                        break
                    if len(buffered) <= ERROR_BODY_LIMIT and context_length_error(buffered):
                        c.close()
                        try:
                            retry = rewriter.retry_body(original, j, info, summarizer=summarizer)
                        except Exception as error:
                            if isinstance(error, HostContractError):
                                record(dict(type='host_contract_failed', t=time.time(), status=422,
                                            reason=str(error), upstream_sent=False, stage='overflow_retry'))
                            record(dict(t=time.time(), error='overflow rewrite failed: ' + type(error).__name__))
                            retry = None
                        if retry is not None:
                            record(dict(info, status=400, seconds=round(time.time()-t0,2),
                                        http_attempt=attempt, context_length_error=True, usage=find_usage(buffered)))
                            j, info = retry
                            info.update(route_observation)
                            info['model'] = j.get('model')
                            body = json.dumps(j, ensure_ascii=False).encode('utf-8')
                            attempt += 1
                            continue
                break
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            usage, tail = None, b""
            try:
                while True:
                    if buffered:
                        chunk, buffered = buffered, b""
                    else:
                        chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
                    if not chunk:
                        if info is not None and r.length not in (None, 0):
                            info['stream_error'] = 'IncompleteRead'
                            self.close_connection = True
                        elif passthrough is not None and r.length not in (None, 0):
                            passthrough['stream_error'] = 'IncompleteRead'
                        break
                    tail = (tail + chunk)[-400000:]
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk)); self.wfile.flush()
                if info is None or not info.get('stream_error'):
                    self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError) as error:
                self.close_connection = True                 # the client went away (e.g. a cancelled request)
                for row in (info, passthrough):
                    if row is not None:
                        row['stream_error'] = type(error).__name__
            except (http.client.HTTPException, TimeoutError, OSError) as error:
                self.close_connection = True
                for row in (info, passthrough):
                    if row is not None:
                        row['stream_error'] = type(error).__name__
            finally:
                c.close()
            if os.environ.get("CTXPRESS_DEBUG"):
                print(f"ctxpress proxy: {method} {self.path} -> {r.status} rewritten={info is not None} log={log}", file=sys.stderr, flush=True)
            if info is not None:
                if dialect == "anthropic":
                    from ctxpress.live import anthropic
                    usage = None if info.get('stream_error') else anthropic.find_usage(tail.decode("utf-8", "ignore"))
                elif dialect == "chat":
                    from ctxpress.live import chat
                    usage = None if info.get('stream_error') else chat.find_usage(tail.decode("utf-8", "ignore"))
                else:
                    usage = None if info.get('stream_error') else find_usage(tail)
                info.update(status=r.status, seconds=round(time.time() - t0, 2), usage=usage, http_attempt=attempt)
                info['response_model'] = None if info.get('stream_error') else find_model(tail)
                if dialect == "anthropic" and not info.get('stream_error'):
                    from ctxpress.live import anthropic
                    info['response_model'] = anthropic.find_model(tail.decode("utf-8", "ignore"))
                elif dialect == "chat" and not info.get('stream_error'):
                    from ctxpress.live import chat
                    info['response_model'] = chat.find_model(tail.decode("utf-8", "ignore"))
                if compact:
                    info['completed'] = 200 <= r.status < 300 and not info.get('stream_error')
                record(info)
            elif passthrough is not None:
                text = tail.decode("utf-8", "ignore")
                if dialect == "anthropic":
                    from ctxpress.live import anthropic
                    usage, returned = anthropic.find_usage(text), anthropic.find_model(text)
                elif dialect == "chat":
                    from ctxpress.live import chat
                    usage, returned = chat.find_usage(text), chat.find_model(text)
                else:
                    usage, returned = find_usage(tail), find_model(tail)
                stream_error = passthrough.get('stream_error')
                record(dict(passthrough, status=r.status, seconds=round(time.time() - t0, 2),
                            usage=None if stream_error else usage, response_model=None if stream_error else returned))

        def _safe(self, method):
            try:
                self._forward(method)
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                self.close_connection = True                 # client or upstream went away; the client retries

        def do_POST(self):
            self._safe("POST")

        def do_GET(self):
            self._safe("GET")

        def do_DELETE(self):
            self._safe("DELETE")

    return H


def find_model(raw):
    """Returned model ID from the terminal response, without retaining response text."""
    text, best = raw.decode('utf-8', 'ignore'), None
    for line in text.splitlines():
        if line.startswith('data:'):
            try:
                event = json.loads(line[5:].strip())
                if event.get('type') == 'response.completed':
                    best = (event.get('response') or {}).get('model')
            except (ValueError, AttributeError):
                pass
    if best is None:
        try:
            best = json.loads(text).get('model')
        except (ValueError, AttributeError):
            pass
    return best if isinstance(best, str) and best.strip() else None


def find_usage(raw):
    """The `usage` object of the last response.completed event in an SSE stream (or a JSON body)."""
    text = raw.decode("utf-8", "ignore")
    best = None
    def normalized(u):
        if not isinstance(u, dict):
            return None
        inputs = u.get("input_tokens_details")
        outputs = u.get("output_tokens_details")
        return dict(input_tokens=u.get("input_tokens"),
                    cached_tokens=inputs.get("cached_tokens") if isinstance(inputs, dict) else None,
                    cache_write_tokens=inputs.get("cache_write_tokens") if isinstance(inputs, dict) else None,
                    output_tokens=u.get("output_tokens"),
                    reasoning_tokens=outputs.get("reasoning_tokens") if isinstance(outputs, dict) else None)
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            d = json.loads(line[5:].strip())
        except Exception:
            continue
        response = d.get("response") if isinstance(d, dict) else None
        u = response.get("usage") if isinstance(response, dict) else None
        if u:
            best = normalized(u)
    if best is None:
        try:
            u = json.loads(text).get("usage")
            best = normalized(u) if u else None
        except Exception:
            pass
    return best


def serve(method_factory, port, upstream, via=None, log=None, params=DEFAULT, host="0.0.0.0", store_dir=None, store_prefix=None,
          retrieve_tool=None, summarizer=None, codex_method_tools=False, control_receipts=None, upstream_key=None,
          chat_session=None, chat_volatile=None):
    def live_factory():
        method = method_factory()
        method.validate_live()
        return method
    sample = live_factory()  # Fail before binding a port or starting the user's agent.
    if type(sample.max_overflow_retries) is not int or not 0 <= sample.max_overflow_retries <= 16:
        raise ValueError('max_overflow_retries must be an integer between 0 and 16')
    rw = Rewriter(live_factory, params, store_dir=store_dir, store_prefix=store_prefix, retrieve_tool=retrieve_tool,
                   summarizer=summarizer)
    from ctxpress.live.method_tools import MethodTools
    adapter = MethodTools(sample, receipts=control_receipts) if codex_method_tools and sample.agent_tools else None
    srv = ThreadingHTTPServer((host, port), make_handler(rw, upstream, via, log,
                                  method_tools=adapter,
                                  summaries=sample.requires_summary and summarizer is None,
                                  max_overflow_retries=sample.max_overflow_retries, upstream_key=upstream_key,
                                  chat_session=chat_session, chat_volatile=chat_volatile,
                                  allow_native_compaction=sample.allow_native_compaction,
                                  compact_prompt=sample.codex_config.get('compact_prompt') if not sample.allow_native_compaction else None))
    return srv, rw


def main(argv=None):
    from ctxpress.methods import build
    from ctxpress.live.factory import frozen_factory
    ap = argparse.ArgumentParser(prog="ctxpress.live.proxy")
    ap.add_argument("--method", default="NoCompaction")
    ap.add_argument("--args", default="{}")
    ap.add_argument("--budget", type=int, help="retention budget; unit and scope depend on the selected method (ctxpress list)")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--upstream", default="https://chatgpt.com/backend-api/codex")
    ap.add_argument("--via", help="HTTP proxy for the upstream connection (CONNECT)")
    ap.add_argument("--log")
    ap.add_argument("--store-dir", help="dedicated memory: where originals are written (host path)")
    ap.add_argument("--store-prefix", help="the same directory as the agent sees it")
    a = ap.parse_args(argv)
    entry = {"class": a.method, "args": json.loads(a.args)}
    if a.budget is not None:
        from ctxpress.methods.budget import with_budget
        entry = with_budget(entry, a.budget)
    srv, _ = serve(frozen_factory(build(entry)), a.port, a.upstream, a.via, a.log, host=a.host, store_dir=a.store_dir, store_prefix=a.store_prefix)
    print(f"ctxpress proxy: {a.method} {a.args} on {a.host}:{a.port} -> {a.upstream}", file=sys.stderr, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
