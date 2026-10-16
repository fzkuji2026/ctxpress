"""Experimental window controls, using deterministic in-memory host substitutes."""
from types import SimpleNamespace

import pytest

from ctxpress.methods import build
from ctxpress.methods.foldact_source import FoldActSource, select_turns
from ctxpress.methods.resum_source import ReSumSource, should_summarize


def summary_host(tokens):
    items = [dict(id='question', seg='msg', role='user', text='task'),
             dict(id='call', seg='call', call_id='read', text='read'),
             dict(id='result', seg='out', call_id='read', text='evidence')]
    written = []
    host = SimpleNamespace(ctx=items, size=lambda: tokens, sent=lambda: items,
                           current_text=lambda s: s['text'],
                           model_text=lambda *a, **kw: '<summary>finding</summary>',
                           summarize_segment=lambda selected, **kw: written.append((selected, kw)))
    return host, written


def folding_host(lengths, prefix=0):
    items = [dict(id='question', seg='msg', role='user', text='task', size=100)]
    for turn, length in enumerate(lengths):
        items.extend([dict(id=f'call{turn}', seg='call', call_id=str(turn), turn=turn,
                           text='read', size=10),
                      dict(id=f'out{turn}', seg='out', call_id=str(turn), turn=turn,
                           text='x' * length, size=length)])
    clipped, removed = [], []
    host = SimpleNamespace(ctx=items, prefix=prefix, tokens=len,
                           size=lambda: prefix + sum(s['size'] for s in items),
                           current_text=lambda s: s['text'], seg_size=lambda s: s['size'],
                           reinstate=lambda history: None,
                           keep_text=lambda s, text, **kw: clipped.append((s['id'], text)),
                           delete=lambda selected: removed.extend(s['id'] for s in selected))
    return host, clipped, removed


def test_author_constructor_defaults_and_positional_arguments_remain_unchanged():
    resum = ReSumSource()
    assert (resum.context_k, resum.interval, resum.summary_model, resum.trigger_tokens) == (32, 40, None, None)
    assert ReSumSource(128, 20, 'aux').summary_model == 'aux'
    foldact = FoldActSource()
    assert (foldact.max_model_len, foldact.max_obs_length, foldact.selection_tokens) == (16384, 4096, None)
    assert FoldActSource(128000, 8192).max_obs_length == 8192


@pytest.mark.parametrize('context_k', [1, 32, 128])
def test_resum_author_threshold_and_periodic_behavior(context_k):
    threshold = (context_k * 1024 - 1000) * 0.9
    assert not should_summarize(threshold - 1, 39, context_k, 40)
    assert should_summarize(threshold, 39, context_k, 40)
    assert should_summarize(0, 40, context_k, 40)
    assert should_summarize(0, 40, context_k, 40, True, False)
    assert not should_summarize(threshold, 39, context_k, 40, True, False)
    assert not should_summarize(threshold, 40, context_k, 40, False, True)


@pytest.mark.parametrize('context_k', [1, 32, 128])
def test_resum_explicit_threshold_is_exact_and_periodic_can_be_disabled(context_k):
    assert not should_summarize(102399, 40, context_k, None, trigger_tokens=102400)
    assert should_summarize(102400, 40, context_k, None, trigger_tokens=102400)
    assert not should_summarize(102400, 40, context_k, None, enabled=False, trigger_tokens=102400)
    assert not should_summarize(102400, 40, context_k, None, calls_available=False, trigger_tokens=102400)
    assert should_summarize(0, 40, context_k, 40, trigger_tokens=102400)


@pytest.mark.parametrize('tokens,expected', [(102399, False), (102400, True)])
def test_resum_step_uses_explicit_threshold_with_size_fallback(tokens, expected):
    method = build({'class': 'ReSumSource', 'args': {'trigger_tokens': 102400, 'interval': None}})
    host, written = summary_host(tokens)
    method.step(host, 40)
    assert bool(written) is expected


@pytest.mark.parametrize('legacy,rendered,expected', [(102399, 102400, True), (102400, 102399, False)])
def test_resum_explicit_trigger_measures_complete_rendered_input(legacy, rendered, expected):
    host, written = summary_host(legacy)
    measured = []

    def measure_history(history):
        measured.append(history)
        return rendered

    host.measure_history = measure_history
    ReSumSource(trigger_tokens=102400, interval=None).step(host, 40)
    assert measured == [host.ctx]
    assert bool(written) is expected


def test_resum_legacy_step_ignores_rendered_measurement():
    host, written = summary_host(0)

    def unexpected_measurement(history):
        raise AssertionError('Legacy ReSum must use the author estimate')

    host.measure_history = unexpected_measurement
    ReSumSource(interval=None).step(host, 40)
    assert not written
    ReSumSource().step(host, 40)
    assert written


def test_resum_never_summarizes_initial_request_even_at_explicit_threshold():
    host, written = summary_host(102400)
    ReSumSource(trigger_tokens=102400, interval=None).step(host, 0)
    assert not written


@pytest.mark.parametrize('option', ['interval', 'trigger_tokens'])
@pytest.mark.parametrize('value', [0, -1, True, 1.5, '102400', float('inf'), float('nan')])
def test_resum_rejects_invalid_optional_controls(option, value):
    with pytest.raises(ValueError, match=option):
        ReSumSource(**{option: value})


def test_foldact_default_selection_retains_author_reserve_and_summary_semantics():
    turns = [dict(length=5000, has_info=True, has_summary=False) for _ in range(3)]
    assert select_turns(turns, 384) == [0, 1, 2]
    assert select_turns(turns, 385) == [1, 2]
    turns[1]['has_summary'] = True
    assert select_turns(turns, 0) == [1, 2]


def test_foldact_explicit_selection_is_exact_and_independent_of_model_reserve():
    turns = [dict(length=51000, has_info=True, has_summary=False) for _ in range(2)]
    assert select_turns(turns, 400, 128000, selection_tokens=102400) == [0, 1]
    assert select_turns(turns, 401, 128000, selection_tokens=102400) == [1]
    assert select_turns(turns, 401, 128000) == [0, 1]
    assert select_turns(turns, 400, 16384, selection_tokens=102400) == [0, 1]
    turns[1]['has_summary'] = True
    assert select_turns(turns, 0, 128000, selection_tokens=102400) == [1]


@pytest.mark.parametrize('prefix,removed_ids', [(280, []), (281, ['call0', 'out0'])])
def test_foldact_step_applies_selection_limit_including_fixed_context(prefix, removed_ids):
    method = build({'class': 'FoldActSource', 'args': {
        'max_model_len': 128000, 'selection_tokens': 102400, 'max_obs_length': None}})
    host, clipped, removed = folding_host([51000, 51000], prefix=prefix)
    method.step(host, 1)
    assert removed == removed_ids
    assert not clipped and not method.force_summarized


@pytest.mark.parametrize('overhead,dropped_turns', [(-1, 0), (0, 0), (25000, 1), (25510, 1), (25511, 2)])
def test_foldact_explicit_selection_reserves_rendered_tool_overhead(overhead, dropped_turns):
    # Prefix + fixed user + four full turns already consume exactly 102400.
    host, clipped, removed = folding_host([25500] * 4, prefix=260)
    measured = []

    def measure_history(history):
        measured.append([s['id'] for s in history])
        return host.prefix + sum(s['size'] for s in history) + overhead

    host.measure_history = measure_history
    FoldActSource(max_model_len=128000, selection_tokens=102400, max_obs_length=None).step(host, 1)
    assert measured[0] == ['question']
    assert removed == [f'{kind}{i}' for i in range(dropped_turns) for kind in ('call', 'out')]
    assert not clipped
    kept_tokens = host.prefix + sum(s['size'] for s in host.ctx if s['id'] not in removed)
    assert kept_tokens + max(0, overhead) <= 102400


@pytest.mark.parametrize('turns', [40, 100, 400])
def test_foldact_rendered_overhead_does_not_grow_with_deleted_history(turns):
    # Rendering adds 20% per item. The reserve must not scale with the history it deletes:
    # the view keeps the newest turns that fit, however long the history grows.
    host, clipped, removed = folding_host([10000] * turns, prefix=20)
    host.measure_history = lambda history: host.prefix + 1.2 * sum(s['size'] for s in history)
    FoldActSource(max_model_len=128000, selection_tokens=102400, max_obs_length=None).step(host, 1)
    kept = [s for s in host.ctx if s['id'] not in removed]
    assert [s['id'] for s in kept[-2:]] == [f'call{turns - 1}', f'out{turns - 1}']
    assert len(kept) == 1 + 2 * 8
    assert host.measure_history(kept) <= 102400
    assert not clipped


def test_foldact_view_keeps_only_fixed_items_when_newest_turn_cannot_fit():
    host, clipped, removed = folding_host([5000, 200000])
    host.measure_history = lambda history: sum(s['size'] for s in history)
    FoldActSource(max_model_len=128000, selection_tokens=102400, max_obs_length=None).step(host, 1)
    assert removed == ['call0', 'out0', 'call1', 'out1']


def test_foldact_author_selection_ignores_rendered_tool_overhead():
    host, clipped, removed = folding_host([25500] * 4, prefix=260)

    def unexpected_measurement(history):
        raise AssertionError('Author FoldAct must use the original estimate')

    host.measure_history = unexpected_measurement
    FoldActSource(max_model_len=128000, max_obs_length=None).step(host, 1)
    assert not clipped and not removed


@pytest.mark.parametrize('measurement', [None, True])
def test_foldact_explicit_selection_ignores_noncallable_measurement(measurement):
    host, clipped, removed = folding_host([25500] * 4, prefix=260)
    host.measure_history = measurement
    FoldActSource(max_model_len=128000, selection_tokens=102400, max_obs_length=None).step(host, 1)
    assert not clipped and not removed


def test_foldact_overhead_reservation_still_preserves_protected_tool_pairs():
    host, clipped, removed = folding_host([25500] * 4, prefix=260)
    host.ctx[2]['has_media'] = True
    host.measure_history = lambda history: host.prefix + sum(s['size'] for s in history) + 25000
    FoldActSource(max_model_len=128000, selection_tokens=102400, max_obs_length=None).step(host, 1)
    # Safety keeps the oldest pair despite selection; the shared guard owns the maximum.
    assert not clipped and not removed
    assert host.measure_history(host.ctx) > 102400


def test_foldact_no_observation_limit_preserves_long_outputs_and_more_than_three_turns():
    host, clipped, removed = folding_host([5000] * 6)
    method = FoldActSource(max_model_len=128000, selection_tokens=102400, max_obs_length=None)
    method.step(host, 1)
    method.step(host, 2)
    assert not clipped and not removed
    assert not method.force_summarized and not method.clipped


def test_foldact_default_clipping_still_forces_three_turn_window():
    host, clipped, removed = folding_host([5000] * 6)
    method = FoldActSource(max_model_len=128000)
    method.step(host, 1)
    assert clipped == [(f'out{i}', 'x' * 4096) for i in range(6)]
    assert removed == [f'{kind}{i}' for i in range(3) for kind in ('call', 'out')]
    assert method.force_summarized


@pytest.mark.parametrize('option', ['selection_tokens', 'max_obs_length'])
@pytest.mark.parametrize('value', [0, -1, True, 1.5, '102400', float('inf'), float('nan')])
def test_foldact_rejects_invalid_optional_controls(option, value):
    with pytest.raises(ValueError, match=option):
        FoldActSource(**{option: value})
