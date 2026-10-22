"""Request sizes in provider tokens, fitted from what the provider billed.

The tokenizer counts what the model reads in an item (`parts`): message text, a call's name and arguments, an
output's text, a reasoning summary; images at their dimension price. What it cannot see is fitted from the bill:
the framing each item kind adds, and the tokens an encrypted reasoning or compaction blob stands for. Per answered
request

    billed input tokens - Σ counted tokens = b + Σ_k o_k · n_k + Σ_k e_k · c_k

with n_k the items and c_k the encrypted characters of kind k (KINDS; reasoning before the last user message is a
kind of its own, so the fit decides whether the provider still bills it), b the rest of the request beyond the
counted instructions and tools. Within a session every n_k and c_k grows with the turns, so levels are nearly
collinear; o and e are fitted on the differences between consecutive answered requests (what each turn added or a
compression removed varies by kind), least squares with o, e >= 0; the prior holds only where the differences
say nothing (a ridge worth PRIOR_WEIGHT of one informative difference: a kind not yet seen keeps it); b is what the last answered request leaves over. A request about to be sent is estimated from the
last answered one, so what the provider billed stays exact and only the difference is modelled:

    size(next) = billed(last) + model(next) - model(last)
"""
from __future__ import annotations
import json
from collections import deque
from ctxpress.core import tokens

KINDS = ("message", "call", "output", "reasoning", "reasoning_old", "compaction", "other")
CALL_TYPES = ("function_call", "custom_tool_call")
OUT_TYPES = ("function_call_output", "custom_tool_call_output")
TEXT_PARTS = ("input_text", "output_text", "text")
# Prior tokens per encrypted character: 841 recorded Vanilla Codex turns (SWE-Milestone, 2026-10-16) carry a median
# 53.2 characters of encrypted_content per reasoning output token the provider reported.
ENCRYPTED_PRIOR = 1 / 53.2
DIM = 1 + 2 * len(KINDS)
PRIOR_WEIGHT = 1e-3


def _image_tokens(value):
    from ctxpress.core.hostsize import image_payloads, tokens_for_payload
    return sum(tokens_for_payload(p) for p in image_payloads(value))


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(x.get("text", "") for x in value if isinstance(x, dict) and x.get("type") in TEXT_PARTS)
    if isinstance(value, dict):
        return _text(value.get("content", value.get("body", "")))
    return "" if value is None else json.dumps(value, ensure_ascii=False)


def parts(item):
    """(kind, counted tokens, encrypted characters) of one Responses wire item; kind "user" is a user message."""
    t = item.get("type") or ("message" if "role" in item else None)
    enc = item.get("encrypted_content")
    enc = len(enc) if isinstance(enc, str) else 0
    if t == "message":
        kind = "user" if item.get("role") == "user" else "message"
        return kind, tokens.count(_text(item.get("content"))) + _image_tokens(item.get("content")), enc
    if t in CALL_TYPES:
        args = item.get("arguments", item.get("input", ""))
        args = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        return "call", tokens.count(item.get("name") or "") + tokens.count(args), enc
    if t in OUT_TYPES or (t or "").endswith("_call_output"):
        out = item.get("output")
        return "output", tokens.count(_text(out)) + _image_tokens(out), enc
    if t == "reasoning":
        text = "\n".join(p.get("text", "") for p in (item.get("summary") or []) + (item.get("content") or [])
                         if isinstance(p, dict))
        return "reasoning", tokens.count(text), enc
    if t in ("compaction", "compaction_summary"):
        return "compaction", 0, enc
    rest = {k: v for k, v in item.items() if k not in ("id", "encrypted_content", "status", "file_data")}
    return "other", tokens.count(json.dumps(rest, ensure_ascii=False)), enc


def media_tokens(item):
    """Tokens of the images in a wire item (priced by their dimensions)."""
    return _image_tokens(item.get("output") if "output" in item else item.get("content"))


def item_tokens(items, model):
    """Estimated tokens of each item of a request: counted plus the model's overhead for its kind (reasoning
    before the last user message as reasoning_old)."""
    rows = [parts(i) for i in items]
    last_user = max((i for i, r in enumerate(rows) if r[0] == "user"), default=-1)
    out = []
    for i, (kind, n, enc) in enumerate(rows):
        kind = "message" if kind == "user" else ("reasoning_old" if kind == "reasoning" and i < last_user else kind)
        out.append(n + model.overhead(kind, enc))
    return out


def fixed_tokens(body):
    """Counted tokens of a request apart from its input: instructions (Responses), the system prompt (Messages: a
    string or text blocks) and tool declarations."""
    if not body:
        return 0
    text = "\n".join(_text(body[k]) for k in ("instructions", "system") if body.get(k))
    return tokens.count(text) + (tokens.count(json.dumps(body["tools"], ensure_ascii=False)) if body.get("tools") else 0)


def features(rows):
    """rows: (kind, counted, encrypted chars) in request order -> (x, counted total). Reasoning before the last user
    message is reasoning_old; a row whose kind is "user" counts as a message."""
    last_user = max((i for i, r in enumerate(rows) if r[0] == "user"), default=-1)
    x, counted = [1.0] + [0.0] * (DIM - 1), 0
    for i, (kind, n, enc) in enumerate(rows):
        kind = "message" if kind == "user" else kind
        if kind == "reasoning" and i < last_user:
            kind = "reasoning_old"
        k = KINDS.index(kind)
        x[1 + k] += 1
        x[1 + len(KINDS) + k] += enc
        counted += n
    return x, counted


class SizeModel:
    def __init__(self):
        self.theta = [0.0] + [0.0] * len(KINDS) + [ENCRYPTED_PRIOR if k in ("reasoning", "reasoning_old", "compaction")
                                                  else 0.0 for k in KINDS]
        self.prior = list(self.theta)
        self.A = [[0.0] * DIM for _ in range(DIM)]   # normal equations of the differences (index 0 unused)
        self.b = [0.0] * DIM
        self.sq = [0.0] * DIM            # Σ Δx_j^2 and the differences where x_j changed: the ridge's scale
        self.changed = [0] * DIM
        self.n = 0                       # differences observed
        self.last = None                 # (x, counted, billed) of the last answered request
        self.seen = set()
        self.errors = deque(maxlen=128)  # billed - estimate of the recent answered requests, estimated before them

    def model(self, x, counted):
        return counted + sum(t * v for t, v in zip(self.theta, x))

    def overhead(self, kind, enc=0):
        k = KINDS.index(kind)
        return self.theta[1 + k] + self.theta[1 + len(KINDS) + k] * enc

    def estimate(self, x, counted):
        if self.last is None:
            return self.model(x, counted)
        lx, lc, billed = self.last
        return billed + self.model(x, counted) - self.model(lx, lc)

    def observe(self, identity, x, counted, billed):
        """One answered request: its features as sent and the input tokens the provider billed."""
        if identity in self.seen or type(billed) is not int or billed <= 0:
            return False
        self.seen.add(identity)
        if self.last is not None:
            self.errors.append(billed - self.estimate(x, counted))
            lx, lc, lb = self.last
            dx = [0.0] + [a - c for a, c in zip(x[1:], lx[1:])]
            dy = (billed - counted) - (lb - lc)
            for i in range(1, DIM):
                self.b[i] += dx[i] * dy
                self.sq[i] += dx[i] * dx[i]
                self.changed[i] += dx[i] != 0
                row = self.A[i]
                for j in range(1, DIM):
                    row[j] += dx[i] * dx[j]
            self.n += 1
            self._fit()
        self.last = (list(x), counted, billed)
        self.theta[0] = 0.0
        self.theta[0] = billed - self.model(x, counted)     # b: what the last answered request leaves over
        return True

    def envelope(self):
        """How far the recent estimates fell short of the bill: (largest shortfall in tokens, samples)."""
        return max([0.0, *self.errors]), len(self.errors)

    def _fit(self):
        lam = [PRIOR_WEIGHT * self.sq[j] / self.changed[j] if self.changed[j] else 1.0 for j in range(DIM)]
        A = [[self.A[i][j] + (lam[i] if i == j else 0.0) for j in range(DIM)] for i in range(DIM)]
        rhs = [self.b[i] + lam[i] * self.prior[i] for i in range(DIM)]
        A[0][0], rhs[0] = 1.0, 0.0                            # b is not fitted here
        th = nonneg_solve(A, rhs, free=())
        self.theta[1:] = th[1:]


def _solve(A, b, idx):
    """Solve A[idx, idx] t = b[idx] (Gaussian elimination, partial pivoting); a singular direction gets 0."""
    n = len(idx)
    M = [[A[i][j] for j in idx] + [b[i]] for i in idx]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12 * max(1.0, abs(A[idx[c]][idx[c]])):
            M[c] = [0.0] * c + [1.0] + [0.0] * (n - c - 1) + [0.0]   # degenerate: pin this coefficient
            for r in range(n):
                if r != c:
                    M[r][c] = 0.0
            continue
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c] / M[c][c]
                M[r] = [a - f * v for a, v in zip(M[r], M[c])]
    return [M[r][n] / M[r][r] for r in range(n)]


def nonneg_solve(A, rhs, free=(0,), start=None):
    """argmin_θ ½θᵀAθ - rhsᵀθ with θ_j >= 0 except j in `free` (A: the normal matrix XᵀX plus any ridge, positive
    semidefinite). Lawson-Hanson active set on the normal equations, columns scaled to a unit diagonal; exact."""
    n = len(rhs)
    d = [A[j][j] ** 0.5 if A[j][j] > 0 else 1.0 for j in range(n)]
    S = [[A[i][j] / (d[i] * d[j]) for j in range(n)] for i in range(n)]
    r = [rhs[i] / d[i] for i in range(n)]
    usable = [j for j in range(n) if A[j][j] > 0]
    passive = [j for j in usable if j in free]
    th = [0.0] * n
    if passive:
        for j, v in zip(passive, _solve(S, r, passive)):
            th[j] = v
    for _ in range(4 * n + 10):
        grad = [r[i] - sum(S[i][k] * th[k] for k in range(n)) for i in range(n)]
        cand = [j for j in usable if j not in passive and grad[j] > 1e-10 * (1 + abs(r[j]))]
        if not cand:
            break
        passive.append(max(cand, key=lambda j: grad[j]))
        while True:
            z = [0.0] * n
            for j, v in zip(passive, _solve(S, r, passive)):
                z[j] = v
            bad = [j for j in passive if j not in free and z[j] <= 0]
            if not bad:
                th = z
                break
            alpha = min(th[j] / (th[j] - z[j]) for j in bad if th[j] - z[j] > 0) if any(th[j] - z[j] > 0 for j in bad) else 0.0
            th = [t + alpha * (zz - t) for t, zz in zip(th, z)]
            passive = [j for j in passive if j in free or th[j] > 1e-12]
            for j in range(n):
                if j not in passive:
                    th[j] = 0.0
    return [t / d[j] for j, t in enumerate(th)]
