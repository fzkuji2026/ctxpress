"""FoldAct's inference window selector on a single shared-host trajectory.

Reference: SHAO-Jiaqi757/FoldAct@138a54dc6c2dce04ff8fccc92690aea8cd259368,
search_r1/llm_agent/generation.py, _apply_sliding_window. Apache-2.0.
The host supplies summaries, turn boundaries and token estimates. FoldAct
training losses, trained weights and batched token types are not reproduced.
Observation clipping uses the shared host token
counter; it is not an assertion of equality with the author's tokenizer.
When the host can measure rendered requests, explicit selection_tokens reserves
the fixed part's rendered overhead and then drops the oldest selected turns until
the rendered view fits. Protected pairs can still exceed the selection target;
the shared context guard enforces the final experimental maximum.
max_obs_length=None disables clipping and its forced three-turn window.
"""
import re

from ctxpress.methods.base import Method


def select_turns(turns, initial_tokens, max_model_len=16384, selection_tokens=None):
    """Author selection semantics; optionally override the max_model_len - 1000 limit."""
    selected = list(range(len(turns)))
    last_info = next((i for i in reversed(selected) if turns[i]['has_info']), -1)
    if last_info >= 0:
        summary = next((i for i in range(last_info, -1, -1) if turns[i]['has_summary']), None)
        if summary is not None:
            selected = list(range(summary, last_info + 1))
    limit = max_model_len - 1000 if selection_tokens is None else selection_tokens
    if initial_tokens + sum(turns[i]['length'] for i in selected) > limit:
        kept, accumulated = [], initial_tokens
        for i in reversed(selected):
            if accumulated + turns[i]['length'] > limit:
                break
            kept.insert(0, i)
            accumulated += turns[i]['length']
        selected = kept
    return selected


SUMMARY = re.compile(r'<\s*(think_summary|information_summary)\b[^>]*>.*?</\s*\1\s*>', re.I | re.S)


class FoldActSource(Method):
    source = 'arXiv 2512.22733'
    name = 'FoldAct (author window selector, host policy)'
    instructions = (
        'When accumulated interaction history should be folded, write a visible '
        '<information_summary>...</information_summary> or <think_summary>...</think_summary> '
        'block containing the evidence and state needed to continue, then continue using tools. '
        'Earlier turns may be removed after the next informative tool result. '
        'This uses the FoldAct inference window selector with your host policy, not trained FoldAct weights.')
    framework = dict(L1='无', L2='从最近摘要到最近信息轮选择上下文', L3='超预算按完整轮次从后截取',
                     cross='用户指令、媒体与未完成调用保留', memory='无', decider='宿主摘要、作者窗口规则')

    def __init__(self, max_model_len=16384, max_obs_length=4096, selection_tokens=None):
        if type(max_model_len) is not int or max_model_len <= 1000:
            raise ValueError('max_model_len must be an integer greater than 1000')
        self.max_model_len = max_model_len
        if max_obs_length is not None and (type(max_obs_length) is not int or max_obs_length <= 0):
            raise ValueError('max_obs_length must be a positive integer or None')
        self.max_obs_length = max_obs_length
        if selection_tokens is not None and (type(selection_tokens) is not int or selection_tokens <= 0):
            raise ValueError('selection_tokens must be a positive integer or None')
        self.selection_tokens = selection_tokens

    def reset(self, sim):
        self.history = []
        self.processed = set()
        self.force_summarized = False
        self.clipped = {}

    def _observations(self, sim, groups):
        if self.max_obs_length is None:
            return
        # The author clips the concatenated observation prefix, not head/tail.
        # Process each completed host turn once, including resumed histories.
        count = sim.tokens
        for group in groups:
            outputs = [s for s in group if s['seg'] == 'out']
            fresh = [s for s in outputs if s['id'] not in self.processed]
            if not fresh:
                continue
            remaining = self.max_obs_length
            total = sum(count(sim.current_text(s) or '') for s in outputs)
            self.force_summarized |= total > self.max_obs_length
            for s in outputs:
                text = sim.current_text(s) or ''
                size = count(text)
                if size > remaining and not s.get('protected') and not s.get('has_media'):
                    lo, hi = 0, len(text)
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        if count(text[:mid]) <= remaining:
                            lo = mid
                        else:
                            hi = mid - 1
                    sim.keep_text(s, text[:lo], form='truncated')
                    self.clipped[s['id']] = text[:lo]
                remaining = max(0, remaining - size)
                self.processed.add(s['id'])

    def step(self, sim, r):
        if not hasattr(self, 'history'):
            self.reset(sim)
        known = {id(s) for s in self.history}
        self.history.extend(s for s in sim.ctx if id(s) not in known)
        sim.reinstate(self.history)
        for item in sim.ctx:
            if item['id'] in self.clipped and item.get('form') == 'full':
                sim.keep_text(item, self.clipped[item['id']], form='truncated')
        groups = {}
        fixed = []
        for s in sim.ctx:
            if s.get('protected') or s.get('role') in ('system', 'developer', 'user'):
                fixed.append(s)
            else:
                groups.setdefault(s.get('turn', 0), []).append(s)
        groups = list(groups.values())
        self._observations(sim, groups)
        turns = []
        for group in groups:
            observations = sum(sim.seg_size(s) for s in group if s['seg'] == 'out')
            summary = any(s['seg'] == 'msg' and s.get('role') == 'assistant' and
                          SUMMARY.search(sim.current_text(s) or '') for s in group)
            turns.append(dict(length=sum(sim.seg_size(s) for s in group),
                              has_info=observations > 20, has_summary=summary))
        initial_tokens = getattr(sim, 'prefix', 0) + sum(sim.seg_size(s) for s in fixed)
        measure = getattr(sim, 'measure_history', None) if self.selection_tokens is not None else None
        if not callable(measure):
            measure = None
        if measure:
            # Reserve what the fixed part renders beyond size() (tool declarations, metadata, serialization).
            # Measured on the fixed items only: measuring the whole history would grow the reserve with every
            # item later deleted, until nothing but the fixed head fits.
            initial_tokens = max(initial_tokens, measure(fixed))
        offset = max(0, len(turns) - 3) if self.force_summarized else 0
        selected = [i + offset for i in select_turns(
            turns[offset:], initial_tokens,
            self.max_model_len, selection_tokens=self.selection_tokens)]
        if measure:
            selected = self._fit(sim, groups, fixed, selected, measure)
        sim.delete(self._removal(sim, groups, set(selected)))

    def _fit(self, sim, groups, fixed, selected, measure):
        """Drop the oldest selected turns until the rendered selection fits selection_tokens (binary search).
        Pairs kept only for API safety stay outside the measured selection, as with the author estimate."""
        base = {s['id'] for s in fixed}

        def fits(k):
            chosen = base | {s['id'] for i in selected[k:] for s in groups[i]}
            return measure([s for s in sim.ctx if s['id'] in chosen]) <= self.selection_tokens
        lo, hi = 0, len(selected)
        while lo < hi:
            mid = (lo + hi) // 2
            if fits(mid):
                hi = mid
            else:
                lo = mid + 1
        return selected[lo:]

    def _removal(self, sim, groups, selected):
        removed = [s for i, group in enumerate(groups) if i not in selected for s in group]
        # Never corrupt the native API when a tool spans request boundaries or
        # carries media. This intentionally differs from token-level truncation.
        protected_pairs = {s.get('call_id') for s in sim.ctx if s.get('protected') or s.get('has_media')}
        candidate_ids = {s['id'] for s in removed}
        pairs = {}
        for s in sim.ctx:
            if s.get('call_id'):
                pairs.setdefault(s['call_id'], []).append(s)
        for cid, items in pairs.items():
            if ({s['seg'] for s in items} != {'call', 'out'} or
                    any(s['id'] not in candidate_ids for s in items)):
                protected_pairs.add(cid)
        return [s for s in removed if not s.get('has_media') and
                (not s.get('call_id') or s['call_id'] not in protected_pairs)]
