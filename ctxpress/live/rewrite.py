"""Apply a context-management method to real model requests.

`Rewriter` keeps one LiveContext per agent session. For every Responses-API request the harness sends
(`body["input"]`, the full history), it
  1. maps each input item to a context item (messages, reasoning, tool calls, tool outputs), adding the ones
     it has not seen before, in order; when the host revises earlier history, the active method state is
     rebuilt from that history while original-output archives remain retrievable,
  2. lets the method act (`LiveContext.before_request`): placeholders, truncation, structured compression,
     deletion of whole call/output pairs, dedicated memory,
  3. writes the result back into the request: changed outputs get their new text, removed items are dropped,
     and a summary the method wrote is inserted where the items it replaced were (as a user message unless
     the method gave it another role).
Call/output pairing and every item the method did not touch are kept byte-for-byte, so the harness's own
behaviour (reasoning items, compaction items, instructions) is unchanged. Each context item also carries how the
host sends it (type, tool name and raw arguments, serialized size with images priced by dimensions), so methods
that work on the request as sent (CliffCompaction's size trigger and summary) see exactly what the host sees.
Summary methods use an optional common callback (the proxy supplies a service using the current upstream
model). With no service, model-written summaries leave history intact for the harness's own compaction.
"""
from __future__ import annotations
import copy, hashlib, json, os, time, threading
from ctxpress.live.context import LiveContext
from ctxpress.core.params import DEFAULT
from ctxpress.core.hostsize import billable_chars

OUT_TYPES = ("function_call_output", "custom_tool_call_output")
CALL_TYPES = ("function_call", "custom_tool_call")


def operation_counts(ctx):
    return {key[3:]: value for key, value in ctx.M.items() if key.startswith("op_")}


def operation_delta(ctx, before):
    return {key: value - before.get(key, 0) for key, value in operation_counts(ctx).items()
            if value > before.get(key, 0)}


def item_view(items):
    """Per-item digests and serialized sizes: what the next request's prompt prefix can share with this one."""
    dumped = [json.dumps(x, ensure_ascii=False, sort_keys=True) for x in items]
    return [hashlib.sha256(d.encode("utf-8")).hexdigest()[:16] for d in dumped], [len(d) for d in dumped]


def prefix_change(previous, digests, sizes):
    """How much of the previous request's items this request repeats unchanged from the start (None for the first)."""
    if previous is None:
        return None
    kept = 0
    for a, b in zip(previous, digests):
        if a != b:
            break
        kept += 1
    return dict(previous_items=len(previous), kept_items=kept, kept_tokens=sum(sizes[:kept]) // 4,
                edited=kept < len(previous))


def composition(items, sizes):
    """Estimated tokens (serialized chars / 4) by kind of item."""
    out = {}
    for item, size in zip(items, sizes):
        t = item.get("type") or ("message" if "role" in item else "other")
        if t == "message":
            role = item.get("role")
            kind = "instructions" if role in ("developer", "system") else role if role in ("user", "assistant") else "other"
        elif t in CALL_TYPES or (t.endswith("_call") and t != "reasoning"):
            kind = "tool_call"
        elif t in OUT_TYPES or t.endswith("_call_output"):
            kind = "tool_output"
        else:
            kind = "reasoning" if t == "reasoning" else "other"
        out[kind] = out.get(kind, 0) + size // 4
    return out


PROMPT_PARTS = ("model", "instructions", "tools")   # the non-history part of a Responses prompt prefix


def fixed_digest(body):
    """Digest of the prompt parts before the history (model, instructions, tools). Per-turn metadata such as
    client_metadata is not part of the cached prompt and is left out."""
    rest = {k: body[k] for k in PROMPT_PARTS if k in (body or {})}
    if not rest:                                   # only the history was given (e.g. the Anthropic adapter)
        return None
    return hashlib.sha256(json.dumps(rest, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


TEXT_PARTS = ("input_text", "output_text", "text")


def content_text(c):
    """Text of a Responses content value: a string, or the text parts of a list joined by newlines."""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") in TEXT_PARTS)
    return ""


def output_text(item):
    o = item.get("output")
    if isinstance(o, dict):                       # {"content": ...} / {"body": ...} variants
        o = o.get("content", o.get("body", ""))
    if isinstance(o, (str, list)):
        return content_text(o)
    return json.dumps(o) if o is not None else ""


def set_output_text(item, text):
    o = item.get("output")
    if isinstance(o, list):
        first = True
        new = []
        for x in o:
            if isinstance(x, dict) and x.get("type") in ("input_text", "output_text", "text"):
                if first:
                    new.append(dict(x, text=text)); first = False
                continue
            new.append(x)                          # images etc. stay
        if first:
            new.insert(0, {"type": "input_text", "text": text})
        item["output"] = new
    else:
        item["output"] = text


def message_text(item):
    return content_text(item.get("content"))


def reasoning_text(item):
    return "\n".join(p.get("text", "") for p in item.get("summary") or [] if isinstance(p, dict)).strip()


def has_media(item):
    """Opaque media must survive text-only model summaries."""
    if isinstance(item, dict):
        if item.get("type") in ("input_image", "output_image", "image", "input_audio", "output_audio", "input_file"):
            return True
        return any(has_media(value) for value in item.values())
    return isinstance(item, list) and any(has_media(value) for value in item)


class ResponsesHost:
    """The OpenAI Responses API as the host: how a method-written message is sent and how big it is."""
    @staticmethod
    def message(text, role="user"):
        part = "output_text" if role == "assistant" else "input_text"       # assistant content is model output
        return {"type": "message", "role": role, "content": [{"type": part, "text": text}]}

    def message_chars(self, text):
        return billable_chars(self.message(text))


def history_keys(items):
    """Content identities with occurrence numbers: equal messages are distinct items.

    Full item fingerprints also notice changed call arguments and output text even
    when a host reuses the same call_id. Keep only digests, not a second raw history.
    """
    counts, keys = {}, []
    for item in items:
        encoded = json.dumps(item, sort_keys=True, ensure_ascii=False).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        occurrence = counts.get(digest, 0)
        keys.append((digest, occurrence))
        counts[digest] = occurrence + 1
    return keys


class Session:
    def __init__(self, method_factory, params, store_dir=None, store_prefix=None, store_namespace=None, retrieve_tool=None,
                 summarizer=None):
        self.ctx = LiveContext(method_factory(), params, store_dir=store_dir, store_prefix=store_prefix,
                               store_namespace=store_namespace, retrieve_tool=retrieve_tool, summarizer=summarizer)
        self.ids = {}                    # occurrence key -> context item id
        self.history = []
        self.history_epoch = 0
        self.n = 0
        self.host = ResponsesHost()
        self.lock = threading.RLock()
        self.saved = None                # (history, ids, ctx, views) before the last request: undoes a one-off fork
        self.forks = 0
        # Digests of the last request as received (host) and as sent (after the method), and of its fixed part.
        self.views = (None, None, None)
        self.compared = None             # the views this request was compared against (a length retry reuses them)

    def rewrite(self, inp, body=None, *, allow_forks=False):
        started = time.perf_counter()
        ctx = self.ctx
        keys = history_keys(inp)
        rebased = keys[:len(self.history)] != self.history
        before = self.saved[0] if self.saved is not None else []
        if (allow_forks and rebased and before and len(self.history) > len(before) and self.history[:len(before)] == before
                and keys[:len(before)] == before):
            # The last request was a one-off fork of the conversation (Claude Code's next-prompt suggestion: the
            # whole history plus one instruction, its answer discarded). Continue from the state before it.
            self.history, self.ids, self.ctx, self.views = self.saved
            ctx, rebased = self.ctx, False
            self.forks += 1
        if rebased:
            # The host has superseded our active history. A fresh method epoch
            # prevents obsolete summaries, eviction indexes and pinning from
            # leaking into the new history. Archives keep their original IDs.
            ctx.reset_history()
            self.ids.clear()
            self.history_epoch += 1
        # Only the Anthropic adapter opts into its suggestion-fork heuristic.
        # Responses history revisions must rebuild state, and ordinary methods
        # may own runtime resources (locks, clients) that cannot be deep-copied.
        self.saved = ((list(self.history), dict(self.ids),
                       copy.deepcopy(ctx, {id(ctx.store): ctx.store, id(ctx.summarizer): ctx.summarizer}), self.views)
                      if allow_forks else None)
        meters = ("pruner_calls", "pruner_input_tokens", "pruner_usage_unknown")
        previous_usage = {key: ctx.M[key] for key in meters}
        before_operations = operation_counts(ctx)
        ctx.host = self.host
        ctx.n_input = len(inp)
        if body is not None:
            ctx.fixed_chars = billable_chars(dict(body, input=[]))
        for k, item in zip(keys, inp):
            if k in self.ids:
                continue
            t = item.get("type") or ("message" if "role" in item else None)
            host = dict(htype=t, chars=billable_chars(item), has_media=has_media(item))
            if t in CALL_TYPES:
                text = item.get("input") or item.get("arguments") or ""
                self.ids[k] = ctx.add_call(item.get("call_id"), text, name=item.get("name") or t,
                                           args=item.get("arguments", item.get("input", "")),
                                           wire_arguments=item.get("arguments", ""), **host)
            elif t in OUT_TYPES:
                call = ctx.calls.get(item.get("call_id"))
                metadata = item.get("internal_chat_message_metadata_passthrough") or {}
                if (call is not None and call.get("name") in ("exec", "functions.exec")
                        and isinstance(metadata, dict) and metadata.get("cell_id") == item.get("call_id")):
                    # A synchronous Code Mode output belongs to this complete
                    # call/output pair. A yielded cell's final wait belongs to
                    # a different pair; don't guess its cross-pair boundaries.
                    call["executed_tool_calls"] = copy.deepcopy(metadata.get("executed_tool_calls"))
                    call["executed_tool_calls_complete"] = metadata.get("tool_calls_complete") is True
                self.ids[k] = ctx.add_output(item.get("call_id"), output_text(item), **host)
            elif t == "message":
                self.ids[k] = ctx.add_message(item.get("role"), message_text(item),
                                              fixed=item.get("role") in ("developer", "system"), **host)
            else:                                  # reasoning, compaction, anything else: kept as is
                if t and t != "reasoning" and t.endswith("_call"):
                    host.update(name=item.get("name") or t, args=item.get("arguments", ""))
                text = reasoning_text(item) if t == "reasoning" else None
                self.ids[k] = ctx.add_other(t, host["chars"] // 4, text=text, **host)
        self.history = keys
        order = ctx.before_request()
        out, changed, dropped = self.render(inp, keys, order)
        self.n += 1
        self.compared = self.views
        info = dict(host_contract=self.host_contract, request=self.n, history_rebased=rebased, history_epoch=self.history_epoch, forks_undone=self.forks,
                    items=len(inp), kept=len(out), changed=changed, dropped=dropped,
                    tokens_before=sum(len(json.dumps(x, ensure_ascii=False)) for x in inp) // 4,
                    tokens_after=sum(len(json.dumps(x, ensure_ascii=False)) for x in out) // 4,
                    forms=ctx.form_counts(), method_overhead_estimate=ctx.last_overhead_estimate,
                    operations=operation_delta(ctx, before_operations),
                    **{key: ctx.M[key] - previous_usage[key] for key in meters})
        received = item_view(inp)[0]
        info.update(self.process(received, out, fixed_digest(body)), rewrite_seconds=round(time.perf_counter() - started, 6))
        return out, info

    def process(self, received, out, fixed):
        """Process telemetry for one rendered request: prefix kept against the previous request (as received
        from the host and as sent after the method), whether the fixed part changed, and the sent composition."""
        last_in, last_out, last_fixed = self.compared
        sent, sizes = item_view(out)
        self.views = (received, sent, fixed)
        host = prefix_change(last_in, received, [0] * len(received))
        if host is not None:
            del host["kept_tokens"]                    # sizes of received items are not the cacheable prompt
        return dict(prefix=prefix_change(last_out, sent, sizes), host_prefix=host,
                    fixed_changed=None if last_fixed is None or fixed is None else fixed != last_fixed,
                    composition=composition(out, sizes))

    def render(self, inp, keys=None, order=None):
        """Render an existing view without ingesting history or advancing a model turn."""
        ctx = self.ctx
        keys = history_keys(inp) if keys is None else keys
        order = ctx.view() if order is None else order
        view = {v["id"]: v for v in order}
        # summaries the method wrote go just before the next context item that came from the request
        before, pending = {}, []
        for v in order:
            if v["seg"] == "summary":
                pending.append(v)
            elif pending:
                before[v["id"]] = pending; pending = []
        out, changed, dropped = [], 0, 0
        for k, item in zip(keys, inp):
            cid = self.ids.get(k)
            v = view.get(cid)
            if v is None:
                dropped += 1
                continue
            for sv in before.pop(cid, []):
                out.append(self.host.message(sv["text"], role=sv.get("role") or "user")); changed += 1
            if item.get("type") in OUT_TYPES and (v["form"] != "full" or v.get("rewritten")):
                item = dict(item); set_output_text(item, v["text"]); changed += 1
            out.append(item)
        for sv in pending:
            out.append(self.host.message(sv["text"], role=sv.get("role") or "user")); changed += 1
        instr = getattr(ctx.method, "instructions", None)
        if instr:                                  # what the method asks the agent to do (same text every request)
            at = 0
            while at < len(out) and out[at].get("type", "message") == "message" and out[at].get("role") in ("developer", "system"):
                at += 1
            out.insert(at, self.host.message(instr, role="developer"))
        out = pair_check(out)
        from ctxpress.live.contract import validate
        self.host_contract = validate(inp, out)
        return out, changed, dropped


def pair_check(items):
    """Drop any call without its output and any output without its call (the API rejects unpaired items)."""
    calls = {x.get("call_id") for x in items if x.get("type") in CALL_TYPES}
    outs = {x.get("call_id") for x in items if x.get("type") in OUT_TYPES}
    keep = calls & outs
    return [x for x in items if x.get("type") not in CALL_TYPES + OUT_TYPES or x.get("call_id") in keep]


class Rewriter:
    """One Session per conversation (keyed by the request's prompt_cache_key / session header)."""
    def __init__(self, method_factory, params=DEFAULT, log=None, store_dir=None, store_prefix=None, retrieve_tool=None,
                 summarizer=None):
        self.factory, self.params, self.sessions = method_factory, params, {}
        self.log = log
        self.store_dir, self.store_prefix = store_dir, store_prefix
        self.retrieve_tool = retrieve_tool
        self.summarizer = summarizer
        self.lock = threading.Lock()

    def rewrite_body(self, body, session_key, summarizer=None, *, allow_forks=False):
        inp = body.get("input")
        if not isinstance(inp, list):
            return body, None
        with self.lock:
            s = self.sessions.get(session_key)
            if s is None:
                namespace = hashlib.sha256(str(session_key).encode()).hexdigest()[:32] if self.store_dir else None
                directory = os.path.join(self.store_dir, namespace) if namespace else None
                prefix = (self.store_prefix or self.store_dir)
                prefix = prefix.rstrip("/\\") + "/" + namespace if namespace else prefix
                s = self.sessions[session_key] = Session(self.factory, self.params, directory, prefix, namespace, self.retrieve_tool,
                                                         self.summarizer)
        with s.lock:
            previous = s.ctx.summarizer
            if summarizer is not None:
                s.ctx.summarizer = summarizer
            try:
                new, info = s.rewrite(inp, body, allow_forks=allow_forks)
                info["summary_failed"] = s.ctx.M["summary_failed"]
                info["summary_skipped"] = s.ctx.M["summary_skipped"]
                info["summary_guidance_skipped"] = s.ctx.M["summary_guidance_skipped"]
            finally:
                s.ctx.summarizer = previous
        body = dict(body, input=new)
        info.update(session=session_key, t=time.time())
        if self.log:
            with open(self.log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(info, ensure_ascii=False) + "\n")
        return body, info

    def retry_body(self, original, rejected, info, summarizer=None):
        """Offer an explicit length rejection to the method for this exact request.

        A newer request for the same session invalidates this ticket. Retry
        rendering uses the existing context and original identities, so it
        cannot ingest the method's own generated summary as user history.
        """
        with self.lock:
            session = self.sessions.get(info["session"])
        if session is None:
            return None
        with session.lock:
            if session.n != info["request"] or session.history != history_keys(original["input"]):
                return None
            ctx = session.ctx
            meters = ("pruner_calls", "pruner_input_tokens", "pruner_usage_unknown")
            before_usage = {key: ctx.M[key] for key in meters}
            before_operations = operation_counts(ctx)
            previous = ctx.summarizer
            if summarizer is not None:
                ctx.summarizer = summarizer
            try:
                if not ctx.method.on_overflow(ctx, rejected):
                    return None
                out, changed, dropped = session.render(original["input"])
            finally:
                ctx.summarizer = previous
            body = dict(original, input=out)
            if body == rejected:
                return None
            overhead = ctx.method.overhead(ctx, max(0,ctx.r-1))
            ctx.last_overhead_estimate = overhead
            if overhead:
                ctx.M["overhead"] += overhead
                ctx.M["cost"] += overhead
            process = session.process(session.views[0], out, session.views[2])
            updated = dict(info, **process, host_contract=session.host_contract, kept=len(out), changed=changed, dropped=dropped, forms=ctx.form_counts(),
                operations=operation_delta(ctx, before_operations),
                tokens_after=sum(len(json.dumps(x, ensure_ascii=False)) for x in out)//4,
                summary_failed=ctx.M["summary_failed"], summary_skipped=ctx.M["summary_skipped"],
                summary_guidance_skipped=ctx.M["summary_guidance_skipped"],
                method_overhead_estimate=overhead, t=time.time(), **{key:ctx.M[key]-before_usage[key] for key in meters})
            return body, updated
