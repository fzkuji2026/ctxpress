"""Optional paper-adapter controls, using local history and a fake summary service."""
import copy
import json
from concurrent.futures import Future

import pytest

from ctxpress.live.context import LiveContext
from ctxpress.methods.comem import CoMem
from ctxpress.methods.context_folding import ContextFoldingTools


class Summary:
    def __init__(self):
        self.calls = []

    def summarize_prompt(self, system, user, **kwargs):
        self.calls.append((user, kwargs))
        return 'summary evidence'


@pytest.fixture
def context():
    contexts = []

    def make(method, **kwargs):
        sim = LiveContext(method, **kwargs)
        contexts.append(sim)
        return sim

    yield make
    for sim in contexts:
        if hasattr(sim.method, 'close'):
            sim.method.close()


def pair(sim, cid, name='read', args=None, text='raw evidence ' * 100):
    sim.add_call(cid, '{}', name=name, args=json.dumps(args or {}))
    return sim.add_output(cid, text)


@pytest.mark.parametrize('value', [0, -1, True, 1.5, '102400'])
def test_comem_rejects_invalid_trigger(value):
    with pytest.raises(ValueError, match='trigger_tokens'):
        CoMem(trigger_tokens=value)


def test_comem_below_trigger_still_archives_each_step(context):
    method = CoMem(trigger_tokens=102400)
    sim = context(method)
    task = sim.add_message('user', 'original task')
    output = pair(sim, 'first')
    method.step(sim, 0)
    assert task in method.archive and output in method.archive
    pair(sim, 'second', text='new evidence')
    method.step(sim, 1)
    assert set(method.archive) == {s['id'] for s in sim.sent()}
    sim.keep_text(next(s for s in sim.ctx if s['id'] == output), 'updated evidence')
    method.step(sim, 2)
    assert method.archive[output]['text'] == 'updated evidence'
    assert method.pending is None and method.executor is None


def test_comem_rendered_prompt_trigger_is_inclusive(context):
    summary = Summary()
    method = CoMem(trigger_tokens=102400, summary_model='aux')
    sim = context(method, summarizer=summary)
    sim.add_message('user', 'task')
    pair(sim, 'first')
    assert sim.size() < method.trigger_tokens
    measurements = []
    rendered_tokens = [102399]

    def measure(items=None):
        measurements.append(items)
        return rendered_tokens[0]

    sim.request_tokens = measure                  # the whole next request, as the live context estimates it
    method.step(sim, 1)
    assert method.pending is None
    rendered_tokens[0] = 102400
    method.step(sim, 2)
    assert method.pending is not None
    method.pending[2].result(timeout=3)
    assert measurements == [None, None]
    assert summary.calls[0][1] == dict(purpose='history', model='aux')


def test_comem_size_fallback_keeps_interval_lag_and_newest_turn(context):
    summary = Summary()
    method = CoMem(interval=2, lag=2)
    sim = context(method, summarizer=summary)
    sim.add_message('user', 'task')
    first = pair(sim, 'first')
    method.trigger_tokens = sim.size()
    method.step(sim, 0)
    method.step(sim, 1)
    assert method.pending is None
    method.step(sim, 2)
    pending = method.pending
    assert pending is not None
    pending[2].result(timeout=3)
    newest = pair(sim, 'newest', text='NEWEST EVIDENCE')
    method.step(sim, 3)
    assert method.pending is pending and newest in method.archive
    method.step(sim, 4)
    assert method.pending is None and sim.size() < method.trigger_tokens
    assert len(summary.calls) == 1
    assert 'NEWEST EVIDENCE' not in summary.calls[0][0]
    assert any(s['id'] == newest for s in sim.ctx)
    assert not any(s['id'] == first for s in sim.ctx)
    assert first in method.archive
    assert any(s.get('comem_summary') for s in sim.ctx)


@pytest.mark.parametrize('answer', ['settled evidence', None])
def test_comem_pending_settles_below_trigger_off_interval(context, answer):
    method = CoMem(interval=5, lag=2, trigger_tokens=102400)
    sim = context(method)
    pair(sim, 'first')
    ids = {s['id'] for s in sim.ctx}
    future = Future()
    future.set_result(answer)
    method.pending = (2, ids, future)
    newest = pair(sim, 'newest', text='latest evidence')
    method.step(sim, 3)
    assert method.pending is not None and newest in method.archive
    method.step(sim, 4)
    assert method.pending is None and method.executor is None
    assert any(s['id'] == newest for s in sim.ctx)
    assert any(s.get('comem_summary') for s in sim.ctx) == bool(answer)
    assert ids <= method.archive.keys()


def test_comem_default_preserves_author_scheduling_without_measurement(context):
    summary = Summary()
    method = CoMem(2, 2, 'aux')
    assert method.trigger_tokens is None
    sim = context(method, summarizer=summary)
    pair(sim, 'first')

    def unexpected_measurement(items=None):
        pytest.fail('the default scheduling must not depend on a token measurement')

    sim.request_tokens = unexpected_measurement
    method.step(sim, 1)
    assert method.pending is None
    method.step(sim, 2)
    assert method.pending is not None
    method.pending[2].result(timeout=3)
    assert len(summary.calls) == 1


def test_comem_deepcopy_preserves_trigger_and_attached_profile(context):
    method = CoMem(2, 3, 'aux', trigger_tokens=102400)
    sim = context(method)
    pair(sim, 'first')
    method.step(sim, 0)
    metadata = dict(context_profile={'version': 1, 'max_context_tokens': 128000,
                                     'compression_trigger_tokens': 102400},
                    requires_host_view=True, instructions='profile instructions',
                    codex_config={'model_context_window': 128000}, unbounded=True,
                    allow_native_compaction=False)
    for name, value in metadata.items():
        setattr(method, name, value)
    method.pending = (0, set(method.archive), Future())
    clone = copy.deepcopy(method)
    assert (clone.interval, clone.lag, clone.summary_model, clone.trigger_tokens) == (2, 3, 'aux', 102400)
    assert clone.archive == method.archive and clone.archive is not method.archive
    assert all(getattr(clone, name) == value for name, value in metadata.items())
    assert clone.pending is None and clone.executor is None
    clone.context_profile['max_context_tokens'] = 1
    clone.codex_config['model_context_window'] = 1
    next(iter(clone.archive.values()))['text'] = 'changed'
    assert method.context_profile['max_context_tokens'] == 128000
    assert method.codex_config['model_context_window'] == 128000
    assert all(s['text'] != 'changed' for s in method.archive.values())


def test_comem_deepcopy_leaves_unattached_metadata_as_class_defaults():
    clone = copy.deepcopy(CoMem())
    assert 'context_profile' not in clone.__dict__
    assert 'requires_host_view' not in clone.__dict__
    assert all(name not in clone.__dict__ for name in
               ('instructions', 'codex_config', 'unbounded', 'allow_native_compaction'))


@pytest.mark.parametrize('option', ['max_branches', 'branch_max_turns'])
@pytest.mark.parametrize('value', [0, -1, True, 1.5, '5'])
def test_context_folding_rejects_invalid_count_caps(option, value):
    with pytest.raises(ValueError, match=option):
        ContextFoldingTools(**{option: value})


@pytest.mark.parametrize('value', [0, -1, True, float('inf'), float('nan'), '5'])
def test_context_folding_rejects_invalid_timeout(value):
    with pytest.raises(ValueError, match='branch_timeout'):
        ContextFoldingTools(branch_timeout=value)


def test_context_folding_defaults_preserved():
    method = ContextFoldingTools()
    assert (method.max_branches, method.branch_max_turns, method.branch_timeout, method.branch_tokens) == (5, 64, 5400, None)


def branch(sim, cid):
    output = pair(sim, cid, 'context_branch', dict(description='work', prompt='find cause'), 'recorded')
    sim.method.step(sim, 0)
    return output


def test_context_folding_none_count_caps_allow_many_branches(context, monkeypatch):
    import ctxpress.methods.context_folding as module
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    method = ContextFoldingTools(max_branches=None, branch_max_turns=None, branch_timeout=None)
    sim = context(method)
    sim.add_message('user', 'parent task')
    parent = pair(sim, 'parent', text='parent evidence')
    for index in range(7):
        opening = branch(sim, f'branch-{index}')
        assert method.branch == opening
        work = pair(sim, f'work-{index}', text='intermediate evidence')
        next(s for s in sim.ctx if s['id'] == work)['turn'] = method.branch_turn + 1000
        clock[0] += 10000
        method.step(sim, index + 1)
        assert method.branch == opening
        pair(sim, f'return-{index}', 'context_return', dict(message=f'finding {index}'), 'recorded')
        method.step(sim, index + 2)
        assert method.branch is None
        assert not any(s['id'] == work for s in sim.ctx)
    assert method.started == 7
    assert any(s['id'] == parent for s in sim.ctx)


@pytest.mark.parametrize('remaining_cap', ['turns', 'timeout'])
def test_context_folding_disables_caps_independently(context, monkeypatch, remaining_cap):
    import ctxpress.methods.context_folding as module
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    method = ContextFoldingTools(max_branches=None,
                                 branch_max_turns=1 if remaining_cap == 'turns' else None,
                                 branch_timeout=5 if remaining_cap == 'timeout' else None)
    sim = context(method)
    opening = branch(sim, 'branch')
    work = pair(sim, 'work', text='intermediate evidence')
    clock[0] = 6
    method.step(sim, 1)
    assert method.branch is None
    assert not any(s['id'] == work for s in sim.ctx)
    boundary = next(s for s in sim.ctx if s['id'] == opening)
    assert 'The last message was:' in sim.current_text(boundary)


def test_context_folding_count_cap_remains_with_time_and_turn_caps_disabled(context):
    method = ContextFoldingTools(max_branches=1, branch_max_turns=None, branch_timeout=None)
    sim = context(method)
    branch(sim, 'first')
    pair(sim, 'return', 'context_return', dict(message='done'), 'recorded')
    method.step(sim, 1)
    opening = branch(sim, 'second')
    boundary = next(s for s in sim.ctx if s['id'] == opening)
    assert method.branch is None and method.started == 1
    assert 'limit of 1 branch calls' in sim.current_text(boundary)


def test_context_folding_token_return_fallback_remains_with_count_caps_disabled(context):
    method = ContextFoldingTools(max_branches=None, branch_max_turns=None,
                                 branch_timeout=None, branch_tokens=25600)
    sim = context(method)
    opening = branch(sim, 'branch')
    work = pair(sim, 'work', text='large evidence ' * 16000)     # ~32k tokens, above branch_tokens
    method.step(sim, 1)
    assert method.branch == opening and method.forced_turn is not None
    output = next(s for s in sim.ctx if s['id'] == work)
    assert 'context limit has been exceeded' in sim.current_text(output)
    method.step(sim, 1)
    assert method.branch == opening
    sim.add_message('assistant', 'partial progress')
    pair(sim, 'extra', text='extra evidence')
    method.step(sim, 2)
    assert method.branch is None and method.forced_turn is None
    assert not any(s['id'] == work for s in sim.ctx)
    boundary = next(s for s in sim.ctx if s['id'] == opening)
    assert 'partial progress' in sim.current_text(boundary)
