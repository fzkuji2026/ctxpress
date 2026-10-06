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
Summaries need a model; a method's summarize step uses `summarizer(items) -> str` if given, otherwise it is
skipped (the harness's own compaction is expected to handle the window).
"""
from __future__ import annotations
import os, time
import copy
from ctxpress.core.engine import Simulator
from ctxpress.core.params import DEFAULT
from ctxpress.core.trace import classify, sub_of, TEST, PATH, norm, patch_anchors, unescape


class LiveContext(Simulator):
    def __init__(self, method, params=DEFAULT, prefix_tokens=0, summarizer=None, tokens=None,
                 store_dir=None, store_prefix=None, store_namespace=None, retrieve_tool=None):
        super().__init__(dict(name="live", reqs=[], prefix=prefix_tokens, alpha=1.0), method, params)
        self.base_prefix = prefix_tokens
        self.window = None if getattr(method, "unbounded", False) else self.window
        self.summarizer = summarizer
        self.tokens = tokens or (lambda s: len(s) // 4 + 4)
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
    # host's API), chars (its serialized size, hostsize.billable_chars), name / args (a call's tool name and raw
    # arguments), raw_text (an output's content as the host serializes it, when it is not plain text).

    def add_message(self, role, text, fixed=False, **host):
        seg = dict(seg="msg", size=self.tokens(text), role=role, text=text, t=time.time(), **host)
        if fixed:
            seg["protected"] = True
        head = self.turn == 0                                 # nothing from the model yet: instructions and the task
        if head and (fixed or (role == "user" and not any(x.get("role") == "user" for x in self.pinned + self.ctx))):
            return self._pin(seg)["id"]
        return self.add(seg)["id"]

    def add_other(self, kind, size, text=None, **host):
        """Items methods leave alone (reasoning, the harness's own compaction items); `text`: readable part."""
        seg = dict(seg="reason", size=size, kind=kind, t=time.time(), **host)
        if kind in ("compaction", "additional_tools"):
            # Host state and executable tool declarations must survive compression.
            seg["protected"] = True
        if text is not None:
            seg["text"] = text
        return self.add(seg)["id"]

    def add_call(self, call_id, text, kind=None, res=None, name=None, **host):
        k, rs = classify(text)
        kind, res = kind or k, sorted(res if res is not None else rs)
        seg = dict(seg="call", size=self.tokens(text), kind=kind, res=res, text=text, call_id=call_id, name=name,
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
        seg = dict(seg="out", size=self.tokens(text), kind=c["kind"], res=c["res"], outpaths=outp, sub=sub_of(c["kind"], c["res"]),
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
        self.__init__(self.method, self.P, prefix_tokens=self.base_prefix,
                      summarizer=self.summarizer, tokens=self.tokens,
                      store_dir=self.store_dir, store_prefix=self.store_prefix,
                      store_namespace=self.store_namespace, retrieve_tool=self.retrieve_tool)
        self.M, self.store, self._nid = metrics, store, next_id
        self.M["history_rebases"] += 1

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
        self.ctx[0]["size"] = self.tokens(self.ctx[0]["text"])

    def summarize_segment(self, items, size=None, text=None, billed=True, guidance=None, prompt=None, text_format=None,
                          **extra):
        """`prompt`: (system, user) the method built itself (a reproduction's original summary prompt, e.g. from
        the archived originals); the service sends it verbatim. `text_format`: wraps the model's text, with
        "{summary}" where it goes. Extra keys (e.g. role="assistant") are kept on the summary item."""
        if text is not None:                                  # the method wrote the summary itself
            new = super().summarize_segment(items, size, text=text, billed=billed, **extra)
            if new is not None:
                new["size"] = self.tokens(text)
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
            new["text"] = text; new["size"] = self.tokens(text)
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
        what = (s.get("res") or [s.get("kind", "output")])[0]
        where = f"{self.store_prefix}/{s['id']}.txt" if self.store_prefix else f"#{s['id']}"
        if f == "placeholder":
            if hasattr(self.method, "placeholder_text"):                  # a method's own wording (the original's)
                return self.method.placeholder_text(s, s.get("text", ""))
            return f"[Old tool output removed to save context ({what}). Re-run the call if you need it.]"
        tool = f" or call ctxpress_retrieve with id {s['id']}" if self.retrieve_tool else ""
        if tool and self.store_namespace:
            tool += f" and session {self.store_namespace}"
        if f == "memid":
            return f"[Old tool output stored at {where}. Read it{tool} if you need it.]"
        if f == "memlabel":
            label = {"spec": "requirements document", "read": "file content", "search": "search result",
                     "command": "command output"}.get(s.get("kind"), "tool output")
            return f"[Old {label} of {what}, stored at {where}. Read it{tool} if you need it.]"
        if f in ("truncated", "structured"):
            return (s.get("kept_text") or "") + (f"\n[Full output stored at {where}.]" if self.memory else "")
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
