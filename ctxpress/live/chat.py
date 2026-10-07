"""The OpenAI Chat Completions API as a host (vLLM, LiteLLM clients and other OpenAI-compatible agents).

A /chat/completions body keeps the history as role messages: assistant messages carry text, optional
`reasoning_content` and `tool_calls`; each tool result is its own `tool` message. Every part becomes one flat item
of the kind ctxpress.live.rewrite already handles (text -> message, tool call -> function_call, tool message ->
function_call_output, reasoning_content -> reasoning), the shared Rewriter applies the method, and the result is
assembled back into a valid Chat Completions body:
  - parts of one original assistant message stay one message (text, reasoning_content and its remaining calls);
  - every assistant tool call is answered by the tool messages that follow it before any other message; a
    method-written message that would land inside that block waits until the block is complete;
  - method instructions are appended to the first system message (many chat templates accept system text only
    at the start), or become a leading system message when the request has none.
Messages the method did not touch are copied byte-for-byte from the request.

Some hosts annotate the newest tool result with a changing signal (the ACM agent appends
"[CURRENT CONTEXT TOKEN: N]" to the last tool message and drops it from the previous one). A `volatile` regex
names such a suffix: it is left out of the item the method sees, so moving it is not a history revision, and it is
put back when the method rewrites that output.
"""
from __future__ import annotations
import copy, hashlib, json, re, time

MEDIA_PARTS = ("image_url", "input_audio", "file")
_MISSING = object()                                # an assistant part not (yet) placed in the rebuilt message


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _media(content):
    if not isinstance(content, list):
        return []
    return [{"type": "input_image" if p.get("type") == "image_url" else "input_file", "chat": p}
            for p in content if isinstance(p, dict) and p.get("type") in MEDIA_PARTS]


def volatile_split(text, volatile):
    """(text without the host's volatile suffix, the suffix) for a tool result."""
    if volatile and isinstance(text, str):
        match = re.search(volatile, text)
        if match and match.end() == len(text):
            return text[:match.start()], text[match.start():]
    return text, ""


def flatten(body, volatile=None):
    """Flat items for the shared Rewriter, and for each the (message index, part) it came from.

    A part is "message" (the whole message), "text", "reasoning" or ("call", j) for an assistant's j-th tool call.
    """
    items, refs = [], []
    for i, message in enumerate(body.get("messages") or []):
        role = message.get("role")
        content = message.get("content")
        if role == "assistant":
            if isinstance(message.get("reasoning_content"), str) and message["reasoning_content"]:
                items.append({"type": "reasoning", "summary": [{"type": "summary_text", "text": message["reasoning_content"]}]})
                refs.append((i, "reasoning"))
            calls = message.get("tool_calls") or []
            if _text(content) or _media(content) or not calls:
                items.append({"type": "message", "role": "assistant",
                              "content": [{"type": "output_text", "text": _text(content)}, *_media(content)]})
                refs.append((i, "text"))
            for j, call in enumerate(calls):
                function = call.get("function") or {}
                arguments = function.get("arguments")
                items.append({"type": "function_call", "call_id": call.get("id"), "name": function.get("name"),
                              "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments or {})})
                refs.append((i, ("call", j)))
        elif role == "tool":
            item = {"type": "function_call_output", "call_id": message.get("tool_call_id"),
                    "output": volatile_split(_text(content), volatile)[0]}
            if _media(content):
                item["media"] = _media(content)
            items.append(item); refs.append((i, "message"))
        elif role in ("system", "developer", "user"):
            items.append({"type": "message", "role": role,
                          "content": [{"type": "input_text", "text": _text(content)}, *_media(content)]})
            refs.append((i, "message"))
        else:                                             # legacy "function" role and anything else: carried unchanged
            items.append({"type": "chat_" + str(role), "message": message}); refs.append((i, "message"))
    return items, refs


def session_key(body):
    """One conversation per model, system prompt and first user message (side calls differ in all of these)."""
    messages = body.get("messages") or []
    system = next((m for m in messages if m.get("role") in ("system", "developer")), None)
    user = next((m for m in messages if m.get("role") == "user"), None)
    seed = json.dumps([body.get("model"), body.get("user"), system, user], sort_keys=True, ensure_ascii=False)
    return "chat:" + hashlib.sha256(seed.encode()).hexdigest()[:32]


def rewritable(body):
    """Only agent turns (with tools) are managed; one-shot side requests (summaries, graders) pass through."""
    return isinstance(body.get("messages"), list) and bool(body.get("tools"))


def unflatten(body, refs, flat, out, volatile=None):
    """Assemble the rewritten flat items `out` (from `flat` + method summaries / instructions) into a chat body."""
    index = {id(item): k for k, item in enumerate(flat)}
    original = body.get("messages") or []
    system_extra, messages = [], []
    open_calls, deferred = set(), []                 # unanswered tool calls of the last assistant message

    def emit(message):
        messages.append(message)
        calls = message.get("tool_calls")
        for call in calls if isinstance(calls, list) else []:
            open_calls.add(call.get("id"))

    def flush():
        while deferred and not open_calls:
            emit(deferred.pop(0))

    current = None                                   # (source index, message) of the assistant being assembled
    for item in out:
        k = index.get(id(item))
        if k is None and item.get("type") == "function_call_output":        # a rewritten copy of an output
            k = next((n for n, x in enumerate(flat) if x.get("type") == "function_call_output"
                      and x.get("call_id") == item.get("call_id")), None)
        if k is None:                                                       # written by the method
            role, text = item.get("role", "user"), _text([{"type": "text", "text": p.get("text", "")}
                                                          for p in item.get("content") or []])
            if role in ("developer", "system"):
                system_extra.append(text)
            else:
                current = None
                (deferred.append if open_calls else emit)({"role": role, "content": text})
            continue
        i, part = refs[k]
        src = original[i]
        if src.get("role") == "assistant":
            if current is None or current[0] != i:
                # Same key order as the request; parts are filled in as their items arrive.
                has_text = (i, "text") in refs
                shell = {key: (_MISSING if key in ("reasoning_content", "tool_calls") or (key == "content" and has_text)
                               else copy.deepcopy(v)) for key, v in src.items()}
                current = (i, shell)
                emit(shell)
            message = current[1]
            if part == "text":
                message["content"] = copy.deepcopy(src.get("content"))
            elif part == "reasoning":
                message["reasoning_content"] = src["reasoning_content"]
            else:
                call = copy.deepcopy(src["tool_calls"][part[1]])
                if message.get("tool_calls", _MISSING) is _MISSING:
                    message["tool_calls"] = []
                message["tool_calls"].append(call)
                open_calls.add(call.get("id"))
            continue
        current = None
        message = copy.deepcopy(src)
        if src.get("role") == "tool":
            if item is not flat[k]:
                text = item.get("output", "") + volatile_split(_text(src.get("content")), volatile)[1]
                media = [m["chat"] for m in flat[k].get("media") or []]
                message["content"] = [{"type": "text", "text": text}, *media] if media else text
            open_calls.discard(src.get("tool_call_id"))
            messages.append(message)
            flush()
        else:
            (deferred.append if open_calls else emit)(message)
    open_calls.clear()
    flush()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for key in [key for key, v in message.items() if v is _MISSING]:
            if key == "content":
                message[key] = None                # its text was removed; a message with calls may have no text
            else:
                del message[key]
        if message.get("content") is None and not message.get("tool_calls"):
            message["content"] = ""                # nothing left but the role
    if system_extra:
        extra = "\n\n".join(system_extra)
        first = next((m for m in messages if m.get("role") in ("system", "developer")), None)
        if first is None:
            messages.insert(0, {"role": "system", "content": extra})
        elif isinstance(first.get("content"), list):
            first["content"] = [*first["content"], {"type": "text", "text": extra}]
        else:
            first["content"] = (first.get("content") or "") + "\n\n" + extra
    return dict(body, messages=messages)


def rewrite_chat(rewriter, body, key=None, summarizer=None, volatile=None):
    """(new body, info) with the method applied, or (body, None) when the request is not an agent turn."""
    if not rewritable(body):
        return body, None
    flat, refs = flatten(body, volatile)
    rewritten, info = rewriter.rewrite_body({"input": flat}, key or session_key(body), summarizer=summarizer)
    if info is None:
        return body, None
    return unflatten(body, refs, flat, rewritten["input"], volatile), info


def _events(text):
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                yield json.loads(line[5:].strip())
            except ValueError:
                continue


def normalized_usage(u):
    """Chat usage -> the shared fields. prompt_tokens include cached prompt tokens, as in the Responses API."""
    if not isinstance(u, dict):
        return None
    prompt = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
    completion = u.get("completion_tokens_details") if isinstance(u.get("completion_tokens_details"), dict) else {}
    return dict(input_tokens=u.get("prompt_tokens"), cached_tokens=prompt.get("cached_tokens"),
                cache_write_tokens=None, output_tokens=u.get("completion_tokens"),
                reasoning_tokens=completion.get("reasoning_tokens"))


def find_usage(text):
    """Usage of a JSON body, or of the last stream chunk that has one (stream_options.include_usage)."""
    usage = None
    for event in _events(text):
        if isinstance(event, dict) and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    if usage is None:
        try:
            usage = json.loads(text).get("usage")
        except (ValueError, AttributeError):
            usage = None
    return normalized_usage(usage)


def find_model(text):
    for event in _events(text):
        if isinstance(event, dict) and isinstance(event.get("model"), str) and event["model"].strip():
            return event["model"]
    try:
        model = json.loads(text).get("model")
    except (ValueError, AttributeError):
        return None
    return model if isinstance(model, str) and model.strip() else None


_THINK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)


class ChatSummarizer:
    """Summaries with the request's own model on the same upstream (the counterpart of ResponsesSummarizer)."""

    def __init__(self, connect, path, headers, request, record=None):
        self.connect, self.path, self.headers, self.request, self.record = connect, path, headers, request, record

    def summarize(self, items, purpose="history", budget=None):
        from ctxpress.live.summarize import GUIDANCE
        return self.summarize_with_guidance(items, purpose, budget, GUIDANCE)

    def summarize_with_guidance(self, items, purpose="history", budget=None, guidance=None):
        from ctxpress.live.summarize import DATA_BOUNDARY
        history = [{k: x[k] for k in ("seg", "role", "kind", "res", "call_id", "text") if k in x} for x in items]
        system = DATA_BOUNDARY + "\n" + guidance + f"\nScope: {purpose}." + (f" Target at most {budget} tokens." if budget else "")
        return self._send(self.request.get("model"), system, json.dumps(dict(history=history), ensure_ascii=False), purpose)

    def summarize_prompt(self, system, user, purpose="history", model=None):
        return self._send(model or self.request.get("model"), system, user, purpose)

    def _send(self, model, system, user, purpose):
        payload = dict(model=model, max_tokens=8192, stream=False,
                       messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        raw = json.dumps(payload, ensure_ascii=False).encode()
        headers = dict(self.headers, **{"Content-Length": str(len(raw)), "Accept-Encoding": "identity"})
        info = dict(type="summary", purpose=purpose, t=time.time(), model=model, dialect="chat")
        connection = self.connect()
        try:
            connection.request("POST", self.path, body=raw, headers=headers)
            response = connection.getresponse()
            data = response.read()
            info["status"] = response.status
            info["usage"] = find_usage(data.decode("utf-8", "ignore"))
            if response.status != 200:
                raise ValueError(f"summary HTTP status {response.status}")
            reply = json.loads(data)
            choice = (reply.get("choices") or [{}])[0]
            text = _THINK.sub("", _text((choice.get("message") or {}).get("content")))
            if choice.get("finish_reason") not in ("stop", "length") or not text.strip():
                raise ValueError("summary response did not complete")
            info["completed"] = True
            info["response_model"] = reply.get("model")
            return text
        except Exception:
            info["completed"] = False
            raise
        finally:
            connection.close()
            info["seconds"] = round(time.time() - info["t"], 3)
            if self.record:
                self.record(info)
