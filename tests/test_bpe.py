"""The pure-Python o200k_base gives tiktoken's tokens, and ctxpress counts with it where tiktoken is missing."""
import random, sys
import pytest
from ctxpress.core import bpe, tokens

TEXTS = ["Don't I'LL we'Re 'S", "def f(x):\n    return x  # ok\r\n\r\n", "x" * 3000, "=" * 5000, "　 \t\n a",
         "😀👍🏽 中文测试 العربية हिन्दी", "1234567 ٣٤٥ ⅫⅣ", "á̂b MixedCASEWord HTTPServer", "\ud800 lone",
         '{"type": "function_call_output", "output": "FAILED tests/test_a.py::test_b - AssertionError"}', ""]


def generated():
    rng = random.Random(3)
    return ["".join(chr(rng.choice([rng.randrange(0x20, 0x3000), rng.randrange(0x3000, 0x30000)]))
                    for _ in range(rng.randrange(1, 40))) for _ in range(300)]


def test_the_same_tokens_as_tiktoken():
    tiktoken = pytest.importorskip("tiktoken")
    ref, ours = tiktoken.get_encoding("o200k_base"), bpe.Encoding()
    for text in TEXTS + generated():
        ids = ours.encode(text, disallowed_special=())
        assert ids == ref.encode(text, disallowed_special=()), repr(text[:60])
        assert ours.decode(ids) == ref.decode(ids) and ours.decode_tokens_bytes(ids) == ref.decode_tokens_bytes(ids)
    assert ours.encode("<|endoftext|>") == ref.encode("<|endoftext|>", disallowed_special=())   # read as text


def test_counts_fall_back_to_pure_python_without_tiktoken(monkeypatch):
    expected = [tokens.count(t) for t in TEXTS]
    monkeypatch.setitem(sys.modules, "tiktoken", None)          # import tiktoken raises ImportError
    tokens.encoding.cache_clear(), tokens.count.cache_clear()
    try:
        assert isinstance(tokens.encoding(), bpe.Encoding)
        assert [tokens.count(t) for t in TEXTS] == expected and tokens.head("one two three", 2) == "one two..."
    finally:
        tokens.encoding.cache_clear(), tokens.count.cache_clear()


def test_the_vocabulary_is_checked_by_its_hash(tmp_path, monkeypatch):
    bad = tmp_path / "vocab"
    bad.write_bytes(b"YQ== 0\n")
    monkeypatch.setenv("CTXPRESS_TOKENIZER_FILE", str(bad))
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(bpe, "__file__", str(tmp_path / "bpe.py"))
    with pytest.raises(FileNotFoundError, match="o200k_base"):
        bpe.vocab_bytes()
