"""o200k_base in pure Python: the same tokens as tiktoken, for interpreters where tiktoken is not installed or not
built (the Agent containers of a run carry their own Python, which sees only the ctxpress runtime).

tiktoken splits a text with the o200k_base pattern and merges the UTF-8 bytes of each piece by rank (lowest rank
first, leftmost on ties). The pattern's Unicode classes (\\p{L}, \\p{N}, ...) are expanded from unicodedata, and \\s
is White_Space as in tiktoken's Rust regex. The vocabulary is OpenAI's o200k_base.tiktoken file, checked against the
hash tiktoken itself checks."""
from __future__ import annotations
import base64, functools, hashlib, heapq, os, re, sys, unicodedata
from pathlib import Path

VOCAB = "o200k_base.tiktoken"
VOCAB_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
VOCAB_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
WHITE_SPACE = "\t\n\x0b\x0c\r \x85\xa0  -     　"
INF = float("inf")


def vocab_paths():
    """Where the vocabulary may be, in order: CTXPRESS_TOKENIZER_FILE; next to this module (a run's runtime gets a
    copy there, ctxpress.harness.jobs.queue); tiktoken's cache (its file name is the SHA-1 of the URL)."""
    cache = os.environ.get("TIKTOKEN_CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "tiktoken")
    found = [os.environ.get("CTXPRESS_TOKENIZER_FILE"), str(Path(__file__).resolve().with_name(VOCAB)),
             os.path.join(cache, hashlib.sha1(VOCAB_URL.encode()).hexdigest())]
    return [p for p in found if p]


def vocab_bytes():
    for path in vocab_paths():
        try:
            data = Path(path).read_bytes()
        except OSError:
            continue
        if hashlib.sha256(data).hexdigest() == VOCAB_SHA256:
            return data
    raise FileNotFoundError("o200k_base vocabulary not found (or its hash differs) in: " + ", ".join(vocab_paths()))


def _ranges(test):
    out, start, last = [], None, None
    for c in range(sys.maxunicode + 1):
        if test(c):
            if start is None:
                start = c
            last = c
        elif start is not None:
            out.append((start, last))
            start = None
    if start is not None:
        out.append((start, last))
    return "".join(re.escape(chr(a)) if a == b else re.escape(chr(a)) + "-" + re.escape(chr(b)) for a, b in out)


@functools.lru_cache(maxsize=1)
def pattern():
    cats = [unicodedata.category(chr(c)) for c in range(sys.maxunicode + 1)]
    cls = {k: _ranges(lambda c, k=k: cats[c] in k) for k in (
        ("Lu", "Ll", "Lt", "Lm", "Lo"), ("Nd", "Nl", "No"), ("Lu", "Lt", "Lm", "Lo", "Mn", "Mc", "Me"),
        ("Ll", "Lm", "Lo", "Mn", "Mc", "Me"))}
    L, N, UP, LO = cls.values()
    S = WHITE_SPACE
    contraction = "(?i:'s|'t|'re|'ve|'m|'ll|'d)?"
    return re.compile("|".join([
        f"[^\\r\\n{L}{N}]?[{UP}]*[{LO}]+{contraction}",
        f"[^\\r\\n{L}{N}]?[{UP}]+[{LO}]*{contraction}",
        f"[{N}]{{1,3}}",
        f" ?[^{S}{L}{N}]+[\\r\\n/]*",
        f"[{S}]*[\\r\\n]+",
        f"[{S}]+(?![^{S}])",
        f"[{S}]+",
    ]))


class Encoding:
    name = "o200k_base"

    def __init__(self, data=None):
        self.ranks, self.tokens = {}, {}
        for line in (data or vocab_bytes()).splitlines():
            if line:
                token, rank = line.split()
                token = base64.b64decode(token)
                self.ranks[token] = int(rank)
                self.tokens[int(rank)] = token
        self.pattern = pattern()
        self._piece = functools.lru_cache(maxsize=1 << 16)(self._merge)

    def encode(self, text, disallowed_special=()):
        """Tokens of a text, special tokens read as ordinary text (as tiktoken with disallowed_special=())."""
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:                     # lone surrogates: tiktoken's own replacement
            text = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        out = []
        for piece in self.pattern.findall(text):
            out.extend(self._piece(piece.encode("utf-8")))
        return out

    encode_ordinary = encode

    def decode_tokens_bytes(self, ids):
        return [self.tokens[i] for i in ids]

    def decode(self, ids, errors="replace"):
        return b"".join(self.tokens[i] for i in ids).decode("utf-8", errors)

    def _merge(self, piece):
        """tiktoken's byte pair merge: join the adjacent pair whose join has the lowest rank, leftmost on ties."""
        ranks = self.ranks
        if piece in ranks:
            return (ranks[piece],)
        n = len(piece)
        nxt = list(range(1, n + 1))                 # parts are identified by their first byte
        prv = list(range(-1, n - 1))
        heap = [(ranks.get(piece[i:i + 2], INF), i, i + 1, i + 2) for i in range(n - 1)]
        heap = [h for h in heap if h[0] != INF]
        heapq.heapify(heap)
        alive = [True] * n
        while heap:
            _, i, k, e = heapq.heappop(heap)
            if not alive[i] or nxt[i] != k or k >= n or nxt[k] != e:
                continue                              # a stale pair: one side was merged since
            nxt[i] = e
            alive[k] = False
            if e < n:
                prv[e] = i
                r = ranks.get(piece[i:nxt[e]])
                if r is not None:
                    heapq.heappush(heap, (r, i, e, nxt[e]))
            p = prv[i]
            if p >= 0:
                r = ranks.get(piece[p:nxt[i]])
                if r is not None:
                    heapq.heappush(heap, (r, p, i, nxt[i]))
        out, i = [], 0
        while i < n:
            out.append(ranks[piece[i:nxt[i]]])
            i = nxt[i]
        return tuple(out)
