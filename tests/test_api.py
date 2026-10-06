import copy
from pathlib import Path
import pytest
from ctxpress import ContextManager, apply
from ctxpress.methods import ComplexityTrap, ReSum, SWEPruner


def history(text='original'):
    return [dict(type='message', role='user', content='fix the bug'),
            dict(type='function_call', name='shell', call_id='c1', arguments='cat a.py'),
            dict(type='function_call_output', call_id='c1', output=text * 100),
            dict(type='function_call', name='shell', call_id='c2', arguments='cat b.py'),
            dict(type='function_call_output', call_id='c2', output='second file' * 100)]


def test_apply_reuses_common_engine_preserves_shape_and_source():
    body = dict(model='fixture', input=history(), tools=[dict(type='function', name='opaque')], stream=True)
    original = copy.deepcopy(body)
    assert apply('NoCompaction', body) == original
    masked, info = apply(ComplexityTrap(1), body, return_info=True)
    assert masked['input'][2]['output'] != body['input'][2]['output']
    assert masked['input'][-1] == body['input'][-1] and masked['tools'] == body['tools']
    assert body == original and info['changed'] == 1 and info['request'] == 1
    masked['tools'][0]['name'] = 'caller changed returned request'
    assert body == original
    assert isinstance(apply('NoCompaction', history()), list)


def test_manager_preserves_summary_counters_and_separates_sessions():
    summaries = []
    def summarize(items):
        summaries.append(items)
        return 'completed work summary'
    manager = ContextManager(ReSum(k=1), summarizer=summarize)
    first = history()
    assert manager.apply(first, session='a') == first
    second = manager.apply(first, session='a')
    assert any('completed work summary' in str(item) for item in second)
    assert len(summaries) == 1 and manager.info('a')['request'] == 2
    assert manager.apply(first, session='b') == first
    assert manager.info('b')['request'] == 1 and len(summaries) == 1
    assert manager.rewriter.sessions['a'].ctx.method is not manager.rewriter.sessions['b'].ctx.method
    apply(manager, first, session='b')
    assert manager.info('b')['request'] == 2 and len(summaries) == 2


def test_python_archives_are_isolated_by_session_and_manager(tmp_path):
    entry = {'class':'ARC', 'args':{'n':1}}
    first, second = ContextManager(entry, store_dir=tmp_path), ContextManager(entry, store_dir=tmp_path)
    entry['args']['n'] = 100  # A later config edit must not alter an existing manager.
    outputs = first.apply(history('session-a'), session='a')
    first.apply(history('session-b'), session='b')
    second.apply(history('manager-two'), session='a')
    a = first.rewriter.sessions['a'].ctx
    b = first.rewriter.sessions['b'].ctx
    c = second.rewriter.sessions['a'].ctx
    item_id = next(iter(a.store))
    assert first.retrieve(item_id, session='a') == 'session-a' * 100
    assert first.retrieve(item_id, session='b') == 'session-b' * 100
    assert second.retrieve(item_id, session='a') == 'manager-two' * 100
    assert first.retrieve(item_id, session='missing') is None
    assert len({a.store_dir,b.store_dir,c.store_dir}) == 3
    assert all(Path(ctx.store_dir, f'{item_id}.txt').is_file() for ctx in (a,b,c))
    assert 'stored at' in outputs[2]['output']


def test_api_checks_live_dependencies_and_does_not_misidentify_messages_api():
    with pytest.raises(ValueError, match='url'):
        ContextManager(SWEPruner())
    with pytest.raises(TypeError, match='Responses'):
        apply('NoCompaction', {'messages':history()})
    manager = ContextManager('NoCompaction')
    assert manager.apply({'input':'short prompt', 'model':'fixture'})['input'] == 'short prompt'
    with pytest.raises(ValueError, match='session'):
        manager.apply(history(), session='')
    with pytest.raises(TypeError, match='constructing'):
        apply(manager, history(), store_dir='unused')
    with pytest.raises(ValueError, match='full Responses history'):
        manager.apply({'input':history(), 'previous_response_id':'server-owned-history'})
    with pytest.raises(ValueError, match='store_dir'):
        ContextManager('ARC', store_prefix='/unavailable')
