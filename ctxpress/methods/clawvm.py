"""ClawVM (arXiv 2604.10352), its selection core ported from the authors' artifact
(github.com/mpi-dsg/clawvm, replay_py/clawvm_replay/tier2.py `_select_representations`, `_calc_upgrade_score`,
`_touch_recency`, POLICIES).

Each turn, every page gets one representation out of none < pointer < structured < compressed < full under a
token budget:
  1. hard-pinned pages and demanded pages are installed at their minimum (or demanded) representation, by
     priority (pin class, type, base utility);
  2. pointers are prefetched for every other page, highest recompute cost first, while they fit;
  3. the remaining budget is spent on step-wise upgrades, best utility per extra token first, where
     utility = base_utility + pin boost (hard 2.0, soft 0.6) + 0.6 * recency + 0.4 * recompute_cost
     (+ 2.2 * demands in the next `horizon` turns for the oracle);
  4. recency decays by 0.88 per turn and +1 for each demand.
`repro/clawvm_compare.py` swaps this function into the authors' Tier-2 simulator and checks that every summary
row is unchanged. Writeback at lifecycle boundaries acts on the harness's own state files and is not ported.
"""
from __future__ import annotations
from ctxpress.methods.base import Method, K
from ctxpress.methods.budget import BudgetSpec
from ctxpress.core.trace import content_type

REPRESENTATIONS = ["none", "pointer", "structured", "compressed", "full"]
REP_INDEX = {name: idx for idx, name in enumerate(REPRESENTATIONS)}

POLICIES = {
    "retrieval_only": dict(auto_pin_hard=False, prefetch_pointer=False, writeback_compaction=False, writeback_reset=False,
                           upgrade_mode="none", pointer_resolves_evidence=False, multisession_budget_scale=0.78),
    "retrieval_only_cached": dict(auto_pin_hard=False, prefetch_pointer=False, writeback_compaction=False, writeback_reset=False,
                                  upgrade_mode="none", pointer_resolves_evidence=True, multisession_budget_scale=0.78),
    "compaction_hybrid": dict(auto_pin_hard=False, prefetch_pointer=True, writeback_compaction=True, writeback_reset=False,
                              upgrade_mode="recency", pointer_resolves_evidence=True, multisession_budget_scale=0.85),
    "clawvm": dict(auto_pin_hard=True, prefetch_pointer=True, writeback_compaction=True, writeback_reset=True,
                   upgrade_mode="clawvm", pointer_resolves_evidence=True, multisession_budget_scale=1.0),
    "lru": dict(auto_pin_hard=True, prefetch_pointer=True, writeback_compaction=True, writeback_reset=True,
                upgrade_mode="lru", pointer_resolves_evidence=True, multisession_budget_scale=1.0),
    "oracle_h3": dict(auto_pin_hard=True, prefetch_pointer=True, writeback_compaction=True, writeback_reset=True,
                      upgrade_mode="oracle", horizon=3, pointer_resolves_evidence=True, multisession_budget_scale=1.0),
}


def coerce(rep):
    return rep if rep in REP_INDEX else "none"


def max_rep(a, b):
    return a if REP_INDEX.get(coerce(a), 0) >= REP_INDEX.get(coerce(b), 0) else b


def next_rep(rep):
    i = REP_INDEX.get(coerce(rep), 0)
    return None if i + 1 >= len(REPRESENTATIONS) else REPRESENTATIONS[i + 1]


def cost(page, rep):
    if rep == "none":
        return 0
    try:
        return max(int(page.get("tokens", {}).get(rep)), 0)
    except (TypeError, ValueError):
        return 0


def priority(page):
    pin, ptype = str(page.get("pin_class", "none")), str(page.get("type", ""))
    return (2 if pin == "hard" else 1 if pin == "soft" else 0,
            2 if ptype == "bootstrap_policy" else 1 if ptype == "constraint" else 0,
            int(float(page.get("base_utility", 0)) * 1000))


def score(policy, page, recency, future=0):
    base, recompute, mode = float(page.get("base_utility", 0.0)), float(page.get("recompute_cost", 0.0)), policy.get("upgrade_mode")
    pin = str(page.get("pin_class", "none"))
    boost = 2.0 if pin == "hard" else 0.6 if pin == "soft" else 0.0
    if mode == "none":
        return 0.0
    if mode == "lru":
        return recency
    if mode == "recency":
        return base + 0.9 * recency + 0.1 * recompute
    if mode == "clawvm":
        return base + boost + 0.6 * recency + 0.4 * recompute
    if mode == "oracle":
        return base + boost + 0.6 * recency + 0.4 * recompute + 2.2 * future
    return base


def select(pages, demands, policy, budget, recency, future=None):
    """pages: [{page_id, type, pin_class, min_repr, tokens{rep: n}, base_utility, recompute_cost}];
    demands: [{page_id, required_repr}]; future(page_id) -> demands in the next `horizon` turns (oracle only).
    Returns (selected {page_id: rep}, tokens used, unmet required {page_id: rep})."""
    page_map = {str(p.get("page_id")): p for p in pages}
    selected = {pid: "none" for pid in page_map}
    required = {}
    if policy.get("auto_pin_hard"):
        for p in pages:
            if str(p.get("pin_class", "none")) == "hard":
                pid = str(p.get("page_id"))
                required[pid] = max_rep(required.get(pid, "none"), coerce(str(p.get("min_repr", "pointer"))))
    for d in demands:
        if not isinstance(d, dict):
            continue
        pid = str(d.get("page_id")); p = page_map.get(pid)
        if p is None:
            continue
        req = coerce(str(d.get("required_repr", p.get("min_repr", "pointer"))))
        required[pid] = max_rep(required.get(pid, "none"), max_rep(req, coerce(str(p.get("min_repr", "pointer")))))
    left = max(int(budget), 0)
    unmet = {}
    for pid, rep in sorted(required.items(), key=lambda kv: priority(page_map[kv[0]]), reverse=True):
        c = cost(page_map[pid], rep)
        if c <= left:
            selected[pid] = rep; left -= c
        else:
            unmet[pid] = rep
    if policy.get("prefetch_pointer"):
        for p in sorted(pages, key=lambda p: (float(p.get("recompute_cost", 0.0)), float(p.get("base_utility", 0.0))), reverse=True):
            pid = str(p.get("page_id"))
            if selected.get(pid, "none") == "none" and cost(p, "pointer") <= left:
                selected[pid] = "pointer"; left -= cost(p, "pointer")
    mode = policy.get("upgrade_mode")
    if mode != "none":
        ups = []
        for p in pages:
            pid = str(p.get("page_id"))
            cur = selected.get(pid, "none"); nxt = next_rep(cur)
            while nxt is not None:
                delta = cost(p, nxt) - cost(p, cur)
                if delta > 0:
                    f = future(pid) if (mode == "oracle" and future) else 0
                    ups.append((score(policy, p, float(recency.get(pid, 0.0)), f) / float(delta), pid, REP_INDEX.get(nxt, 0), delta, nxt))
                cur, nxt = nxt, next_rep(nxt)
        ups.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        for _, pid, _, delta, target in ups:
            cur = selected.get(pid, "none")
            if REP_INDEX.get(target, 0) <= REP_INDEX.get(cur, 0) or next_rep(cur) != target:
                continue
            if delta <= left:
                selected[pid] = target; left -= delta
    return selected, max(int(budget), 0) - left, unmet


def touch_recency(recency, demanded):
    for pid in list(recency):
        recency[pid] *= 0.88
        if recency[pid] < 0.0001:
            recency[pid] = 0.0
    for pid in demanded:
        recency[pid] = recency.get(pid, 0.0) + 1.0


class ClawVM(Method):
    """ClawVM (2604.10352) with the authors' selection core (`clawvm.select`, verified against their simulator).
    Every request each tool output is a page with four representations: full, compressed (head and tail, a third
    of the text), structured (definitions / failures / paths), pointer (placeholder with the stored original).
    Pages are chosen under `budget` tokens by ClawVM's two phases: minimum representations for pinned and
    demanded pages, pointer prefetch, then upgrades by utility per token. Page parameters follow the authors'
    converter for real transcripts (workloads/convert_transcripts.py): evidence pages soft-pinned, min pointer,
    base utility 5, recompute cost 8 (reads, commands), 12 (edits), 6 (other); requirement documents are
    hard-pinned constraint pages (min structured, utility 7). Demands: the newest turn's outputs at full, outputs
    of files the newest call touches at pointer. A page ClawVM leaves out ("none") is still sent as a pointer: the
    Responses API needs every call's output, so the prompt can exceed the budget by those pointers."""
    source = "arXiv 2604.10352"
    budget_spec = BudgetSpec("budget", "tokens", "resident page representations", 32 * K)

    def __init__(self, budget=32 * K, policy="clawvm"):
        budget = self.budget_spec.validate(budget)
        self.budget, self.policy = budget, dict(POLICIES[policy])
        self.memory = "id"
        self.name = f"ClawVM（{budget // K}k）"
        self.framework = dict(L1="每次请求在预算内为每条输出选表示：全文/压缩/结构/指针（按效用每 token 升级）", L2="无", L3="无",
                              cross="约束类（需求文档）硬固定", memory="有：指针指向持久的原文", decider="效用分数（原文公式）")
        self.recency = {}

    def reset(self, sim):
        self.recency = {}

    def _page(self, sim, s):
        c = s.setdefault("_cvm", {})
        if "structured" not in c:
            from ctxpress.core import textops
            text = s.get("text") or ""
            c["structured"] = len(textops.structured(text, content_type(s))) // 4 + 12 if text else s["size"] // 4 + 12
            c["compressed"] = max(1, s["size"] // 3) + 12
        spec = s["kind"] == "spec"
        rc = 12.0 if s["kind"] == "edit" else 8.0 if s["kind"] in ("read", "command", "spec") else 6.0
        return dict(page_id=str(s["id"]), type="constraint" if spec else "evidence", pin_class="hard" if spec else "soft",
                    min_repr="structured" if spec else "pointer", base_utility=7.0 if spec else 5.0, recompute_cost=1.0 if spec else rc,
                    tokens=dict(pointer=sim.PH + 5, structured=min(c["structured"], s["size"]), compressed=min(c["compressed"], s["size"]), full=s["size"]))

    def step(self, sim, r):
        outs = sim.outputs()
        if not outs:
            return
        pages = [self._page(sim, s) for s in outs]
        # A resumed or rebased history arrives in one request, so `born` is
        # identical for old and new outputs. Model turns preserve their order
        # and keep parallel outputs from the newest turn together.
        newest = max(s["turn"] for s in outs)
        demands = [dict(page_id=str(s["id"]), required_repr="full") for s in outs if s["turn"] == newest]
        demands += [dict(page_id=str(s["id"]), required_repr="pointer") for s in outs if s["last"] >= r and s["turn"] != newest]
        for s in outs:
            self.recency.setdefault(str(s["id"]), 0.0)
        chosen, _, _ = select(pages, demands, self.policy, self.budget, self.recency)
        for s in outs:
            rep, form = chosen.get(str(s["id"]), "none"), s.get("form", "full")
            if rep == "full":
                if form != "full":
                    s["form"] = "full"; s.pop("kept", None); s.pop("kept_text", None)
            elif rep == "compressed":
                if form != "truncated":
                    s["form"] = "full"; sim.truncate(s, max(1, s["size"] // 3))
            elif rep == "structured":
                if form != "structured":
                    s["form"] = "full"; s.pop("kept", None); s.pop("kept_text", None)
                    if not sim.structure(s):
                        sim.to_placeholder([s])
            elif form not in ("memid", "placeholder", "memlabel"):
                sim.to_placeholder([s])
        touch_recency(self.recency, [d["page_id"] for d in demands])
