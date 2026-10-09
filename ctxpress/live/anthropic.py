"""The Anthropic Messages API (Claude Code) as a host.

A /v1/messages body keeps tool calls, tool results and thinking as blocks inside user / assistant messages. Each
block becomes one flat item of the kind ctxpress.live.rewrite already handles (text -> message, tool_use ->
function_call, tool_result -> function_call_output, thinking -> reasoning, anything else kept as is), the shared
Rewriter applies the method, and the result is assembled back into a valid Messages body:
  - roles alternate and the conversation starts with a user message (adjacent same-role messages are merged);
  - every tool_use is answered in the next user message, whose tool_result blocks come first (the shared pairing
    check already drops a call or result whose partner is gone);
  - the last assistant message keeps its thinking blocks and their order (the API requires them in a tool loop);
    thinking from earlier turns may be dropped by a method, as the API ignores it anyway;
  - cache_control is taken from the request being sent, not from history, because Claude Code moves its cache
    breakpoints every turn; it is left out of the history keys so moving it is not a history revision.
Method instructions go to the end of `system`; summaries the method wrote become text blocks of their role.
"""
from __future__ import annotations
import copy, hashlib, json, time

TEXT, TOOL_USE, TOOL_RESULT = "text", "tool_use", "tool_result"
THINKING = ("thinking", "redacted_thinking")


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == TEXT)
    return ""


def _strip_cache(block):
    if isinstance(block, dict) and "cache_control" in block:
        block = {k: v for k, v in block.items() if k != "cache_control"}
    if isinstance(block, dict) and isinstance(block.get("content"), list):
        block = dict(block, content=[_strip_cache(b) for b in block["content"]])
    return block


def blocks(message):
    content = message.get("content")
    return [{"type": TEXT, "text": content}] if isinstance(content, str) else list(content or [])


def flatten(body):
    """Flat items for the shared Rewriter, and for each the (message index, block index) it came from."""
    items, refs = [], []
    for i, message in enumerate(body.get("messages") or []):
        role = message.get("role")
        for j, raw in enumerate(blocks(message)):
            b = _strip_cache(raw)
            t = b.get("type") if isinstance(b, dict) else None
            if t == TEXT:
                item = {"type": "message", "role": role, "content": [{"type": "input_text", "text": b.get("text", "")}]}
            elif t == TOOL_USE:
                item = {"type": "function_call", "call_id": b.get("id"), "name": b.get("name"),
                        "arguments": json.dumps(b.get("input", {}), ensure_ascii=False, separators=(",", ":"))}
            elif t == TOOL_RESULT:
                content = b.get("content")
                media = isinstance(content, list) and any(isinstance(x, dict) and x.get("type") != TEXT for x in content)
                item = {"type": "function_call_output", "call_id": b.get("tool_use_id"), "output": _text(content)}
                if media:
                    item["media"] = [x for x in content if isinstance(x, dict) and x.get("type") != TEXT]
                if b.get("is_error"):
                    item["is_error"] = True
            elif t in THINKING:
                item = {"type": "reasoning", "summary": [{"type": "summary_text", "text": b.get("thinking", "")}],
                        "anthropic": b}
            else:                                         # images, documents, server tools: carried unchanged
                item = {"type": f"anthropic_{t}", "block": b, "role": role}
                if t in ("image", "document"):
                    item["content"] = [{"type": "input_image"}]          # marks media for summaries
            items.append(item); refs.append((i, j))
    return items, refs


def session_key(body):
    """Main conversation, sub-agents and side calls (titles, small-model checks) are separate conversations: the
    model and the first message identify one; the metadata user_id carries Claude Code's session."""
    messages = body.get("messages") or []
    first = json.dumps(_strip_cache(messages[0]) if messages else None, sort_keys=True, ensure_ascii=False)
    user = ((body.get("metadata") or {}).get("user_id")) or ""
    return "anthropic:" + hashlib.sha256(f"{user}\x00{body.get('model')}\x00{first}".encode()).hexdigest()[:32]


def rewritable(body):
    """Only agent turns (with tools) are managed; one-shot side requests pass through."""
    return isinstance(body.get("messages"), list) and bool(body.get("tools"))


def unflatten(body, refs, flat, out):
    """Assemble the rewritten flat items `out` (from `flat` + method summaries / instructions) into a Messages body."""
    index = {id(item): k for k, item in enumerate(flat)}
    original = body.get("messages") or []
    system_extra, messages = [], []

    def add(role, block, src=None):
        if not (messages and messages[-1]["role"] == role):
            messages.append({"role": role, "content": []})
        messages[-1]["content"].append((block, src))

    last_assistant = max((i for i, m in enumerate(original) if m.get("role") == "assistant"), default=None)
    for item in out:
        k = index.get(id(item))
        if k is None and item.get("type") == "function_call_output":     # a rewritten copy of an output
            k = next((n for n, x in enumerate(flat) if x.get("type") == "function_call_output"
                      and x.get("call_id") == item.get("call_id")), None)
        if k is None:                                                     # written by the method
            role, text = item.get("role", "user"), _text([{"type": TEXT, "text": p.get("text", "")} for p in item.get("content") or []])
            if role in ("developer", "system"):
                system_extra.append(text)
            else:
                add(role, {"type": TEXT, "text": text})
            continue
        i, j = refs[k]
        src = blocks(original[i])[j]
        role = original[i].get("role")
        block = copy.deepcopy(src)
        if src.get("type") == TOOL_RESULT and item is not flat[k]:
            text = item.get("output", "")
            media = flat[k].get("media") or []
            block["content"] = [{"type": TEXT, "text": text}, *media] if media else text
        add(role, block, i)
    final = []
    for m in messages:
        content = m["content"]
        if m["role"] == "assistant":
            sources = {src for _, src in content if src is not None}
            if last_assistant in sources:
                # the last assistant turn keeps all its thinking, first and in order (even if a method removed it)
                own = [b for j, b in enumerate(blocks(original[last_assistant])) if b.get("type") in THINKING]
                rest = [b for b, src in content if b.get("type") not in THINKING]
                content = own + rest
            elif len(sources) > 1:                                         # merged earlier turns: stale thinking goes
                content = [b for b, src in content if b.get("type") not in THINKING] or [b for b, _ in content]
            else:
                content = [b for b, _ in content]
        else:                                                              # tool results first in a user message
            content = sorted((b for b, _ in content), key=lambda b: b.get("type") != TOOL_RESULT)
        final.append({"role": m["role"], "content": content})
    if final and final[0]["role"] != "user":
        final.insert(0, {"role": "user", "content": [{"type": TEXT, "text": "(earlier conversation omitted)"}]})
    result = dict(body, messages=[m for m in final if m["content"]])
    if system_extra:
        system = body.get("system")
        extra = [{"type": TEXT, "text": t} for t in system_extra]
        result["system"] = ([{"type": TEXT, "text": system}] if isinstance(system, str) else list(system or [])) + extra
    return result


def rewrite_messages(rewriter, body, key=None, summarizer=None):
    """(new body, info) with the method applied, or (body, None) when the request is not an agent turn."""
    if not rewritable(body):
        return body, None
    flat, refs = flatten(body)
    rewritten, info = rewriter.rewrite_body({"input": flat}, key or session_key(body), summarizer=summarizer,
                                         allow_forks=True)
    if info is None:
        return body, None
    return unflatten(body, refs, flat, rewritten["input"]), info


def find_model(text):
    """The model a Messages response reports (message_start in a stream, or the JSON body)."""
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except ValueError:
                continue
            if d.get("type") == "message_start":
                model = (d.get("message") or {}).get("model")
                return model if isinstance(model, str) else None
    try:
        model = json.loads(text).get("model")
    except (ValueError, AttributeError):
        return None
    return model if isinstance(model, str) else None


def normalized_usage(u):
    """Anthropic usage -> the shared fields: input counts uncached, cache reads and cache writes together."""
    if not isinstance(u, dict):
        return None
    parts = [u.get("input_tokens"), u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0]
    total = sum(parts) if all(isinstance(p, int) for p in parts) else None
    return dict(input_tokens=total, cached_tokens=u.get("cache_read_input_tokens") or 0,
                cache_write_tokens=u.get("cache_creation_input_tokens") or 0, output_tokens=u.get("output_tokens"),
                reasoning_tokens=None)


def find_usage(text):
    """Usage of an Anthropic SSE stream (message_start, then message_delta's output count) or JSON body."""
    usage = None
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            d = json.loads(line[5:].strip())
        except ValueError:
            continue
        if d.get("type") == "message_start":
            usage = dict((d.get("message") or {}).get("usage") or {})
        elif d.get("type") == "message_delta" and usage is not None:
            usage.update({k: v for k, v in (d.get("usage") or {}).items() if v is not None})
    if usage is None:
        try:
            usage = json.loads(text).get("usage")
        except (ValueError, AttributeError):
            usage = None
    return normalized_usage(usage)


class MessagesSummarizer:
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
        payload = dict(model=model, max_tokens=8192, system=system, stream=False,
                       messages=[{"role": "user", "content": user}])
        raw = json.dumps(payload, ensure_ascii=False).encode()
        headers = dict(self.headers, **{"Content-Length": str(len(raw)), "Accept-Encoding": "identity"})
        info = dict(type="summary", purpose=purpose, t=time.time(), model=model)
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
            text = _text(reply.get("content"))
            if reply.get("stop_reason") not in ("end_turn", "stop_sequence", "max_tokens") or not text.strip():
                raise ValueError("summary response did not complete")
            info["completed"] = True
            info["response_model"] = reply.get("model")
            return text
        except Exception as error:
            info["completed"] = False
            from ctxpress.live.summarize import summary_error
            info["error"] = summary_error(error)
            raise
        finally:
            connection.close()
            info["seconds"] = round(time.time() - info["t"], 3)
            if self.record:
                self.record(info)
