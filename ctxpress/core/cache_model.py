"""The provider's prompt cache as measured on real Codex runs (experiment-method.zh.html §9.7).

Measured on 122 finished SWE-Milestone jobs (about 45,000 requests, 2026-10-14 to 10-20, OpenAI Responses API):

  - A request whose history kept the previous request unchanged is served from cache for 99.2% of the previous
    input (median 99.7%; the rest is the last partial block), whatever the idle gap (0 s to 30 min observed;
    only 27 requests waited more than a minute).
  - After an edit, what stays cached lies between two points:
        lcp    the prefix unchanged since the previous request;
        point  the end of the longest earlier request whose whole prompt is still unchanged (cache points only
               at request ends).
    hit = point + rho * (lcp - point), where rho depends on how many of the previous 3 requests also changed the
    history: 0 -> 0.759, 1 -> 0.705, 2 -> 0.335, 3 -> 0.198. An occasional edit loses the cache from the edit on;
    edits on consecutive requests lose it back to the last intact request end. Repeated rewriting (Complexity Trap's
    masking) is why such methods hit the cache for only 39-58% of their input.

`Cache` keeps the request history of one session and answers both questions the methods need: what a request
actually hit (billing), and what a planned change would cost (planning)."""
from __future__ import annotations
import collections

UNCHANGED_HIT = 0.992
RHO = (0.759, 0.705, 0.335, 0.198)
BLOCK = 128


class Cache:
    """Prompt cache of one session. Requests are sequences of hashable item keys with their token sizes."""

    def __init__(self, fixed=0, rho=RHO, unchanged_hit=UNCHANGED_HIT, window=3):
        self.fixed = fixed                     # tokens ahead of the items (instructions, tools), never changed here
        self.rho, self.unchanged = tuple(rho), unchanged_hit
        self.prev = None                       # keys of the last request
        self.ends = []                         # (keys, tokens) of earlier requests, for cache points
        self.edits = collections.deque(maxlen=window)

    # ------------------------------------------------------------------ geometry
    @staticmethod
    def common(a, b):
        n = 0
        for x, y in zip(a, b):
            if x != y:
                break
            n += 1
        return n

    def point(self, keys, sizes):
        """Tokens up to the end of the longest earlier request that is a prefix of `keys`."""
        best = 0
        for k, tokens in self.ends:
            if len(k) <= len(keys) and tuple(keys[:len(k)]) == k:
                best = max(best, tokens)
        return best

    def expect(self, keys, sizes):
        """Expected cached tokens of a request with these items, given the history so far."""
        total = self.fixed + sum(sizes)
        if self.prev is None:
            return 0.0
        n = self.common(self.prev, keys)
        lcp = self.fixed + sum(sizes[:n])
        if n == len(self.prev):                # nothing earlier changed: the previous request is a prefix
            return self.unchanged * min(lcp, total)
        point = self.point(keys, sizes)
        k = min(len(self.rho) - 1, sum(self.edits))
        hit = point + self.rho[k] * max(0, lcp - point)
        return self.unchanged * (hit // BLOCK * BLOCK)

    def record(self, keys, sizes):
        """The request was sent: remember it as a cache point and whether it changed the history."""
        keys = tuple(keys)
        if self.prev is not None:
            self.edits.append(self.common(self.prev, keys) < len(self.prev))
        self.prev = keys
        self.ends.append((keys, self.fixed + sum(sizes)))
        if len(self.ends) > 64:                # old points are short prefixes; keep the newest and the first, the
            self.ends = self.ends[:1] + self.ends[-63:]   # head a summary falls back to

    # ------------------------------------------------------------------ planning
    def cost(self, keys, sizes, cached_price):
        """Expected cost of sending `keys` at this request, in uncached-token units."""
        total = self.fixed + sum(sizes)
        hit = self.expect(keys, sizes)
        return cached_price * hit + (total - hit)

    def rebuild(self, keys_now, sizes_now, keys_new, sizes_new, cached_price):
        """Extra cost of sending `keys_new` instead of `keys_now` at this request, in uncached-token units: the
        tokens that lose the cache, net of the tokens the change removes."""
        return self.cost(keys_new, sizes_new, cached_price) - self.cost(keys_now, sizes_now, cached_price)
