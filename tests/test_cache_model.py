"""The provider cache model (core/cache_model.py) and the cost chooser of infinite-context that uses it (§9.7)."""
from ctxpress.core.cache_model import Cache, RHO, UNCHANGED_HIT, BLOCK
from ctxpress.methods import InfiniteContext
from test_infinite_v5 import requests, run, text


def keys(n):
    return [("i", k) for k in range(n)]


def test_append_only_hits_the_previous_request():
    c = Cache(fixed=1000)
    c.record(keys(10), [500] * 10)
    assert c.expect(keys(12), [500] * 12) == UNCHANGED_HIT * 6000


def test_an_occasional_edit_keeps_most_of_the_unchanged_prefix():
    c = Cache(fixed=0)
    for n in (4, 8, 12):
        c.record(keys(n), [1000] * n)
    edited = keys(12)
    edited[6] = ("i", "placeholder")
    hit = c.expect(edited, [1000] * 12)
    point = 4000                                    # request 1 is the longest earlier request still a prefix
    assert hit == UNCHANGED_HIT * ((point + RHO[0] * (6000 - point)) // BLOCK * BLOCK)


def test_consecutive_edits_fall_back_to_the_last_intact_request_end():
    c = Cache(fixed=0)
    c.record(keys(4), [1000] * 4)
    seq = keys(8)
    for i in range(4):                              # every request rewrites a later output, as masking does
        seq = list(seq) + [("n", i)]
        seq[4 + i] = ("m", i)
        c.record(seq, [1000] * len(seq))
    nxt = list(seq) + [("n", 9)]
    nxt[8] = ("m", 9)
    hit = c.expect(nxt, [1000] * len(nxt))
    lcp = 8000
    assert hit == UNCHANGED_HIT * ((4000 + RHO[3] * (lcp - 4000)) // BLOCK * BLOCK)
    assert hit < 0.6 * lcp


def test_rebuild_prices_a_change_against_leaving_it():
    c = Cache(fixed=0)
    c.record(keys(10), [1000] * 10)
    same = c.rebuild(keys(10), [1000] * 10, keys(10), [1000] * 10, 0.1)
    early = list(keys(10)); early[1] = ("x", 1)
    late = list(keys(10)); late[8] = ("x", 8)
    shrink = lambda k: [1000 if x[0] == "i" else 10 for x in k]
    assert same == 0
    assert c.rebuild(keys(10), [1000] * 10, early, shrink(early), 0.1) > c.rebuild(keys(10), [1000] * 10, late, shrink(late), 0.1)


def test_cost_chooser_runs_and_never_exceeds_the_hard_limit():
    sent = run(lambda: InfiniteContext(version=5, t=1000, choose="cost"))
    assert len(sent) == len(requests())
    cliff_like = run(lambda: InfiniteContext(version=5, t=1000))
    assert sum(len(text(x)) for x in sent) <= sum(len(text(x)) for x in requests())
    assert any(text(a) != text(b) for a, b in zip(sent, requests()))
    assert cliff_like


def test_cost_chooser_keeps_specs_and_holds_by_default():
    m = InfiniteContext(version=5, t=1000, choose="cost")
    assert m.spec.guard and m.spec.protect_spec and m.spec.recall
    assert m.spec.hold == "window" and m.spec.supersede and m.spec.guard_keep_frac == 0.1
    assert m.spec.cached_price == 0.05 and m.spec.output_price == 5.0 and m.spec.candidates == ("summary",)
    assert m.spec.summary_at == "boundary" and m.spec.boundary_late == 1.0
    assert m.spec.writers == ("hybrid",) and m.writer_model == "deepseek-flash"
    assert "成本模型" in m.framework["decider"]


def test_summary_waits_for_rent_then_pays():
    """A summary right after the last one pays no rent and is refused; the same summary after a long cycle pays."""
    m = InfiniteContext(version=5, t=1000, choose="cost", summary_at="any", cached_price=0.1)
    m._cache = Cache(fixed=0)
    m._cache.record(keys(10), [100] * 10)
    now, new = (keys(10), [100] * 10), ([("s", 0)], [200])

    class Sim:
        fixed_chars, fixed_tokens, prefix = 0, 0, 0
    m._sizes, m._cycle = [1000], 0
    young = m._summary_net(Sim(), now, new, [])
    m._sizes = [300 + 50 * k for k in range(15)]
    old = m._summary_net(Sim(), now, new, [])
    assert young < 0 < old


def test_cost_chooser_can_wait_for_a_topic_change():
    m = InfiniteContext(version=5, t=1000, choose="cost", summary_at="topic")
    assert m.spec.summary_at == "topic"
    assert run(lambda: InfiniteContext(version=5, t=1000, choose="cost", summary_at="topic"))


def test_boundary_takes_a_topic_change_early_and_waits_longer_without_one():
    """Rent between 0.5 F and F: a summary pays at a recent topic change, not elsewhere; above F it pays."""
    m = InfiniteContext(version=5, t=1000, choose="cost")
    m._cache = Cache(fixed=0)
    m._cache.record(keys(10), [100] * 10)
    now, new = (keys(10), [100] * 10), ([("s", 0)], [200])

    class Sim:
        fixed_chars, fixed_tokens, prefix = 0, 0, 0
    first = m._cache.cost(*new, 0.05) - 0.05 * 200
    m._cycle, m._sizes = 0, [200 + 800] * int(0.5 * first / (0.05 * 800) + 1)   # rent just above F / 2
    m._topic_req = None
    assert m._summary_net(Sim(), now, new, []) < 0
    m._topic_req = len(m._sizes) - 1
    assert m._summary_net(Sim(), now, new, []) > 0
    m._topic_req = None
    m._sizes = [1000] * int(1.0 * first / (0.05 * 800) + 2)                        # rent above F
    assert m._summary_net(Sim(), now, new, []) > 0
