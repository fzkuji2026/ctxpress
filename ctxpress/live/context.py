"""Use a method on a live conversation: the same rules and text operations the simulator evaluates, applied to
real items (ctxpress.live.rewrite feeds it the requests a harness sends; tests feed it directly).

    ctx = LiveContext(ComplexityTrap(10))
    ctx.add_message("user", task, fixed=True)
    ctx.add_call("c1", 'sed -n 1,200p a/main.go')
    ctx.add_output("c1", output_text)
    view = ctx.before_request()      # [{id, seg, role, call_id, form, text}] in order; text is the rendered content

Live bookkeeping that the replay gets from the trace:
  - `last` of an item is updated when a later call touches the same file (it is "in use");
  - a call that reads a file whose earlier output is no longer in context in full is a fault (method.on_fault);
  - fixed items (system / developer instructions, the task) are never offered to the method.
Dedicated memory: with `store_dir`, the original text of every output is written to <store_dir>/<id>.txt and a
memory placeholder tells the agent where it is (`store_prefix` = the same directory as seen by the agent), so
retrieval is an ordinary file read and needs no extra tool.
Sizes are tokens (ctxpress.core.sizes): each item's text counted with o200k_base when it arrives (`content`), its
framing and encrypted parts fitted from what the provider billed (`sizes`, one model per requested model), and a
request estimated from the last answered one. Summaries need a model; a method's summarize step uses `summarizer(items) -> str` if given, otherwise it is
skipped (the harness's own compaction is expected to handle the window).
"""
from __future__ import annotations
import os, time
import copy
from ctxpress.core import sizes as sizemodel, tokens
from ctxpress.core.engine import Simulator
from ctxpress.core.params import DEFAULT
from ctxpress.core.trace import classify, sub_of, TEST, PATH, norm, patch_anchors, unescape


class LiveContext(Simulator):
    def __init__(self, method, params=DEFAULT, prefix_tokens=0, summarizer=None, store_dir=None, store_prefix=None,
                 store_namespace=None, retrieve_tool=None, size_models=None):
        super().__init__(dict(name="live", reqs=[], prefix=prefix_tokens), method, params)
        self.base_prefix = prefix_tokens
        self.window = None if getattr(method, "unbounded", False) else self.window
        self.summarizer = summarizer
        self.size_models = {} if size_models is None else size_models   # requested model -> SizeModel
        self.request_model = None
        self.fixed = 0                   # counted tokens of the request apart from its input (instructions, tools)
        self._order = (None, None, {})
        self.store_dir, self.store_prefix = store_dir, store_prefix or store_dir
        self.store_namespace = store_namespace
        self.retrieve_tool = bool(os.environ.get("CTXPRESS_MCP")) if retrieve_tool is None else retrieve_tool
        self.store = {}
        self.calls = {}
        self.pinned = []                 # fixed items: rendered, never offered to the method
        self.r = 0
        self.last_overhead_estimate = 0.0
        if hasattr(method, "reset"):
            method.reset(self)

    # ---------------------------------------------------------------- adding content
    def _pin(self, seg):
        s = self.mark(self.new(seg)); self.pinned.append(s); self.prefix += s["size"]
        return s

    def sent(self):
        return self.pinned + self.ctx

    # Keyword arguments `host` (all optional) describe the item as the host sends it: htype (its type in the
    # host's API), skind / content / enc (sizes.parts of the wire item: its size kind, counted tokens, encrypted
    # characters), media (tokens of its images), name / args (a call's tool name and raw arguments).

    def add_message(self, role, text, fixed=False, **host):
        host.setdefault("skind", "user" if role == "user" else "message")
        host.setdefault("content", tokens.count(text))
        seg = dict(seg="msg", size=0, role=role, text=text, t=time.time(), **host)
        if fixed:
            seg["protected"] = True
        head = self.turn == 0                                 # nothing from the model yet: instructions and the task
        if head and (fixed or (role == "user" and not any(x.get("role") == "user" for x in self.pinned + self.ctx))):
            return self._pin(seg)["id"]
        return self.add(seg)["id"]

    def add_other(self, kind, text=None, **host):
        """Items methods leave alone (reasoning, the harness's own compaction items); `text`: readable part."""
        host.setdefault("skind", kind if kind in ("reasoning", "compaction") else "other")
        host.setdefault("content", tokens.count(text or ""))
        seg = dict(seg="reason", size=0, kind=kind, t=time.time(), **host)
        if kind in ("compaction", "additional_tools"):
            # Host state and executable tool declarations must survive compression.
            seg["protected"] = True
        if text is not None:
            seg["text"] = text
        return self.add(seg)["id"]

    def add_call(self, call_id, text, kind=None, res=None, name=None, **host):
        k, rs = classify(text)
        kind, res = kind or k, sorted(res if res is not None else rs)
        host.setdefault("skind", "call")
        host.setdefault("content", tokens.count(name or "") + tokens.count(text))
        seg = dict(seg="call", size=0, kind=kind, res=res, text=text, call_id=call_id, name=name,
                   t=time.time(), **host)
        if kind == "edit":
            seg["anchors"] = {p: patch_anchors(text, p) for p in res}
        self.calls[call_id] = seg
        touched = set(res)
        for s in self.ctx:                                   # live "needs": the files this call touches are in use
            if touched & set(s.get("res", [])):
                if s.get("form", "full") != "full" and s["seg"] == "out" and kind in ("read", "spec", "edit"):
                    self.M["faults"] += 1
                    if hasattr(self.method, "on_fault"):
                        self.method.on_fault(self, s)
                s["last"] = self.r
        stored = self.add(seg)
        # Simulator.add creates the actual retained segment. Later host
        # observations must update that same object, including method history.
        self.calls[call_id] = stored
        return stored["id"]

    def add_output(self, call_id, text, dur=None, **host):
        c = self.calls.get(call_id, dict(kind="other", res=[], text=""))
        outp = sorted({norm(m.group(1)) for m in PATH.finditer(text[:20000])})[:200] if c["kind"] in ("search", "command") else []
        host.setdefault("skind", "output")
        host.setdefault("content", tokens.count(text) + host.get("media", 0))
        seg = dict(seg="out", size=0, kind=c["kind"], res=c["res"], outpaths=outp, sub=sub_of(c["kind"], c["res"]),
                   test=bool(c["kind"] == "command" and TEST.search(unescape(c.get("text", "")))), text=text, call_id=call_id, dur=dur, t=time.time(),
                   call_name=c.get("name"), call_text=c.get("text", ""), **host)
        s = self.add(seg)
        self.store[s["id"]] = text
        if self.store_dir:
            os.makedirs(self.store_dir, exist_ok=True)
            with open(os.path.join(self.store_dir, f"{s['id']}.txt"), "w", encoding="utf-8") as fh:
                fh.write(text)
        return s["id"]

    def reset_history(self):
        """Start a method epoch for revised host history; retain immutable archives.

        Internal ages, summary membership and reuse indexes belong to the old
        history. Lifetime meters and positive archive IDs belong to the session.
        The same method configuration and current summary service are reused.
        """
        metrics, store, next_id = self.M, self.store, self._nid
        model, fixed = self.request_model, self.fixed
        self.__init__(self.method, self.P, prefix_tokens=self.base_prefix,
                      summarizer=self.summarizer,
                      store_dir=self.store_dir, store_prefix=self.store_prefix,
                      store_namespace=self.store_namespace, retrieve_tool=self.retrieve_tool,
                      size_models=self.size_models)
        self.M, self.store, self._nid = metrics, store, next_id
        self.request_model, self.fixed = model, fixed
        self.M["history_rebases"] += 1

    # ---------------------------------------------------------------- sizes (tokens)
    @property
    def sizes(self):
        return self.size_models.setdefault(self.request_model or "", sizemodel.SizeModel())

    @staticmethod
    def _row_kind(s):
        """Size kind of an item as sent (sizes.KINDS, "user" for a user message)."""
        if s["seg"] == "summary" or s.get("virtual"):
            return "user" if (s.get("role") or "user") == "user" else "message"
        if s.get("skind"):
            return s["skind"]
        if s["seg"] == "msg":
            return "user" if s.get("role") == "user" else "message"
        return {"out": "output", "call": "call"}.get(s["seg"], "other")

    def _positions(self):
        """{item id: position} in the request and the position of its last user message (memoized on the order)."""
        items = self.pinned + self.ctx
        key = (len(items), items[0]["id"] if items else None, items[-1]["id"] if items else None, self._seg_edits)
        if self._order[0] != key:
            pos = {x["id"]: i for i, x in enumerate(items)}
            last = max((i for i, x in enumerate(items) if self._row_kind(x) == "user"), default=-1)
            self._order = (key, last, pos)
        return self._order[2], self._order[1]

    def overhead(self, s):
        kind = self._row_kind(s)
        kind = "message" if kind == "user" else kind
        if kind == "reasoning":
            pos, last = self._positions()
            if pos.get(s["id"], last) < last:
                kind = "reasoning_old"
        return self.sizes.overhead(kind, s.get("enc", 0))

    def kept_suffix(self, s):
        where = f"{self.store_prefix}/{s['id']}.txt" if self.store_prefix else f"#{s['id']}"
        return f"\n[Full output stored at {where}.]" if self.memory else ""

    def _counted(self, s):
        """Counted tokens of an item as it would be sent now."""
        if s.get("virtual") or s["seg"] == "summary":
            return tokens.count(s.get("text", ""))
        f = s.get("form", "full")
        if f == "full" and not (s["seg"] == "out" and hasattr(self.method, "output_text")):
            return s["content"]
        return tokens.count(self.render(s)) + s.get("media", 0)

    def seg_size(self, s):
        return self._counted(s) + self.overhead(s)

    def message_tokens(self, text):
        return tokens.count(text) + self.sizes.overhead("message")

    def request_tokens(self, items=None):
        """Estimated input tokens of the next request with `items` (default: what it would send now), the method's
        instructions message and the fixed part included: the last billed request plus the modelled difference."""
        items = self.sent() if items is None else items
        rows = [(self._row_kind(s), self._counted(s), s.get("enc", 0)) for s in items]
        from ctxpress.methods.experiment_context import instructions
        instr = instructions(self.method)
        if instr:
            at = next((i for i, s in enumerate(items) if s.get("role") not in ("developer", "system")), len(items))
            rows.insert(at, ("message", tokens.count(instr), 0))
        x, counted = sizemodel.features(rows)
        return self.sizes.estimate(x, counted + self.fixed)

    def size(self):
        return self.request_tokens()

    def measure_history(self, items):
        """Estimated input tokens of a request whose history after the fixed head is `items`."""
        return self.request_tokens(self.pinned + list(items))

    # ---------------------------------------------------------------- before each model request
    def before_request(self, t=None):
        self.t = t or time.time()
        self.method.step(self, self.r)
        if self.window and self.size() > self.window and self.summarizer:
            self.summarize()
        self.last_overhead_estimate = self.method.overhead(self, self.r)
        if self.last_overhead_estimate:
            self.M["overhead"] += self.last_overhead_estimate
            self.M["cost"] += self.last_overhead_estimate
        self.bill(self.ctx)
        self.r += 1
        return self.view()

    def summarize(self, keep=(), size=None, keep_user=False, bill_call=None, sid=None, guidance=None):
        if not self.summarizer:
            self.M["summary_skipped"] += 1                   # no model: leave the window to the harness
            return
        keep = list(keep)
        keep += [s for s in self.ctx if s.get("protected") or s.get("has_media") or s.get("role") in ("system", "developer")
                 or (keep_user and s["seg"] == "msg" and s.get("role") == "user")]
        paired = {s.get("call_id") for s in keep if s.get("call_id")}
        keep += [s for s in self.ctx if s.get("call_id") in paired]
        removed = [s for s in self.ctx if s not in keep]
        if not removed:
            return
        text = self._summary_text(removed, "history", self.P.get("summary_tokens") if size is None else size, guidance)
        if text is None:
            return
        super().summarize(keep=keep, size=size, keep_user=keep_user, bill_call=False, sid=sid)
        self.ctx[0]["text"] = text
        self.ctx[0]["size"] = self.message_tokens(text)

    def summarize_segment(self, items, size=None, text=None, billed=True, guidance=None, prompt=None, text_format=None,
                          **extra):
        """`prompt`: (system, user) the method built itself (a reproduction's original summary prompt, e.g. from
        the archived originals); the service sends it verbatim. `text_format`: wraps the model's text, with
        "{summary}" where it goes. Extra keys (e.g. role="assistant") are kept on the summary item."""
        if text is not None:                                  # the method wrote the summary itself
            new = super().summarize_segment(items, size, text=text, billed=billed, **extra)
            if new is not None:
                new["size"] = self.message_tokens(text)
            return new
        if not self.summarizer:
            self.M["summary_skipped"] += 1
            return None
        items = [s for s in items if s in self.ctx]
        protected = {s.get("call_id") for s in self.ctx if s.get("has_media") and s.get("call_id")}
        items = [s for s in items if not s.get("protected") and not s.get("has_media") and s.get("call_id") not in protected
                 and s.get("role") not in ("system", "developer")]
        if not items:
            return None
        if prompt is not None:
            text = self._prompt_text(prompt, "segment")
        else:
            target = size or max(self.P.get("segsummary_min"), int(self.P.get("segsummary_ratio") * sum(self.seg_size(s) for s in items)))
            text = self._summary_text(items, "segment", target, guidance)
        if text is None:
            return None
        if text_format is not None:
            text = text_format.replace("{summary}", text)
        new = super().summarize_segment(items, size, billed=billed, **extra)
        if new is not None:
            new["text"] = text; new["size"] = self.message_tokens(text)
        return new

    def _summary_text(self, items, purpose, budget, guidance=None):
        """Only commit after a successful result. Callbacks see the current representation."""
        rendered = [dict(copy.deepcopy(s), text=self.render(s)) for s in items]
        try:
            if guidance is not None and hasattr(self.summarizer, "summarize_with_guidance"):
                text = self.summarizer.summarize_with_guidance(rendered, purpose=purpose, budget=budget, guidance=guidance)
            elif hasattr(self.summarizer, "summarize"):
                if guidance is not None:
                    self.M["summary_guidance_skipped"] += 1
                text = self.summarizer.summarize(rendered, purpose=purpose, budget=budget)
            else:
                if guidance is not None:
                    self.M["summary_guidance_skipped"] += 1
                text = self.summarizer(rendered)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("summary must contain text")
        except Exception:
            self.M["summary_failed"] += 1
            return None
        return text

    def model_text(self, system, user, purpose="reflect", model=None):
        """Ask the summary service one question with a method's own prompt (e.g. AgentDiet's reflection) and
        return its text, or None when there is no service or the call fails (counted in summary_failed)."""
        if not self.summarizer:
            self.M["summary_skipped"] += 1
            return None
        return self._prompt_text((system, user), purpose, model)

    def _prompt_text(self, prompt, purpose, model=None):
        """A method's verbatim (system, user) prompt; services without `summarize_prompt` cannot honour it."""
        try:
            if purpose == "infinite_context_compression" and model == "deepseek-flash":
                from ctxpress.live.deepseek import summarize
                return summarize(*prompt, model=model, record=getattr(self.summarizer, "record", None))
            if not hasattr(self.summarizer, "summarize_prompt"):
                raise TypeError("summary service cannot send a method's own prompt")
            system, user = prompt
            kwargs = {} if model is None else {"model": model}
            text = self.summarizer.summarize_prompt(system, user, purpose=purpose, **kwargs)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("summary must contain text")
        except Exception:
            self.M["summary_failed"] += 1
            return None
        return text

    def compress_output(self, item, budget=None, guidance=None):
        """Layer 1 uses the same model service as layers 2 and 3."""
        if not self.summarizer:
            self.M["summary_skipped"] += 1
            return False
        if item.get("has_media") or item.get("protected"):
            return False
        text = self._summary_text([item], "observation", budget, guidance)
        if text is None:
            return False
        changed = self.keep_text(item, text)
        if changed:
            self.M["op_observation_summary"] += 1
        return changed

    def retrieve(self, item_id):
        return self.store.get(item_id)

    # ---------------------------------------------------------------- rendering
    def render(self, s):
        f = s.get("form", "full")
        if f == "full" and s["seg"] == "out" and hasattr(self.method, "output_text"):
            return self.method.output_text(s, s.get("text", ""))       # a method's own presentation (the original's)
        if f == "full" or s["seg"] == "summary":
            return s.get("text", "")
        if f in ("placeholder", "memid", "memlabel"):
            return self.placeholder_text(s, f)
        if f in ("truncated", "structured"):
            return (s.get("kept_text") or "") + self.kept_suffix(s)
        return ""

    def view(self):
        """`rewritten`: a full output the method presents differently (output_text), so the host request changes."""
        out = []
        for s in self.pinned + self.ctx:
            text = self.render(s)
            out.append(dict(id=s["id"], seg=s["seg"], role=s.get("role"), call_id=s.get("call_id"), form=s.get("form", "full"),
                            text=text, rewritten=s["seg"] == "out" and text != s.get("text", "")))
        return out

    def form_counts(self):
        c = {}
        for s in self.ctx:
            if s["seg"] == "out":
                c[s.get("form", "full")] = c.get(s.get("form", "full"), 0) + 1
        return c
