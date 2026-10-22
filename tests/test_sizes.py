"""Request sizes in tokens: the tokenizer, the size model fitted on billed input tokens, and its use by the live
context and the rewriter (ctxpress.core.tokens, ctxpress.core.sizes)."""
import random

from ctxpress.core import sizes, tokens
from ctxpress.core.trace import fit_overhead
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import HostDefault

# A provider with a known billing rule: counted text, a framing per item kind, encrypted reasoning at 1/40 token per
# character while it follows the last user message, older reasoning not billed, and a fixed part.
FRAME = dict(message=4, call=7, output=6, reasoning=3, reasoning_old=0, compaction=2, other=5)
PER_ENC = dict(reasoning=1 / 40, reasoning_old=0.0, compaction=1 / 40)
FIXED = 300


def billed(body):
    x, counted = sizes.features([sizes.parts(i) for i in body["input"]])
    n = {k: x[1 + i] for i, k in enumerate(sizes.KINDS)}
    enc = {k: x[1 + len(sizes.KINDS) + i] for i, k in enumerate(sizes.KINDS)}
    return round(FIXED + counted + sizes.fixed_tokens(body)
                 + sum(FRAME[k] * n[k] + PER_ENC.get(k, 0) * enc[k] for k in sizes.KINDS))


def words(rng, n):
    return " ".join(rng.choice(["alpha", "beta", "parser", "cache", "测试", "def f(x):", "{\"k\": 1}", "FAIL"])
                    for _ in range(n))


def conversation(rng, turns, users=3):
    items = [dict(type="message", role="developer", content=[dict(type="input_text", text="rules " * 40)]),
             dict(type="message", role="user", content=[dict(type="input_text", text=words(rng, 50))])]
    bodies = []
    for t in range(turns):
        if t and t % (turns // users) == 0:
            items.append(dict(type="message", role="user", content=[dict(type="input_text", text=words(rng, 30))]))
        if rng.random() < 0.7:               # kinds vary independently, so each framing is identifiable
            items.append(dict(type="reasoning", id=f"r{t}", summary=[],
                              encrypted_content="x" * rng.randint(400, 4000)))
        if rng.random() < 0.3:
            items.append(dict(type="message", role="assistant", content=[dict(type="output_text", text=words(rng, 20))]))
        for j in range(rng.randint(1, 3)):
            items.append(dict(type="function_call", call_id=f"c{t}.{j}", name="shell",
                              arguments='{"cmd": "sed -n 1,80p a%d.py"}' % t))
            if rng.random() < 0.8:
                items.append(dict(type="function_call_output", call_id=f"c{t}.{j}", output=words(rng, rng.randint(20, 400))))
            else:
                items.append(dict(type="function_call_output", call_id=f"c{t}.{j}", output=""))
        bodies.append(dict(model="m", instructions="be brief", tools=[], input=[dict(i) for i in items]))
    return bodies


def test_tokenizer_helpers():
    text = "\n".join(f"line {i} 测试 😀" for i in range(400))
    cut = tokens.head_tail(text, 120)
    assert tokens.count(cut) <= 120 and "lines truncated" in cut
    assert tokens.count(tokens.head(text, 50, mark="")) == 50
    assert tokens.count(tokens.tail(text, 40)) <= 40
    assert "".join(tokens.split(text, 64)) == text
    assert all(tokens.count(p) <= 66 for p in tokens.split(text, 64))


def test_fit_recovers_the_billing_rule():
    """What the bill identifies is recovered: message framing (messages vary on their own), call plus output
    framing (they come in pairs), and the requests' sizes; older reasoning, never billed, gets no rate."""
    rng = random.Random(1)
    bodies = conversation(rng, 160)
    m = sizes.SizeModel()
    for i, body in enumerate(bodies[:120]):
        x, counted = sizes.features([sizes.parts(it) for it in body["input"]])
        m.observe(i, x, counted + sizes.fixed_tokens(body), billed(body))
    assert abs(m.overhead("message") - FRAME["message"]) < 1
    assert abs(m.overhead("call") + m.overhead("output") - FRAME["call"] - FRAME["output"]) < 1
    assert m.theta[1 + len(sizes.KINDS) + sizes.KINDS.index("reasoning_old")] < 0.002
    for body in bodies[120:]:                                   # requests the fit has not seen
        x, counted = sizes.features([sizes.parts(it) for it in body["input"]])
        assert abs(m.model(x, counted + sizes.fixed_tokens(body)) - billed(body)) < 0.002 * billed(body)


def test_rewriter_estimates_the_next_bill():
    """The estimate of the next request (appended items, a new user message) against what the provider bills: within
    0.2% once the fit has 20 requests, except the first request after a new user message (old reasoning is a kind
    the fit has not seen until then); an unchanged request is exactly the last bill."""
    rng = random.Random(2)
    rw = Rewriter(HostDefault)
    errors, users = [], 1
    for i, body in enumerate(conversation(rng, 60)):
        out, info = rw.rewrite_body(body, "s")
        bill = billed(out)
        n = sum(1 for x in out["input"] if x.get("role") == "user")
        if i >= 20 and n == users:
            errors.append(abs(info["rendered_prompt_tokens"] - bill) / bill)
        users = n
        rw.observe_response(dict(info, status=200, usage=dict(input_tokens=bill)))
    assert max(errors) < 0.002, max(errors)
    again, info = rw.rewrite_body(body, "s")
    assert info["rendered_prompt_tokens"] == billed(again)


def test_live_sizes_follow_compression():
    """A truncated output and a summary change the estimate by what they change on the wire."""
    rng = random.Random(3)
    rw = Rewriter(HostDefault)
    bodies = conversation(rng, 40, users=2)
    for body in bodies:
        out, info = rw.rewrite_body(body, "s")
        rw.observe_response(dict(info, status=200, usage=dict(input_tokens=billed(out))))
    ctx = rw.sessions["s"].ctx
    before = ctx.request_tokens()
    big = max(ctx.outputs(), key=lambda s: s["content"])
    old = ctx.seg_size(big)
    assert ctx.truncate(big, 40)
    assert abs((before - ctx.request_tokens()) - (old - ctx.seg_size(big))) < 1e-6
    assert ctx.seg_size(big) <= 40 + 1
    members = ctx.outputs()[:5]
    removed = sum(ctx.seg_size(s) for s in members)
    text = "summary of five outputs " * 10
    s = ctx.summarize_segment(members, text=text, billed=False)
    assert s is not None
    expected = before - (old - ctx.seg_size(big)) - removed + ctx.message_tokens(text)
    assert abs(ctx.request_tokens() - expected) < 1e-6


def test_replay_overhead_fit():
    """Replay traces: per-kind overhead and the reasoning rate are fitted on the recorded input tokens."""
    rng = random.Random(4)
    reqs, total, prefix = [], 0, 500
    for r in range(80):
        before = ([dict(seg="call", size=rng.randint(10, 60)) for _ in range(rng.randint(1, 3))]
                  + [dict(seg="out", size=rng.randint(20, 900)) for _ in range(rng.randint(0, 3))]
                  + [dict(seg="reason", size=0, enc=rng.randint(50, 600)) for _ in range(rng.randint(0, 1))])
        total += sum(s["size"] + {"call": 7, "out": 5, "reason": 0}[s["seg"]] + 0.8 * s.get("enc", 0) for s in before)
        reqs.append(dict(input=round(prefix + total), cached=0, out_tokens=0, before=before))
    got_prefix, fit = fit_overhead(reqs)
    assert abs(got_prefix - prefix) <= 3
    assert abs(fit["overhead"]["call"] - 7) < 0.5 and abs(fit["overhead"]["out"] - 5) < 0.5
    assert abs(fit["per_enc"]["reason"] - 0.8) < 0.01
