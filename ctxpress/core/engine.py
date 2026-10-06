"""The shared engine. Every context-management method is only a rule (ctxpress.methods); everything else is
here and identical for all methods:

  replay      the agent's real requests are replayed in order; each request appends the segments the agent
              produced since the previous one (trace.py)
  operations  what a method may do to the context before a request (see `Simulator` methods below):
                to_placeholder  item -> short placeholder (optionally with a memory id / label)
                truncate        item -> its first/last lines up to a token budget
                structure       item -> structured compression (code: signatures; tests: failures; search: paths)
                delete          item -> removed without a trace
                summarize       everything except `keep` -> one session summary at the front (layer 3)
                summarize_segment  one finished segment of work -> a segment summary in place (layer 2)
  billing     cached prefix at `cached`, the rest at `write`; output at `out`; cache = longest common prefix
              with the previous request ('lcp') or with the end of an earlier request ('checkpoint'), and
              optionally expires after `cache_ttl` seconds (trace timestamps)
  needs       what each next call needs (by file path): an edit needs the file's latest read plus later patches;
              a first read/edit of a file needs the search result that listed it; every edit needs the
              requirements documents
  recovery    a needed item not in context in full: if it was truncated / structured, the kept part may cover
              the need (checked on the actual text when available); otherwise the agent re-fetches it with
              probability q(form, type) (one extra request), else it is a silent miss
  re-explore  removing an output that was in active use makes the agent redo work (e_warm / e_cold)
  window      above `window` tokens the shared fallback summarizes once (as Codex does)
  overhead    the method's own decision cost (LLM / small-model scoring), if any
"""
from __future__ import annotations
import collections, statistics
from ctxpress.core.params import DEFAULT, Params
from ctxpress.core.trace import content_type
from ctxpress.core import textops

SUMMARY_FORMS = ("summary", "segsummary")


def item_type(s):
    """Coarse type used by the reuse curves (as in §8)."""
    return s["kind"] if s["seg"] == "out" else ("patch" if s.get("kind") == "edit" else "call")


MODEL_TYPES = {"reasoning", "function_call", "custom_tool_call", "local_shell_call", "web_search_call",
               "tool_search_call", "image_generation_call"}


def is_model(s):
    """An item the model produced: its calls, reasoning, and assistant text."""
    h = s.get("htype")
    if h is not None:
        return h in MODEL_TYPES or (h == "message" and s.get("role") == "assistant")
    return s["seg"] in ("call", "reason") or (s["seg"] == "msg" and s.get("role") == "assistant")


def miss_class(s):
    if s.get("kind") == "spec" and s["seg"] == "out":
        return "spec"
    return "code" if (s.get("kind") == "read" or s["seg"] == "call") else "other"


class Simulator:
    def __init__(self, trace, method, params: Params = DEFAULT, start=0, horizon=None, window="default",
                 on_request=None, e=None, e_cold=None):
        frozen = getattr(method, "parameters", None)
        if frozen is not None:
            from ctxpress.core.policy import same_parameters
            if params is not DEFAULT and not same_parameters(params, frozen):
                raise ValueError("host parameters differ from the frozen cost policy")
            params = frozen
        self.trace, self.method, self.P = trace, method, params
        self.reqs, self.prefix = trace["reqs"], trace["prefix"]
        self.alpha = trace.get("alpha", 1.17)
        self.start, self.horizon = start, horizon
        self.window = params.get("window") if window == "default" else window
        if getattr(method, "unbounded", False):
            self.window = None
        self.on_request = on_request
        self.e = params.get("e_warm") if e is None else e
        self.ec = params.get("e_cold") if e_cold is None else e_cold
        g = params.get
        self.CACHED, self.WRITE, self.OUT = g("cached"), g("write"), g("out")
        self.PH, self.LABEL, self.EXTRA_OUT = g("placeholder_tokens"), g("label_tokens"), g("extra_out")
        self.ctx, self.r, self.t = [], 0, None
        self.M = collections.Counter()
        self._nid = 0
        self.latest_read, self.search_hits, self.spec_items = {}, {}, {}
        self.patches = collections.defaultdict(list)
        self.members = {}                     # summary segment id -> items it replaced
        self._prev, self._trie, self._last_t = None, {}, None
        self._seen_summaries = set()
        self.segid, self._seg_edits, self._last_user_r = 0, 0, 0
        self.turn, self.uturn, self._prev_model, self._prev_out = 0, 0, False, False
        self.fixed_chars = None               # host: size of the request without its items (set by the host)
        self.n_input = None                   # host: number of items in the request as the harness sent it
        self.host = None                      # host adapter (rewrite.ResponsesHost): renders method-written messages
        self.memory = getattr(method, "memory", None)
        ts = [q.get("t") for q in self.reqs if q.get("t")]
        gaps = [b - a for a, b in zip(ts, ts[1:]) if b and a and b > a]
        self.model_time = params.get("latency_extra_request") or (statistics.median(gaps) if gaps else 10.0)

    # ------------------------------------------------------------------ sizes
    def seg_size(self, s):
        f = s.get("form", "full")
        if f == "full":
            return s["size"]
        return s.get("kept", self.PH)

    def size(self):
        return self.prefix + sum(self.seg_size(s) for s in self.ctx)

    def host_chars(self, s):
        """Characters the item takes in the host's request (exact for live items in full; otherwise ~4 / token)."""
        if s.get("form", "full") == "full" and "chars" in s:
            return s["chars"]
        return self.seg_size(s) * 4

    def request_chars(self, items=None):
        from ctxpress.core.hostsize import request_chars
        items = self.sent() if items is None else items
        fixed = self.fixed_chars if self.fixed_chars is not None else self.prefix * 4
        return request_chars(fixed, [self.host_chars(s) for s in items])

    def message_chars(self, text):
        """Host size of a user message the method adds (a summary)."""
        return self.host.message_chars(text) if self.host else len(text) + 60

    def sent(self):
        """The items of the next request, in order (the live context adds its fixed head in front)."""
        return list(self.ctx)

    def mark(self, s):
        """Turn bookkeeping on a new item. `turn`: model steps (a step starts at a model item whose predecessor is
        not one; items before the first step are turn 0, the head). `uturn`: user turns as Anthropic messages count
        them (each run of tool outputs, each user message); instructions and other items do not count."""
        model = is_model(s)
        if model and not self._prev_model:
            self.turn += 1
        if s["seg"] == "out":
            if not self._prev_out:
                self.uturn += 1
            self._prev_out = True
        elif s["seg"] == "msg" and s.get("role") == "user":
            self.uturn += 1; self._prev_out = False
        elif model:
            self._prev_out = False
        self._prev_model = model
        s["turn"], s["uturn"] = self.turn, self.uturn
        return s

    def outputs(self):
        return [s for s in self.ctx if s["seg"] == "out" and not s.get("protected")]

    def candidates(self):
        """Tool outputs and calls currently in context in full."""
        return [s for s in self.ctx if s["seg"] in ("out", "call") and s.get("form", "full") == "full"
                and not s.get("protected")]

    def new(self, seg):
        s = dict(seg); s["id"] = self._nid; self._nid += 1
        s["size"] = max(1, int(s["size"] * self.alpha)) if s["seg"] != "summary" else s["size"]
        s.setdefault("form", "full"); s["born"] = self.r; s["last"] = self.r
        return s

    # ------------------------------------------------------------------ billing
    def bill(self, segs=None):
        segs = self.ctx if segs is None else segs
        cur = [(s["id"], s.get("form", "full"), s.get("kept")) for s in segs]
        tot = self.prefix + sum(self.seg_size(s) for s in segs)
        ttl = self.P.get("cache_ttl")
        if ttl is not None and self.t is not None and self._last_t is not None and self.t - self._last_t > ttl:
            self._prev, self._trie = None, {}          # cache expired
        if self.P.get("cache_mode") == "lcp":
            hit = self.prefix if self._prev is not None else 0
            if self._prev is not None:
                for a, b, s in zip(self._prev, cur, segs):
                    if a != b:
                        break
                    hit += self.seg_size(s)
        else:
            hit = self.prefix if self._trie else 0
            node, run = self._trie, self.prefix
            for key, s in zip(cur, segs):
                if key not in node:
                    break
                node = node[key]; run += self.seg_size(s)
                if "$" in node:
                    hit = run
            node = self._trie
            for key in cur:
                node = node.setdefault(key, {})
            node["$"] = True
        self._prev = cur
        if self.t is not None:
            self._last_t = self.t
        M = self.M
        M["input"] += tot; M["uncached"] += tot - hit; M["cost"] += self.CACHED * hit + self.WRITE * (tot - hit)
        M["requests"] += 1; M["peak"] = max(M["peak"], tot)
        return tot

    def extra_request(self, L, size, weight, key, seconds=0.0):
        M = self.M
        M[key] += weight; M["requests"] += weight
        M["cost"] += weight * (self.CACHED * L + self.WRITE * size + self.OUT * self.EXTRA_OUT)
        M["input"] += weight * (L + size); M["uncached"] += weight * size; M["output"] += weight * self.EXTRA_OUT
        M["extra_latency"] += weight * (self.model_time + (seconds or 0.0))

    # ------------------------------------------------------------------ operations (used by methods)
    def protect(self, items):
        """Keep items and their tool-call pairs intact across all later context operations."""
        items = list(items)
        pairs = {s.get("call_id") for s in items if s.get("call_id")}
        for s in items + [s for s in self.ctx if s.get("call_id") in pairs]:
            s["protected"] = True

    def placeholder_form(self):
        return {None: "placeholder", "id": "memid", "label": "memlabel"}[self.memory]

    def placeholder_size(self, form=None):
        form = form or self.placeholder_form()
        return self.PH + {"placeholder": 0, "memid": 5, "memlabel": self.LABEL}[form]

    def to_placeholder(self, items, form=None):
        form = form or self.placeholder_form()
        size = self.placeholder_size(form)
        for s in items:
            if s.get("protected"):
                continue
            if s.get("form", "full") != form:
                self.M["op_" + form] += 1
            s["form"] = form; s["kept"] = size; s.pop("kept_text", None)
            if form != "placeholder":
                self.M["stored"] += 1

    def reinstate(self, items):
        """Make the context exactly `items`, in order; ones a method deleted come back in full. For methods that,
        like the original they port, recompute their view from the whole history before every request."""
        for s in items:
            if s.get("form") == "gone":
                s["form"] = "full"
        self.ctx = list(items)

    def delete(self, items):
        items = [s for s in items if not s.get("protected")]
        ids = {s["id"] for s in items}
        self.M["op_delete"] += sum(x["id"] in ids for x in self.ctx)
        for s in items:
            s["form"] = "gone"
        self.ctx = [x for x in self.ctx if x["id"] not in ids]

    def current_text(self, s):
        """Text available to a compression rule; archived originals require explicit recovery."""
        form = s.get("form", "full")
        if form == "full":
            return s.get("text")
        if form in ("truncated", "structured"):
            return s.get("kept_text")
        return None

    def _text_compressible(self, s):
        return (not s.get("protected") and not s.get("has_media")
                and s.get("form", "full") in ("full", "truncated", "structured"))

    def _commit_compression(self, s, text, size, form):
        if size >= self.seg_size(s):
            return False
        s.update(kept_text=text, kept=size, form=form)
        self.M["op_" + form] += 1
        return True

    def truncate(self, s, budget):
        """Shorten the current representation; unsuccessful attempts leave it unchanged."""
        if not self._text_compressible(s):
            return False
        text = self.current_text(s)
        if text is not None:
            kept = textops.head_tail(text, int(budget / self.alpha * 4))
            size = max(1, int(len(kept) / 4 * self.alpha)) + 12
        else:
            kept, size = None, min(self.seg_size(s), int(budget)) + 12
        return self._commit_compression(s, kept, size, "truncated")

    def structure(self, s, ratio_if_no_text=0.25, keep_also=()):
        """Structured compression: code -> signatures; run output -> failures and the last lines;
        search -> the list of paths; others -> head/tail."""
        if not self._text_compressible(s) or s.get("form") == "structured":
            return False
        text = self.current_text(s)
        if text is not None:
            kept = textops.structured(text, content_type(s), keep_also=keep_also)
            size = max(1, int(len(kept) / 4 * self.alpha)) + 12
        else:
            kept, size = None, int(self.seg_size(s) * ratio_if_no_text) + 12
        return self._commit_compression(s, kept, size, "structured")

    def keep_text(self, s, text, form="structured"):
        """Keep `text` (written by the method, e.g. a model's selection of relevant lines) in place of an output's
        content; recovery checks run on it as on any kept text."""
        if s.get("protected"):
            return False
        s["kept_text"] = text; s["kept"] = max(1, int(len(text) / 4 * self.alpha)) + 12
        s["form"] = form; self.M["op_" + form] += 1
        return True

    def summarize(self, keep=(), size=None, keep_user=False, bill_call=None, sid=None, guidance=None):
        """Layer 3: replace everything after the fixed prefix (except `keep`) with one summary at the front."""
        size = self.P.get("summary_tokens") if size is None else size
        if self.P.get("bill_summary_call") if bill_call is None else bill_call:
            self.bill(self.ctx)
        kid = {x["id"] for x in keep}
        kept, gone = [], []
        for s in self.ctx:
            if s.get("protected") or s["id"] in kid or (keep_user and s["seg"] == "msg" and s.get("role") == "user"):
                kept.append(s); continue
            gone.append(s)
            if s["seg"] in ("out", "call"):
                s["form"] = "summary"; s["ncomp"] = 1; s.pop("kept", None); s.pop("kept_text", None)
            elif s["seg"] == "summary":
                for m in self.members.pop(s["id"], []):
                    m["ncomp"] = m.get("ncomp", 1) + 1         # passed through one more summary
                    gone.append(m)
            else:
                s["form"] = "gone"
        sid = -1000 - self.r if sid is None else sid
        self.ctx = [dict(id=sid, seg="summary", size=size, form="full", born=self.r, last=self.r)] + kept
        self.members[sid] = [g for g in gone if g["seg"] in ("out", "call")]
        self.M["op_history_summary"] += 1
        if getattr(self.method, "hint", False):
            self.reread_hint()

    def model_text(self, system, user, purpose="reflect", model=None):
        """A method's own question to a model; replay has no model (LiveContext answers it)."""
        return None

    def summarize_segment(self, items, size=None, text=None, billed=True, guidance=None, prompt=None, text_format=None,
                          model_written=False, **extra):
        """Layer 2: replace `items` with one summary item, in place (at the first of them). `text`: a summary the
        method wrote itself (mechanical, no model call: billed=False), or obtained from `model_text` beforehand
        (model_written=True); otherwise a model writes it. Returns it.
        `prompt` / `text_format` only matter where a model writes the text (LiveContext); replay sizes it."""
        items = [s for s in items if s in self.ctx and not s.get("protected")]
        if not items:
            return None
        tok = sum(self.seg_size(s) for s in items)
        if text is not None:
            size = len(text) // 4 + 4
        size = size or max(self.P.get("segsummary_min"), int(self.P.get("segsummary_ratio") * tok))
        ids = {s["id"] for s in items}
        pos = min(i for i, x in enumerate(self.ctx) if x["id"] in ids)
        sid = -2000000 - self._nid; self._nid += 1
        members = []
        for s in items:
            if s["seg"] in ("out", "call"):
                s["form"] = "segsummary"; s["ncomp"] = 1; s.pop("kept", None); s.pop("kept_text", None); members.append(s)
            elif s["seg"] == "summary":
                for m in self.members.pop(s["id"], []):
                    m["ncomp"] = m.get("ncomp", 1) + 1; members.append(m)
            else:
                s["form"] = "gone"
        rest = [x for x in self.ctx if x["id"] not in ids]
        new = dict(id=sid, seg="summary", size=size, form="full", born=self.r, last=self.r, segsum=True, **extra)
        if text is not None:
            new["text"] = text
        new["turn"], new["uturn"] = items[0].get("turn", 0), items[0].get("uturn", 0)
        rest.insert(pos, new)
        self.ctx = rest
        self.members[sid] = members
        if billed:
            self.M["cost"] += self.WRITE * tok; self.M["input"] += tok; self.M["uncached"] += tok   # the summarizing call reads the segment
        self.M["segsummaries"] += 1
        self.M["op_mechanical_summary" if text is not None and not model_written else "op_segment_summary"] += 1
        return new

    def hint_items(self):
        """What a re-read hint brings back: the requirements documents and the latest read of every file the
        agent patched within the last w_warm requests."""
        want = list(self.spec_items.values())
        for p, pts in self.patches.items():
            if pts and self.r - max(x["born"] for x in pts) <= self.P.get("w_warm") and p in self.latest_read:
                want.append(self.latest_read[p])
        out, seen = [], set()
        for s in want:
            if s["id"] not in seen:
                seen.add(s["id"]); out.append(s)
        return out

    def reread_hint(self):
        """After a compaction, tell the agent to re-read the requirements and the files it is editing; they come
        back in full at the end of the context (one extra request each)."""
        ids = {x["id"] for x in self.ctx if x.get("form", "full") == "full"}
        for s in self.hint_items():
            if s["id"] in ids:
                continue
            cp = self.new(dict(s, form="full")); cp["size"] = s["size"]; cp["key"] = None
            for k in ("kept", "kept_text", "ncomp"):
                cp.pop(k, None)
            self.ctx.append(cp)
            self.extra_request(self.size(), s["size"], 1.0, "hint", s.get("dur") or 0.0)
            for p in s.get("res", []):
                if s["kind"] in ("read", "spec"):
                    self.latest_read[p] = cp; self.patches[p] = []
                if s["kind"] == "spec":
                    self.spec_items[p] = cp

    def segments(self):
        """Segments of work currently in context: segid -> items. The last one is still open."""
        out = collections.OrderedDict()
        for s in self.ctx:
            if "segid" in s and s["seg"] != "summary":
                out.setdefault(s["segid"], []).append(s)
        return out

    # ------------------------------------------------------------------ recovery helpers
    def q_of(self, s, form):
        q = self.P.qv(form, s["kind"] if s["seg"] == "out" else "*")
        if form in SUMMARY_FORMS and s.get("ncomp", 1) > 1:
            q *= self.P.get("recompress_retention") ** (s["ncomp"] - 1)
        return q

    def coverage(self, s, call, path, why):
        """Probability that a truncated / structured item still covers what `call` needs."""
        form = s["form"]
        kt = s.get("kept_text")
        if kt is None:
            return self.P.cov(form, s["kind"] if s["seg"] == "out" else "*")
        if why == "hit":                 # the call needs the search result that listed `path`
            return 1.0 if path.split("/")[-1] in kt else 0.0
        if why == "edit":
            anchors = (call.get("anchors") or {}).get(path)
            if not anchors:
                return self.P.cov(form, s["kind"])
            pool = kt + "\n" + "\n".join(x.get("text", "") for x in self.patches.get(path, []) if x.get("form", "full") == "full")
            found = sum(1 for a in anchors if a in pool)
            return 1.0 if found / len(anchors) >= self.P.get("coverage_threshold") else 0.0
        return self.P.cov(form, s["kind"])

    # ------------------------------------------------------------------ main loop
    def ingest(self, r):
        for xi, x in enumerate(self.reqs[r]["before"]):
            self.add(x, (r, xi), hooks=r >= self.start)

    def add(self, x, key=None, hooks=True):
        """Append one segment to the context and update the bookkeeping (also used by ctxpress.live.context)."""
        r = self.r
        s = self.new(x); s["key"] = key; s["segid"] = self.segid
        self.mark(s)
        self.ctx.append(s)
        if s["seg"] == "msg" and s.get("role") == "user" and r > 0:
            self.segid += 1; s["segid"] = self.segid; self._seg_edits = 0
            self._last_user_r = r
        if s["seg"] == "call" and s.get("kind") == "edit":
            self._seg_edits += 1
            for p in s.get("res", []):
                self.patches[p].append(s)
        if s["seg"] == "out":
            for p in s.get("res", []):
                if s["kind"] in ("read", "spec"):
                    self.latest_read[p] = s; self.patches[p] = []
                if s["kind"] == "spec":
                    self.spec_items[p] = s
            for p in s.get("outpaths", []):
                self.search_hits.setdefault(p, s)
            if s.get("test") and self._seg_edits:            # a read -> edit -> test cycle closed
                self.segid += 1; self._seg_edits = 0
        hook = getattr(self.method, "on_ingest", None)
        if hook and hooks:
            hook(self, s)
        return s

    def run(self):
        M, P = self.M, self.P
        reqs = self.reqs
        if hasattr(self.method, "reset"):
            self.method.reset(self)
        fallback_size = P.get("summary_tokens")
        end = len(reqs) - 1 if self.horizon is None else min(len(reqs) - 1, self.start + self.horizon)
        for r in range(end):
            self.r = r
            self.t = reqs[r].get("t")
            self.ingest(r)
            if r < self.start:
                continue
            before = {s["id"]: s for s in self.ctx if s.get("form", "full") == "full" and s["seg"] in ("out", "call")}
            self.method.step(self, r)
            if self.window and self.size() > self.window:
                self.summarize(size=fallback_size); M["forced"] += 1
            oh = self.method.overhead(self, r) if hasattr(self.method, "overhead") else 0.0
            if oh:
                M["overhead"] += oh; M["cost"] += oh
            for s in self.ctx:
                if s["seg"] == "summary" and s["id"] not in self._seen_summaries:
                    self._seen_summaries.add(s["id"]); M["cost"] += self.OUT * s["size"]; M["output"] += s["size"]
                    if s.get("segsum"):
                        M["segsummary_out"] += s["size"]
                    else:
                        M["summaries"] += 1
            L = self.bill(self.ctx)
            if self.on_request:
                self.on_request(r, self, L)
            now = {s["id"] for s in self.ctx if s.get("form", "full") == "full"}
            for i, s in before.items():                     # removed without a summary -> re-exploration
                if i in now or s["seg"] != "out" or s.get("form") in SUMMARY_FORMS:
                    continue
                warm = r - max(s["born"], s["last"]) <= P.get("w_warm")
                w = self.e if warm else self.ec
                if s.get("form") in ("truncated", "structured"):
                    w *= 1 - P.cov(s["form"], s["kind"])
                self.extra_request(L, s["size"], w, "reexplore", 0.0 if self.memory else (s.get("dur") or 0.0))
            for c in [x for x in reqs[r + 1]["before"] if x["seg"] == "call"]:
                self.serve(c, L)
        M["wall"] = (reqs[end]["t"] - reqs[self.start]["t"]) if reqs[end].get("t") and reqs[self.start].get("t") else 0.0
        return dict(M)

    def serve(self, c, L):
        """Account for what call `c` (issued by the response to this request) needs."""
        M, P = self.M, self.P
        needs = []
        for p in c.get("res", []):
            if c["kind"] == "edit" and p in self.latest_read:
                needs.append((self.latest_read[p], p, "edit")); needs.extend((x, p, "edit") for x in self.patches[p])
            elif c["kind"] in ("read", "edit") and p not in self.latest_read and p in self.search_hits:
                needs.append((self.search_hits[p], p, "hit"))
        ids = {x["id"] for x in self.ctx}
        refreshed = set()
        for i, p, why in needs:
            i["last"] = self.r
            form = i.get("form", "full")
            if set(i.get("res", [])) & refreshed or (form == "full" and i["id"] in ids):
                continue
            if form == "full":
                form = "gone"
            miss = 1.0
            if form in ("truncated", "structured") and i["id"] in ids:
                miss = 1.0 - self.coverage(i, c, p, why)
                if miss <= 0:
                    M["covered"] += 1
                    continue
            q = self.q_of(i, form)
            k = miss_class(i)
            M["silent"] += miss * (1 - q); M["silent_" + k] += miss * (1 - q)
            M[("silent_sum_" if form in SUMMARY_FORMS else "silent_ph_") + k] += miss * (1 - q)
            M["silent_form_" + form] += miss * (1 - q)
            reexec = 0.0 if form in ("memid", "memlabel") else (i.get("dur") or 0.0)
            self.extra_request(L, i["size"], miss * q, "recover", reexec)
            if form in ("memid", "memlabel"):
                M["retrieved"] += miss * q
            if miss * q >= 0.5:
                cp = self.new(dict(i, form="full")); cp["size"] = i["size"]; cp["key"] = None
                for kk in ("kept", "kept_text", "ncomp"):
                    cp.pop(kk, None)
                cp["source"] = "memory" if form in ("memid", "memlabel") else i.get("source")
                self.ctx.append(cp)
                for pp in i.get("res", []):
                    if i["kind"] in ("read", "spec") or i["seg"] == "call":
                        self.latest_read[pp] = cp; self.patches[pp] = []
                if P.get("dedupe_recovery"):
                    for pp in i.get("outpaths", []):          # a re-run search lists the same files again
                        if self.search_hits.get(pp) is i:
                            self.search_hits[pp] = cp
                    if i["kind"] == "spec":
                        for pp, sp in list(self.spec_items.items()):
                            if sp is i:
                                self.spec_items[pp] = cp
                refreshed |= set(i.get("res", []))
                if hasattr(self.method, "on_fault"):
                    self.method.on_fault(self, i)
        if c["kind"] == "edit" and self.spec_items:
            ids = {x["id"] for x in self.ctx}
            items = list(self.spec_items.items())
            if P.get("spec_recovery"):
                # only the current requirements: the most recently read one and those read within w_warm requests
                # before it (the agent re-reads its own milestone's SRS, not every SRS it ever opened)
                latest = max(s["born"] for _, s in items)
                items = [(p, s) for p, s in items if s["born"] >= latest - P.get("w_warm")]
            seen = set()
            for p, s in items:
                if s["id"] in seen and P.get("dedupe_recovery"):   # one output can cover several requirement files
                    continue
                seen.add(s["id"])
                form = s.get("form", "full")
                if form == "full" and s["id"] in ids:
                    continue
                form = "gone" if form == "full" else form
                miss = 1.0
                if form in ("truncated", "structured"):
                    miss = 1.0 - P.cov(form, "spec")
                q = self.q_of(s, form)
                M["spec_gap"] += miss * (1 - q)
                if P.get("spec_recovery") and miss * q >= 0.5:
                    # the agent re-reads the requirements (§5: 20/20 after one compaction, 19/20 after three);
                    # the copy is back in context, so later edits do not count the gap again
                    self.extra_request(L, s["size"], miss * q, "recover", 0.0)
                    cp = self.new(dict(s, form="full")); cp["size"] = s["size"]; cp["key"] = None
                    for kk in ("kept", "kept_text", "ncomp"):
                        cp.pop(kk, None)
                    self.ctx.append(cp)
                    for pp, sp in list(self.spec_items.items()):
                        if sp is s:
                            self.spec_items[pp] = cp
                    for pp in s.get("res", []):
                        self.latest_read[pp] = cp; self.patches[pp] = []


def run(trace, method, params: Params = DEFAULT, **kw):
    return Simulator(trace, method, params, **kw).run()
