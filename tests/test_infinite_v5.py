"""infinite-context V5: dispatch, the Cliff special case, and what single switches change."""
import copy, json
import pytest
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import CliffCompaction, InfiniteContext, build
from ctxpress.methods.infinite_v5 import InfiniteContextV5


def session(turns=12, long_chars=3000):
    items = [dict(role='user', content=[dict(type='input_text', text='Fix the failing parser test.')])]
    for i in range(turns):
        items.append(dict(type='function_call', name='shell', call_id=f'c{i}', arguments=json.dumps(dict(cmd=f'cat f{i}.py'))))
        out = (f'line {i} ' * long_chars)[:long_chars] if i % 2 == 0 else f'ok {i}'
        items.append(dict(type='function_call_output', call_id=f'c{i}', output=out))
        items.append(dict(type='message', role='assistant', content=[dict(type='output_text', text=f'step {i} done')]))
        if i == 3:
            items.append(dict(role='user', content=[dict(type='input_text', text='Also keep the public API unchanged.')]))
    return items


def requests():
    full = session()
    # one request per growing prefix, ending after each tool output, as a host sends them
    return [full[:n] for n in range(2, len(full) + 1) if full[n - 1].get('type') == 'function_call_output']


def run(factory):
    rw, sent = Rewriter(factory), []
    for inp in requests():
        body = dict(model='gpt-5', instructions='sys', input=copy.deepcopy(inp), tools=[], stream=True)
        out, _ = rw.rewrite_body(body, 's')
        sent.append(out['input'])
    return sent


def text(inp):
    return json.dumps(inp, ensure_ascii=False)


def test_dispatch_and_switch_validation():
    assert type(InfiniteContext(version=5)) is InfiniteContextV5
    assert type(build({'class': 'infinite-context', 'args': {'version': 5, 't': 50000}})).__name__ == 'InfiniteContextV5'
    assert type(InfiniteContext()) is InfiniteContext
    with pytest.raises(ValueError):
        InfiniteContext(keep_recent=2)
    with pytest.raises(ValueError):
        InfiniteContext(version=5, no_such_switch=1)
    with pytest.raises(ValueError):
        InfiniteContext(version=5, writer='poetry')


def test_defaults_are_cliff_request_for_request():
    cliff, v5 = run(lambda: CliffCompaction(t=1000)), run(lambda: InfiniteContext(version=5, t=1000))
    assert any(a != b for a, b in zip(cliff, [r for r in requests()]))  # Cliff did compact
    assert [text(x) for x in cliff] == [text(x) for x in v5]


def test_recall_leaves_references_where_cliff_drops_outputs():
    last = run(lambda: InfiniteContext(version=5, t=1000, recall=True))[-1]
    assert '[archived #' in text(last)
    assert '[archived #' not in text(run(lambda: CliffCompaction(t=1000))[-1])


def test_protect_user_keeps_later_user_message_verbatim():
    req = 'Also keep the public API unchanged.'
    cliff = run(lambda: CliffCompaction(t=1000))[-1]
    kept = run(lambda: InfiniteContext(version=5, t=1000, protect_user=True))[-1]
    as_message = lambda inp: any(x.get('role') == 'user' and req in text(x.get('content')) for x in inp)
    assert not as_message(cliff) and as_message(kept)


def test_cost_decider_defers_when_saving_is_small():
    never = run(lambda: InfiniteContext(version=5, t=1000, decider='cost', horizon=1, cached_price=0.0))
    assert [text(x) for x in never] == [text(x) for x in requests()]


def code_mode_session():
    items = [dict(role='user', content=[dict(type='input_text', text='Fix the failing parser test.')])]
    for i in range(12):
        items.append(dict(type='custom_tool_call', name='exec', call_id=f'c{i}', input=f'await tools.exec_command({{cmd:"cat f{i}.py"}})'))
        out = 'x' * 3000 if i % 2 else f'short result {i}'
        items.append(dict(type='custom_tool_call_output', call_id=f'c{i}', output=[dict(type='input_text', text=out)]))
        items.append(dict(type='message', role='assistant', content=[dict(type='output_text', text=f'step {i} done')]))
    return [items[:n] for n in range(2, len(items) + 1) if items[n - 1].get('type') == 'custom_tool_call_output']


def test_tool_dialect_full_keeps_code_mode_results_and_calls():
    def last(factory):
        rw = Rewriter(factory)
        for inp in code_mode_session():
            out, _ = rw.rewrite_body(dict(model='gpt-5', instructions='sys', input=copy.deepcopy(inp), tools=[], stream=True), 's')
        return text(out['input'])
    wire, full = last(lambda: CliffCompaction(t=1000)), last(lambda: InfiniteContext(version=5, t=1000, tool_dialect='full'))
    assert last(lambda: InfiniteContext(version=5, t=1000)) == wire
    # the summary keeps only what came after the previous one, so look at the latest summary's lines
    assert 'result: ' not in wire and '[exec] await' not in wire     # Cliff's wire dialect: empty signature, no result
    assert 'result: short result ' in full and '[exec] await tools.exec_command' in full


def work_session(long_chars=6000, user_at=None):
    """read -> edit -> test cycles over different files; returns one request per tool output."""
    items = [dict(role='user', content=[dict(type='input_text', text='Fix the failing parser test.')])]
    n = 0
    for cycle in range(4):
        steps = [('shell', json.dumps(dict(cmd=f'cat pkg{cycle}/mod.py')), 'def f():\n' + 'x = 1\n' * (long_chars // 6)),
                 ('apply_patch', f'*** Begin Patch\n*** Update File: pkg{cycle}/mod.py\n@@\n-x = 1\n+x = 2\n*** End Patch', 'Done'),
                 ('shell', json.dumps(dict(cmd=f'pytest tests/test_{cycle}.py')), 'FAILED tests/test_x.py::t - assert 1\n' + 'log line\n' * (long_chars // 9))]
        for name, args, out in steps:
            items.append(dict(type='function_call', name=name, call_id=f'c{n}', arguments=args))
            items.append(dict(type='function_call_output', call_id=f'c{n}', output=out))
            items.append(dict(type='message', role='assistant', content=[dict(type='output_text', text=f'step {n}')]))
            n += 1
        if user_at == cycle:
            items.append(dict(role='user', content=[dict(type='input_text', text='Now also handle the CLI flags.')]))
    return [items[:k] for k in range(2, len(items) + 1) if items[k - 1].get('type') == 'function_call_output']


def last_of(factory, reqs):
    rw = Rewriter(factory)
    for inp in reqs:
        out, _ = rw.rewrite_body(dict(model='gpt-5', instructions='sys', input=copy.deepcopy(inp), tools=[], stream=True), 's')
    return out['input'], rw


def test_entry_layer_shortens_long_outputs_once_with_their_number():
    out, _ = last_of(lambda: InfiniteContext(version=5, t=200000, entry=True, entry_frac=0.002), work_session())
    s = text(out)
    assert 'Full output stored at' in s and 'x = 1\nx = 1\n' * 300 not in s
    assert 'FAILED tests/test_x.py' in s                      # test output keeps its failure line
    plain, _ = last_of(lambda: InfiniteContext(version=5, t=200000), work_session())
    assert text(plain) == text(work_session()[-1])            # below t Cliff leaves everything alone


def test_segment_layer_batches_closed_cycles_into_numbered_placeholders():
    out, _ = last_of(lambda: InfiniteContext(version=5, t=200000, segment=True, segment_min_frac=0.001), work_session())
    s = text(out)
    assert 'Old tool output stored at' in s
    assert 'def f():' not in s                                # every cycle has closed: its reads are numbered
    assert 'FAILED tests/test_x.py' in s                      # the latest test result stays verbatim


def test_topic_layer_compacts_early_on_a_new_user_request():
    reqs = work_session(user_at=1)
    early, _ = last_of(lambda: InfiniteContext(version=5, t=40000, topic=True, topic_min_frac=0.05), reqs)
    late, _ = last_of(lambda: InfiniteContext(version=5, t=40000), reqs)
    assert 'summary of your previous actions' in text(early) and 'summary of your previous actions' not in text(late)


def test_adaptive_keeps_more_after_a_removed_file_is_read_again():
    m = InfiniteContext(version=5, t=200000, adaptive=True, entry=True, entry_frac=0.002)
    reqs = work_session()
    rw = Rewriter(lambda: m)
    for inp in reqs:
        rw.rewrite_body(dict(model='gpt-5', instructions='sys', input=copy.deepcopy(inp), tools=[], stream=True), 's')
    base = m.spec.keep_recent
    m._removed['cat pkg0/mod.py'] = {'pkg0/mod.py'}
    from ctxpress.live.context import LiveContext  # noqa: F401  (the session's context is the rewriter's)
    sim = next(iter(rw.sessions.values())).ctx
    sim.add(dict(seg='call', size=5, kind='read', res=['pkg0/mod.py'], text='cat pkg0/mod.py', call_id='again'))
    assert m._harm == 1 and 'pkg0/mod.py' in m._sticky and m._keep_recent() >= base + 1


def test_reading_an_edited_file_again_is_not_harm():
    m = InfiniteContext(version=5, t=200000, adaptive=True)
    rw = Rewriter(lambda: m)
    rw.rewrite_body(dict(model='gpt-5', instructions='sys', input=copy.deepcopy(work_session()[0]), tools=[], stream=True), 's')
    sim = next(iter(rw.sessions.values())).ctx
    m._removed['cat a.py'] = {'a.py'}
    sim.add(dict(seg='call', size=5, kind='edit', res=['a.py'], text='*** Update File: a.py', call_id='e'))
    sim.add(dict(seg='call', size=5, kind='read', res=['a.py'], text='cat  a.py', call_id='r'))
    assert m._harm == 0


def test_operator_list_is_open_ended():
    from ctxpress.methods.infinite_v5 import Op
    assert [o.label() for o in InfiniteContext(version=5).ops] == ['size:summary']          # Cliff
    layers = InfiniteContext(version=5, entry=True, segment=True, topic=True).ops
    assert [o.when for o in layers] == ['entry', 'segment_end', 'topic', 'size']
    # any list works: here only search results are cut at entry, and nothing compacts by size
    m = InfiniteContext(version=5, t=200000, ops=[dict(when='entry', transform='structure', kinds=['search'], frac=0.0001)])
    assert [o.label() for o in m.ops] == ['entry:structure[search]@0.0001'] and 'ops=' in m.name
    out, _ = last_of(lambda: InfiniteContext(version=5, t=200000, ops=[Op('entry', 'truncate', ('test',), 0.002)]), work_session())
    s = text(out)
    assert 'def f():\\n' + 'x = 1\\n' * 300 in s         # reads untouched (JSON-escaped newlines)
    assert 'Full output stored at' in s                     # test outputs cut
    with pytest.raises(ValueError):
        InfiniteContext(version=5, ops=[dict(when='entry', transform='summary')])
    with pytest.raises(ValueError):
        InfiniteContext(version=5, ops=[dict(when='entry', transform='truncate', kinds=['poems'])])


def test_standard_codex_is_the_host_operator():
    from ctxpress.methods import HostDefault
    codex = lambda: InfiniteContext(version=5, ops=[dict(when='size', transform='host')])
    m = codex()
    assert m.host_compaction == HostDefault.host_compaction == 'default' and m.codex_config == {}
    for inp in work_session():                                   # the proxy never rewrites: Codex compacts itself
        out, _ = Rewriter(codex).rewrite_body(dict(model='gpt-5', instructions='sys', input=copy.deepcopy(inp),
                                                   tools=[], stream=True), 's')
        assert text(out['input']) == text(inp)
    limited = InfiniteContext(version=5, host_t=180000, ops=[dict(when='entry', transform='shorten', frac=0.02),
                                                            dict(when='size', transform='host')])
    assert limited.codex_config == {'model_auto_compact_token_limit': 180000}
    assert getattr(limited, 'host_compaction', None) != 'default'


def test_codex_compaction_rule_keeps_real_user_messages_only():
    from ctxpress.methods.codex_compact import retained
    msg = lambda role, t: dict(type='message', role=role, content=[dict(type='input_text', text=t)])
    history = [msg('developer', 'instructions'), msg('user', '<environment_context>\n<cwd>/x</cwd>'),
               msg('user', 'Fix the parser.'), dict(type='function_call', name='shell', call_id='a', arguments='{}'),
               dict(type='function_call_output', call_id='a', output='x' * 900), msg('assistant', 'done'),
               msg('user', 'Also the CLI.'), msg('user', 'y' * 400_000)]
    kept = retained(history[:-1])
    assert [x['content'][0]['text'] for x in kept] == ['Fix the parser.', 'Also the CLI.']
    kept = retained(history)          # newest first within 64k tokens: the oversized newest one is cut, older go
    assert len(kept) == 1 and len(kept[0]['content'][0]['text']) == 64_000 * 4


def reuse_profile(rate, kind='read', n=1000):
    from ctxpress.methods.reuse_memory import AGES, profile
    from ctxpress.core.calibration import bucket
    pts = [(0, kind, bucket(a), 'explore', int(i < rate * n)) for a in AGES for i in range(n)]
    return profile([({'task': 'demo'}, pts)], {'task': 'demo'})


def test_reuse_memory_keeps_likely_needed_outputs_and_their_calls():
    cliff = run(lambda: InfiniteContext(version=5, t=3000))[-1]      # a long output is ~1300 tokens
    kept = run(lambda: InfiniteContext(version=5, t=3000, reuse_profile=reuse_profile(0.5), reuse_keep_frac=1.0, reuse_min_lift=0.5))[-1]
    old = [x for x in kept if x.get('type') == 'function_call_output' and 'line ' in str(x.get('output'))]
    assert len(old) > len([x for x in cliff if x.get('type') == 'function_call_output' and 'line ' in str(x.get('output'))])
    calls = {x['call_id'] for x in kept if x.get('type') == 'function_call'}
    assert all(x['call_id'] in calls for x in kept if x.get('type') == 'function_call_output')


def test_reuse_memory_with_unlikely_reuse_is_cliff():
    cliff = run(lambda: InfiniteContext(version=5, t=1000))
    v5 = run(lambda: InfiniteContext(version=5, t=1000, reuse_profile=reuse_profile(0.0), reuse_keep_frac=1.0))
    assert [text(x) for x in cliff] == [text(x) for x in v5]


def test_reuse_memory_weights_the_habit_that_predicts_the_session():
    from ctxpress.methods.reuse_memory import ReuseMemory
    m = ReuseMemory(reuse_profile(0.0))
    m.attrs = {}                       # one expert besides global and phase: all start equal
    m.experts = ['global', 'phase']
    m.logw = dict.fromkeys(m.experts, 0.0)
    m.request(0, 'explore')
    m.output('a', 'read', ['x.py'], [])
    for r in range(1, 60):
        m.request(r, 'explore')
        if r % 3 == 0:
            m.call('edit', ['x.py'])
    assert m.resolved > 0 and set(m.weights()) == {'global', 'phase'}
