"""Token counts: every size in ctxpress is counted with the o200k_base tokenizer (tiktoken), the published tokenizer
of OpenAI's current models. There is no character estimate: without tiktoken and its vocabulary nothing that needs
a size runs. What the tokenizer cannot see (item framing, encrypted reasoning) is fitted from the provider's bill
(ctxpress.core.sizes)."""
from __future__ import annotations
import functools, os


@functools.lru_cache(maxsize=1)
def encoding():
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", os.path.join(os.path.expanduser("~"), ".cache", "tiktoken"))
    import tiktoken
    return tiktoken.get_encoding("o200k_base")


@functools.lru_cache(maxsize=65536)
def count(text):
    """Tokens of a text."""
    return len(encoding().encode(text, disallowed_special=())) if text else 0


def head(text, n, mark="..."):
    """The first n tokens of a text, `mark` after them when cut (n <= 0: all of it)."""
    text = text or ""
    if n <= 0:
        return text
    ids = encoding().encode(text, disallowed_special=())
    return text if len(ids) <= n else encoding().decode(ids[:n]) + mark


def head_tail(text, n, head_share=0.6):
    """First and last lines of a text within n tokens (the marker line included), whole lines only."""
    text = text or ""
    if count(text) <= n:
        return text
    lines = text.split("\n")
    sizes = [count(l + "\n") for l in lines]
    budget = n - count("[... 0000000 lines truncated ...]\n")
    h_budget = int(budget * head_share)
    first, used = [], 0
    for l, k in zip(lines, sizes):
        if used + k > h_budget:
            break
        first.append(l)
        used += k
    last = []
    for l, k in zip(reversed(lines[len(first):]), reversed(sizes[len(first):])):
        if used + k > budget:
            break
        last.append(l)
        used += k
    cut = len(lines) - len(first) - len(last)
    if not first and not last:                    # a single line longer than n: cut inside it
        return head(text, max(1, budget), mark=f"\n[... {cut} lines truncated ...]")
    return "\n".join(first + [f"[... {cut} lines truncated ...]"] + list(reversed(last)))


def tail(text, n):
    """The last n tokens of a text (n <= 0: all of it)."""
    text = text or ""
    if n <= 0:
        return text
    ids = encoding().encode(text, disallowed_special=())
    return text if len(ids) <= n else encoding().decode(ids[-n:])


def split(text, n):
    """The text in consecutive pieces of at most n tokens each, cut only between characters."""
    text = text or ""
    if not text:
        return []
    e = encoding()
    pieces, cur, k = [], b"", 0
    for b in e.decode_tokens_bytes(e.encode(text, disallowed_special=())):
        cur += b; k += 1
        if k >= n:
            try:
                pieces.append(cur.decode("utf-8")); cur, k = b"", 0
            except UnicodeDecodeError:              # a character continues in the next token
                pass
    if cur:
        pieces.append(cur.decode("utf-8", "replace"))
    return pieces
