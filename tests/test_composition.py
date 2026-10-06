"""Composition must consume the current representation, without recovering hidden originals."""
import copy
import pytest
from ctxpress import apply
from ctxpress.core.engine import Simulator
from ctxpress.live.context import LiveContext
from ctxpress.methods import NoCompaction, ScoredMethod, EntryTruncation, WithMemory, PinRequirements, Composed, ARC


def live(method=None):
    return LiveContext(method or NoCompaction())


def output(ctx, ident='a', text=None):
    ctx.add_call(ident, f'cat {ident}.py')
    ctx.add_output(ident, text if text is not None else 'available line\n' * 1000)
    return ctx.outputs()[-1]


@pytest.mark.parametrize('operation', ['truncate', 'structure'])
def test_failed_compression_retains_existing_view_archive_and_metrics(operation):
    ctx = live()
    item = output(ctx)
    assert ctx.truncate(item, 100)
    # An oversized line cannot be structurally shortened; a larger target also
    # must not replace a smaller view with the archived original.
    ctx.keep_text(item, 'already kept line')
    if operation == 'structure':
        item['form'] = 'truncated'
    before = copy.deepcopy((item, ctx.M))
    result = ctx.truncate(item, 100000) if operation == 'truncate' else ctx.structure(item)
    assert not result and (item, ctx.M) == before
    assert ctx.retrieve(item['id']) == 'available line\n' * 1000
    assert ctx.render(item) == 'already kept line'


def test_structure_after_truncation_does_not_reintroduce_removed_definitions():
    ctx = live()
    original = ('def visible_start():\n' + '    first body line\n' * 200 +
                'def discarded_middle():\n' + '    last body line\n' * 200 + 'def visible_end():\n')
    item = output(ctx, text=original)
    assert ctx.truncate(item, 200)
    assert 'discarded_middle' not in ctx.current_text(item)
    assert ctx.structure(item)
    assert 'discarded_middle' not in ctx.current_text(item)
    assert 'visible_start' in ctx.current_text(item) and 'visible_end' in ctx.current_text(item)
    assert ctx.retrieve(item['id']) == original


def test_smaller_second_truncation_uses_only_retained_text():
    ctx = live()
    item = output(ctx, text='archived line\n' * 1000)
    ctx.keep_text(item, 'visible retained line\n' * 100)
    before_size = ctx.seg_size(item)
    assert ctx.truncate(item, 100)
    assert ctx.seg_size(item) < before_size
    assert 'visible retained line' in ctx.current_text(item) and 'archived line' not in ctx.current_text(item)
    before = copy.deepcopy((item, ctx.M))
    assert not ctx.truncate(item, 10000) and (item, ctx.M) == before


@pytest.mark.parametrize('form', ['placeholder', 'memid', 'memlabel', 'gone', 'summary', 'segsummary'])
def test_mechanical_operations_cannot_recover_evicted_content(form):
    ctx = live()
    item = output(ctx)
    if form in ('placeholder', 'memid', 'memlabel'):
        ctx.to_placeholder([item], form=form)
    else:
        item['form'] = form
    before = copy.deepcopy((item, ctx.M))
    assert not ctx.truncate(item, 100) and not ctx.structure(item)
    assert (item, ctx.M) == before and ctx.current_text(item) is None


def test_no_text_replay_tracks_current_sizes_and_does_not_repeat_structure():
    sim = Simulator({'reqs': [], 'prefix': 0}, NoCompaction())
    item = {'id': 0, 'seg': 'out', 'kind': 'read', 'size': 4000, 'res': ['a.py']}
    assert sim.truncate(item, 1000)
    assert sim.structure(item)
    assert sim.seg_size(item) == int(1012 * .25) + 12
    before = copy.deepcopy((item, sim.M))
    assert not sim.structure(item) and (item, sim.M) == before
    assert not sim.truncate(item, 2000) and (item, sim.M) == before


def test_protection_and_nontext_media_prevent_mechanical_replacement():
    ctx = live()
    protected = output(ctx)
    ctx.protect([protected])
    ctx.add_call('image', 'capture screen')
    ctx.add_output('image', 'image metadata\n' * 1000, has_media=True)
    image = ctx.outputs()[-1]
    for item in (protected, image):
        before = copy.deepcopy((item, ctx.M))
        assert not ctx.truncate(item, 100) and not ctx.structure(item)
        assert (item, ctx.M) == before


def test_scored_budget_handles_ingress_truncated_outputs_and_keeps_latest_pair():
    ctx = live(EntryTruncation(ScoredMethod(budget=350, protect=1), budget=200))
    for i in range(3):
        output(ctx, str(i))
    assert all(s['form'] == 'truncated' for s in ctx.outputs())
    originals = [ctx.retrieve(s['id']) for s in ctx.outputs()]
    ctx.before_request()
    assert [s['form'] for s in ctx.outputs()] == ['placeholder', 'placeholder', 'truncated']
    assert sum(ctx.seg_size(s) for s in ctx.outputs()) <= 350
    assert [ctx.retrieve(s['id']) for s in ctx.outputs()] == originals
    assert len([s for s in ctx.ctx if s['seg'] == 'call']) == 3


@pytest.mark.parametrize('op', ['truncate', 'structure'])
def test_scored_operations_see_current_view_and_continue_after_uncompressible_item(op):
    ctx = live(ScoredMethod(budget=250, op=op, protect=0, truncate_to=100))
    tiny = output(ctx, 'tiny')
    ctx.keep_text(tiny, 'tiny kept view')
    large = output(ctx, 'large')
    ctx.keep_text(large, 'retained line\n' * 1000)
    large['form'] = 'truncated'
    tiny_before = copy.deepcopy(tiny)
    ctx.before_request()
    assert tiny == tiny_before
    assert ctx.seg_size(large) < 200 and 'available line' not in ctx.render(large)
    assert ctx.render(large) and large['form'] == ('truncated' if op == 'truncate' else 'structured')


def test_scored_placeholder_cannot_enlarge_a_small_current_view():
    ctx = live(ScoredMethod(budget=0, protect=0))
    item = output(ctx)
    ctx.keep_text(item, 'tiny')
    before = copy.deepcopy(item)
    assert ctx.placeholder_size() > ctx.seg_size(item)
    ctx.before_request()
    assert item == before


def test_scored_size_order_uses_current_sizes_instead_of_archive_sizes():
    ctx = live(ScoredMethod(budget=350, score='size', protect=0))
    big_archive = output(ctx, 'archive', 'archive line\n' * 10000)
    ctx.keep_text(big_archive, 'already small view\n' * 5)
    current_big = output(ctx, 'current', 'current line\n' * 100)
    ctx.before_request()
    assert big_archive['form'] == 'structured' and current_big['form'] == 'placeholder'


def test_scored_delete_can_remove_memory_pointers_and_their_calls():
    ctx = live(Composed([ARC(budget=0), ScoredMethod(budget=0, op='delete', protect=0)]))
    for i in range(3):
        output(ctx, str(i))
    original_ids = [s['id'] for s in ctx.outputs()]
    ctx.before_request()
    assert ctx.ctx == [] and all(ctx.retrieve(i) for i in original_ids)


def test_live_rewriter_composes_memory_budget_and_requirements_pinning():
    method = PinRequirements(WithMemory(EntryTruncation(ScoredMethod(budget=350, protect=1), budget=200)))
    inputs = [dict(type='message', role='user', content='task')]
    for i in range(4):
        inputs.extend([dict(type='function_call', call_id=str(i), name='shell',
                            arguments='cat SPEC.md' if i == 0 else f'cat {i}.py'),
                       dict(type='function_call_output', call_id=str(i), output='original line\n' * 1000)])
    original = copy.deepcopy(inputs)
    result = apply(method, inputs)
    assert inputs == original and inputs[1] in result and inputs[2] in result
    outputs = [s for s in result if s.get('type') == 'function_call_output']
    assert all('stored at' in s['output'] for s in outputs[1:-1])
    assert 'truncated' in outputs[-1]['output']
    assert [s['call_id'] for s in result if s.get('type') == 'function_call'] == ['0', '1', '2', '3']


@pytest.mark.parametrize('args', [{'op': 'typo'}, {'protect': -1}, {'protect': True}, {'truncate_to': 0}])
def test_invalid_scored_configuration_is_rejected_before_request(args):
    with pytest.raises(ValueError, match='ScoredMethod'):
        ScoredMethod(**args)
