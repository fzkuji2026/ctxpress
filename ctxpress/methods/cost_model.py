"""The cost model (this paper, experiment.zh.html §8–§9).

Before each request it compares, in expected cost (input + output + recovery + re-exploration + lambda x
weighted silent misses), these options and takes the cheapest:
  nothing
  layer 1  a batch of items, each changed by its best allowed operation for its content type
           (placeholder | truncate | structured), paid once by the cache break; optional lookahead: would
           waiting 1/2/4/8 requests and changing a larger batch then be cheaper?
  layer 2  fold finished segments of work (older than the newest `seg_keep`) into segment summaries in place
  layer 3  summarize everything except the items worth keeping
Per item:
  keep cost      cached * (size - kept_after) * min(m_k(a), R)       m: expected requests it would be carried
  removal cost   p_k(a) * (1 - cov) * [q * (cached L + size + out * extra_out) + (1 - q) * lambda * harm]
                 + re-exploration (warm / cold) * (1 - cov)
  p_k, m_k   reuse curves per type and idle age, estimated on other sessions (ctxpress.reuse)
  q          re-fetch probability of the form the item would be in (params.q; memory forms if memory is on)
  cov        chance the kept part of a truncated / structured item is enough (measured on training traces)
  cache break  (write - cached) x tokens after the first changed item; 0 if the cache has already expired
With only placeholders and summaries allowed and memory off, this is exactly the §8 model (FullCostModel).
"""
from __future__ import annotations
from ctxpress.methods.base import Method
from ctxpress.core import textops
from ctxpress.core import calibration
from ctxpress.core.engine import item_type
from ctxpress.core.trace import content_type

ALL_TYPES = ["spec", "read_code", "read_other", "search", "run", "patch", "call"]


class CostModel(Method):
    source = "本文"

    def __init__(self, traces_for_params=None, lam=0.0, use_types=True, use_reexplore=True, allow_summary=True,
                 allow_placeholder=True, smooth=True, online=False, name=None, type_fn=None, item_model=None,
                 lookahead=0, harm=None, ops=None, segments=False, seg_keep=2, memory=None, hint=False,
                 cov_rates=None, truncate_budget=None, e=None, profile=None):
        """ops: {content type: [allowed operations]}; default placeholder for every type.
        harm: weight of a silent miss by class ('spec' | 'code' | 'other'), or by form then class."""
        self.lam, self.use_types, self.reex = lam, use_types, use_reexplore
        self.allow_sum, self.allow_ph, self.online = allow_summary, allow_placeholder, online
        self.item_model, self.lookahead, self.harm = item_model, lookahead, harm
        self.ops = ops or {k: ["placeholder"] for k in ALL_TYPES}
        self.segments, self.seg_keep = segments, seg_keep
        self.memory, self.hint = memory, hint
        self.requires_summary = allow_summary or segments
        if profile is not None:
            if traces_for_params is not None or type_fn is not None or item_model is not None:
                raise ValueError("use a frozen profile or training inputs, not both")
            fitted = calibration.validate(profile) if isinstance(profile, dict) else calibration.load(profile)
            self.budget = fitted["truncate_budget"] if truncate_budget is None else truncate_budget
            if self.budget != fitted["truncate_budget"]:
                raise ValueError("truncation budget differs from the fitted coverage budget")
            self.curves = calibration.FrozenCurves(fitted["curves"], fitted["raw_curves"])
            self._train_lengths = fitted["lengths"]
            self.cov_rates = {(row["form"], row["type"]): (row["rate"], row["count"]) for row in fitted["coverage"]}
            self.provenance = fitted.get("provenance", {})
        else:
            from ctxpress.core import reuse
            traces_for_params = list(traces_for_params or [])
            if not traces_for_params:
                raise ValueError("CostModel needs training traces or a profile; create one with ctxpress fit")
            self.budget = 2000 if truncate_budget is None else truncate_budget
            self.curves = reuse.Curves(traces_for_params, type_fn or item_type, smooth=smooth)
            if not self.curves.tab.get("*"):
                raise ValueError("training traces contain no reusable tool observations")
            self._train_lengths = [len(trace["reqs"]) for trace in traces_for_params]
            needs_cov = any(o in ("truncate", "structure") for v in self.ops.values() for o in v)
            self.cov_rates = reuse.coverage_rates(traces_for_params, self.budget) if needs_cov and cov_rates is None else {}
            self.provenance = dict(sessions=[trace.get("name", "") for trace in traces_for_params], smooth=smooth)
        if cov_rates is not None:
            self.cov_rates = cov_rates
        self.rem = calibration.remaining_from_lengths(self._train_lengths)
        self.e_override = e
        self.name = name or f"成本模型（λ={lam:g}）"
        on = [k for k, v in self.ops.items() if v != ["placeholder"]]
        self.framework = dict(
            L1="成本决定：按类型的再用曲线" + ("；" + "，".join(f"{k}: {'/'.join(self.ops[k])}" for k in on) if on else "；换成占位符"),
            L2="成本决定何时折叠已完成的段" if segments else "无",
            L3="成本决定何时整体摘要" if allow_summary else "无",
            cross="需求文档、正在用的自动保留" + ("；压缩时提示重读" if hint else ""),
            memory={None: "无", "id": "有：占位符带编号", "label": "有：占位符带编号和标签"}[memory],
            decider="成本模型 + 性能约束（λ）")

    # ------------------------------------------------------------------ parameters
    def save_profile(self, path):
        """Save statistics only; policy switches, prices and lambda remain explicit method arguments."""
        if self.item_model is not None:
            raise ValueError("portable calibrations do not serialize learned item classifiers")
        return calibration.save(path, calibration.pack(self.curves, self._train_lengths, self.cov_rates,
                                                       self.budget, self.provenance))

    def reset(self, sim):
        self.hot = set()
        self._p_cache = {}
        self._trial = {}
        self.e = sim.e if self.e_override is None else self.e_override

    def pm(self, s, a):
        if self.item_model is not None and s["id"] in self._p_cache:
            return self._p_cache[s["id"]], self.curves.pm(s, a)[1]
        p, m = self.curves.pm(s, a, self.use_types)
        if self.online and set(s.get("res", [])) & self.hot:
            p = max(p, 0.9)
        return p, m

    def cov(self, sim, s, form):
        if form not in ("truncated", "structured"):
            return 0.0
        v = self.cov_rates.get((form, content_type(s)))
        return v[0] if v and v[1] >= 5 else sim.P.cov(form, s["kind"])

    def harm_w(self, s, form):
        if self.harm is None:
            return 1.0
        kk = "spec" if s.get("kind") == "spec" and s["seg"] == "out" else ("code" if s.get("kind") == "read" or s["seg"] == "call" else "other")
        hh = self.harm.get(form, self.harm) if isinstance(self.harm.get(form), dict) else self.harm
        if form in ("truncated", "structured", "memid", "memlabel", "gone") and isinstance(self.harm.get("placeholder"), dict):
            hh = self.harm["placeholder"]
        if form == "segsummary" and isinstance(self.harm.get("summary"), dict):
            hh = self.harm["summary"]
        return hh.get(kk, 1.0)

    def removal_cost(self, sim, s, form, L, r):
        a = r - s["last"]
        p, m = self.pm(s, max(a, 1))
        q = sim.q_of(s, form) if s["seg"] == "out" else sim.P.qv(form, "*")
        miss = 1.0 - self.cov(sim, s, form)
        unit = sim.CACHED * L + sim.WRITE * s["size"] + sim.OUT * sim.EXTRA_OUT
        cost = p * miss * (q * unit + (1 - q) * self.lam * self.harm_w(s, form))
        if self.reex and s["seg"] == "out" and form not in ("summary", "segsummary"):
            ee = self.e if r - max(s["born"], s["last"]) <= sim.P.get("w_warm") else sim.ec
            cost += ee * miss * unit
        return cost, m

    def kept_after(self, sim, s, op):
        """Tokens the item would keep under operation `op` (computed once per item)."""
        if op == "placeholder":
            return sim.PH + {None: 0, "id": 5, "label": sim.LABEL}[self.memory]
        key = (s["id"], op)
        if key not in self._trial:
            text = s.get("text")
            if op == "truncate":
                k = (int(len(textops.head_tail(text, int(self.budget / sim.alpha * 4))) / 4 * sim.alpha) + 12) if text is not None else min(s["size"], self.budget) + 12
            else:
                k = (int(len(textops.structured(text, content_type(s))) / 4 * sim.alpha) + 12) if text is not None else int(s["size"] * 0.25) + 12
            self._trial[key] = k
        return self._trial[key]

    def best_op(self, sim, s, L, r, R, age_shift=0):
        """Best layer-1 operation for item s and its net gain (keep cost saved minus removal cost)."""
        allowed = self.ops.get(content_type(s), ["placeholder"]) if self.allow_ph else []
        best, best_g = None, 0.0
        last = s["last"]
        if age_shift:
            s["last"] = last - age_shift
        try:
            for op in allowed:
                form = {"placeholder": sim.placeholder_form(), "truncate": "truncated", "structure": "structured"}[op]
                kept = self.kept_after(sim, s, op)
                if kept >= s["size"]:
                    continue
                c, m = self.removal_cost(sim, s, form, L, r)
                g = sim.CACHED * (s["size"] - kept) * min(m, R) - c
                if g > best_g:
                    best, best_g = op, g
        finally:
            s["last"] = last
        return best, best_g

    def break_cost(self, sim, first):
        ttl = sim.P.get("cache_ttl")
        if ttl is not None and sim.t is not None and sim._last_t is not None and sim.t - sim._last_t > ttl:
            return 0.0                                  # the cache is gone anyway: changing the context is free
        return (sim.WRITE - sim.CACHED) * sum(sim.seg_size(x) for x in sim.ctx[first:])

    def _predict(self, sim, r):
        from ctxpress.core.itemmodel import features
        import collections
        reads, patches, searched = collections.Counter(), collections.Counter(), set()
        F, ids = [], []
        for x in sim.ctx:
            if x["seg"] == "out" and x.get("kind") in ("read", "spec"):
                for p in x.get("res", []): reads[p] += 1
            if x["seg"] == "call" and x.get("kind") == "edit":
                for p in x.get("res", []): patches[p] += 1
            if x["seg"] == "out":
                searched |= set(x.get("outpaths", []))
        for x in sim.ctx:
            if x["seg"] not in ("out", "call") or x.get("form", "full") != "full":
                continue
            res = x.get("res", [])
            F.append(features(item_type(x), max(1, r - x["last"]), x["size"], sum(reads[p] for p in res),
                              sum(patches[p] for p in res), any(p in searched for p in res)))
            ids.append(x["id"])
        self._p_cache = dict(zip(ids, self.item_model.p_many(F))) if F else {}

    # ------------------------------------------------------------------ decision
    def step(self, sim, r):
        L = sim.size()
        R = self.rem(r)
        protected = {s.get("call_id") for s in sim.ctx if s.get("has_media") and s.get("call_id")}
        if self.item_model is not None:
            self._predict(sim, r)
        ph_gain, ph_set, first = 0.0, [], None
        sum_keep, sum_gain = [], 0.0
        for idx, s in enumerate(sim.ctx):
            if s.get("protected") or s.get("has_media") or s.get("call_id") in protected:
                sum_keep.append(s)
                continue
            if s["seg"] not in ("out", "call") or s.get("form", "full") != "full":
                continue
            op, g = self.best_op(sim, s, L, r, R)
            if op and r - s["last"] >= 1:
                ph_gain += g; ph_set.append((s, op)); first = idx if first is None else first
            c_sm, m = self.removal_cost(sim, s, "summary", L, r)
            g2 = sim.CACHED * s["size"] * min(m, R) - c_sm
            if g2 > 0 and r - s["last"] >= 1:
                sum_gain += g2
            else:
                sum_keep.append(s)
        best, choice = 0.0, None
        if ph_set:
            net = ph_gain - self.break_cost(sim, first)
            if net > 0 and self.lookahead:
                for k in (1, 2, 4, 8):
                    if k > self.lookahead:
                        break
                    g_k, first_k = 0.0, None
                    for idx, x in enumerate(sim.ctx):
                        if (x["seg"] not in ("out", "call") or x.get("form", "full") != "full"
                                or x.get("protected") or x.get("has_media") or x.get("call_id") in protected):
                            continue
                        op_k, gk = self.best_op(sim, x, L, r, R, age_shift=k)
                        if op_k:
                            g_k += gk; first_k = idx if first_k is None else first_k
                    if first_k is None:
                        continue
                    wait_loss = sum(sim.CACHED * (x["size"] - self.kept_after(sim, x, op)) * k for x, op in ph_set)
                    if g_k - self.break_cost(sim, first_k) - wait_loss > net:
                        net = -1.0
                        break
            if net > best:
                best, choice = net, "ph"
        if self.segments and (not hasattr(sim, "summarizer") or sim.summarizer):
            seg_net, seg_items = self._segment_option(sim, r, L, R)
            if seg_net > best:
                best, choice = seg_net, "seg"
        if self.allow_sum and sum_gain > 0 and (not hasattr(sim, "summarizer") or sim.summarizer):
            keep_ids = {x["id"] for x in sum_keep}
            kept = [x for x in sim.ctx if x["id"] in keep_ids or x["seg"] == "msg" and x.get("role") == "user"]
            S = sim.P.get("summary_tokens")
            l_after = sum(sim.seg_size(x) for x in kept) + S
            cost = sim.CACHED * L + sim.OUT * S + (sim.WRITE - sim.CACHED) * l_after + sim.CACHED * S * R
            if self.hint:          # re-reading what the hint brings back, and carrying it afterwards
                back = sum(x["size"] for x in sim.hint_items() if x["id"] not in keep_ids)
                cost += sim.WRITE * back + sim.CACHED * back * R + sim.OUT * sim.EXTRA_OUT
            net = sum_gain - cost
            if net > best:
                best, choice = net, "sum"
        if choice == "ph":
            for s, op in ph_set:
                if op == "placeholder":
                    sim.to_placeholder([s])
                elif op == "truncate":
                    if not sim.truncate(s, self.budget):
                        sim.to_placeholder([s])
                else:
                    if not sim.structure(s):
                        sim.to_placeholder([s])
        elif choice == "seg":
            for items in seg_items:
                sim.summarize_segment(items)
        elif choice == "sum":
            sim.summarize(keep=sum_keep, keep_user=True, bill_call=True, sid=-900000 - r)

    def _segment_option(self, sim, r, L, R):
        """Fold finished segments (older than the newest seg_keep). Within a segment only items whose own
        gain is positive are folded; the rest (e.g. requirements) stay in place in full."""
        segs = list(sim.segments().items())
        old = segs[:-(self.seg_keep + 1)] if len(segs) > self.seg_keep + 1 else []
        total, chosen, first = 0.0, [], None
        for sid, items in old:
            fold, g = [], 0.0
            for s in items:
                if s.get("protected"):
                    continue
                if s["seg"] in ("out", "call"):
                    if s.get("form", "full") != "full":
                        continue
                    c, m = self.removal_cost(sim, s, "segsummary", L, r)
                    gi = sim.CACHED * s["size"] * min(m, R) - c
                    if gi > 0:
                        fold.append(s); g += gi
                elif s["seg"] in ("msg", "reason") and s.get("role") != "user":
                    fold.append(s); g += sim.CACHED * sim.seg_size(s) * R
            if not any(s["seg"] in ("out", "call") for s in fold):
                continue
            tok = sum(sim.seg_size(s) for s in fold)
            S = max(sim.P.get("segsummary_min"), int(sim.P.get("segsummary_ratio") * tok))
            g -= sim.WRITE * tok + sim.OUT * S + sim.CACHED * S * R
            if g > 0:
                total += g; chosen.append(fold)
                pos = min(sim.ctx.index(s) for s in fold)
                first = pos if first is None else min(first, pos)
        if not chosen:
            return 0.0, []
        return total - self.break_cost(sim, first), chosen

    def on_fault(self, sim, item):
        if self.online:
            self.hot |= set(item.get("res", []))
