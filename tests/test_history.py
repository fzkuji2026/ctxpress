"""Full-history hosts may repeat, revise or compact items between requests."""
import copy
from pathlib import Path
import pytest
from ctxpress import ContextManager
from ctxpress.methods import ARC, CodexAutoCompact, NoCompaction, ReSum, SlidingWindow


def message(text, role='user'):
    return dict(type='message', role=role, content=text)


def pair(call_id, text='output', command='cat a.py'):
    return [dict(type='function_call', name='shell', call_id=call_id, arguments=command),
            dict(type='function_call_output', call_id=call_id, output=text)]


def context(manager):
    return next(iter(manager.rewriter.sessions.values())).ctx


def test_equal_messages_have_distinct_turns_and_accounting():
    manager = ContextManager(NoCompaction())
    repeated = [message('repeat'), message('ok', 'assistant')] * 3
    assert manager.apply(repeated) == repeated
    ctx = context(manager)
    assert len(ctx.sent()) == len(repeated)
    assert len({s['id'] for s in ctx.sent()}) == len(repeated)
    assert ctx.uturn == 3 and ctx.turn == 3
    assert ctx.size() == sum(ctx.tokens(s['content']) for s in repeated)
    original_ids = [s['id'] for s in ctx.sent()]
    more = repeated + [message('repeat')]
    result, info = manager.apply(more, return_info=True)
    assert result == more and not info['history_rebased']
    assert [s['id'] for s in ctx.sent()][:len(repeated)] == original_ids
    assert ctx.uturn == 4


def test_append_keeps_method_summary_age_and_state():
    manager = ContextManager(ReSum(k=1), summarizer=lambda items: 'retained summary')
    history = [message('task')] + pair('a')
    manager.apply(history)
    history += [message('continue')]
    result, info = manager.apply(history, return_info=True)
    assert any('retained summary' in str(s) for s in result)
    assert not info['history_rebased'] and info['history_epoch'] == 0
    assert info['request'] == 2


def test_changed_output_versions_keep_old_archive_retrievable(tmp_path):
    manager = ContextManager(ARC(n=1), store_dir=tmp_path)
    history = [message('task')] + pair('a', 'old' * 100) + pair('b', 'other' * 100)
    manager.apply(history)
    ctx = context(manager)
    old_id = next(s['id'] for s in ctx.ctx if s.get('call_id') == 'a' and s['seg'] == 'out')
    updated = copy.deepcopy(history)
    updated[2]['output'] = 'new' * 100
    result, info = manager.apply(updated, return_info=True)
    new_id = next(s['id'] for s in ctx.ctx if s.get('call_id') == 'a' and s['seg'] == 'out')
    assert new_id != old_id and info['history_rebased'] and info['history_epoch'] == 1
    assert manager.retrieve(old_id) == 'old' * 100
    assert manager.retrieve(new_id) == 'new' * 100
    assert Path(ctx.store_dir, f'{old_id}.txt').read_text(encoding='utf-8') == 'old' * 100
    assert Path(ctx.store_dir, f'{new_id}.txt').read_text(encoding='utf-8') == 'new' * 100
    assert f'/{new_id}.txt' in result[2]['output']
    again, info = manager.apply(updated, return_info=True)
    assert again == result and not info['history_rebased'] and info['request'] == 3


def test_changed_call_arguments_refresh_classification_and_reuse_indexes():
    manager = ContextManager('NoCompaction')
    history = [message('task')] + pair('a', command='cat old.py')
    manager.apply(history)
    history[1]['arguments'] = 'cat new.py'
    manager.apply(history)
    ctx = context(manager)
    assert ctx.calls['a']['res'] == ['new.py']
    assert set(ctx.latest_read) == {'new.py'}
    assert next(s for s in ctx.ctx if s['seg'] == 'out')['res'] == ['new.py']


def test_host_compaction_drops_pinned_accounting_and_obsolete_summaries():
    manager = ContextManager(CodexAutoCompact(t=500), summarizer=lambda items: 'obsolete summary')
    old = [message('old instructions', 'system'), message('old task')] + pair('a', 'x' * 4000)
    assert any('obsolete summary' in str(s) for s in manager.apply(old))
    revised = [message('current instructions', 'developer'), message('host continuation')]
    result, info = manager.apply(revised, return_info=True)
    assert result == revised and info['history_rebased']
    ctx = context(manager)
    assert ctx.members == {} and ctx.calls == {} and ctx.latest_read == {}
    assert ctx.size() == sum(ctx.tokens(s['content']) for s in revised)
    assert all('old' not in s.get('text', '') for s in ctx.sent())
    assert ctx.M['history_rebases'] == 1


@pytest.mark.parametrize('change', ['insert', 'reorder', 'remove', 'empty'])
def test_revised_history_has_host_order_and_fresh_turns(change):
    original = [message('task'), message('a', 'assistant'), message('b'), message('c', 'assistant')]
    manager = ContextManager('NoCompaction')
    manager.apply(original)
    if change == 'insert':
        revised = original[:1] + [message('new instruction', 'developer')] + original[1:]
    elif change == 'reorder':
        revised = [original[0], original[3], original[2], original[1]]
    elif change == 'remove':
        revised = original[2:]
    else:
        revised = []
    result, info = manager.apply(revised, return_info=True)
    fresh = ContextManager('NoCompaction')
    assert result == revised == fresh.apply(revised)
    actual, expected = context(manager), context(fresh)
    assert [(s['text'], s['turn'], s['uturn']) for s in actual.sent()] == [
        (s['text'], s['turn'], s['uturn']) for s in expected.sent()]
    assert actual.size() == expected.size() and info['history_rebased']


def test_instructions_added_after_model_turn_survive_deletion():
    manager = ContextManager(SlidingWindow(100))
    history = [message('task')] + pair('a', 'x' * 2000)
    manager.apply(history)
    late = message('keep this instruction', 'developer')
    result = manager.apply(history + [late] + pair('b', 'y' * 2000))
    assert late in result


def test_responses_revision_of_last_appended_turn_rebuilds_state():
    manager = ContextManager('NoCompaction')
    first = [message('task')]
    manager.apply(first)
    manager.apply(first + [message('old answer', 'assistant')])
    revised = first + [message('corrected answer', 'assistant')]
    result, info = manager.apply(revised, return_info=True)
    assert result == revised
    assert info['history_rebased'] and info['history_epoch'] == 1
    assert info.get('forks_undone', 0) == 0
    assert context(manager).M['history_rebases'] == 1


def test_responses_method_can_own_noncopyable_runtime_state():
    import threading
    from ctxpress.live.rewrite import Rewriter

    class LockedMethod(NoCompaction):
        def __init__(self):
            self.runtime_lock = threading.Lock()

    rw = Rewriter(LockedMethod)
    first = [message('task')]
    for history in (first, first + [message('answer', 'assistant')]):
        body, info = rw.rewrite_body({'input': history}, 'session')
        assert body['input'] == history
        assert not info['history_rebased']
