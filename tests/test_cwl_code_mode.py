"""CWL must retain its graduated policy on complete, atomic Code Mode outputs."""
import json
import pytest
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import CWL


def wrapped(identity, tools, text, *, complete=True, origin=None, script='await tools.exec_command({cmd:"fixture"});'):
    return [dict(type='custom_tool_call', call_id=identity, name='exec', input=script),
        dict(type='custom_tool_call_output', call_id=identity, output=text,
            internal_chat_message_metadata_passthrough=dict(cell_id=origin or identity,
                executed_tool_calls=tools, tool_calls_complete=complete))]


def shell(cmd):
    return dict(name='exec_command', arguments=dict(cmd=cmd))


@pytest.mark.parametrize('tools,expected', [
    ([shell('rg pattern src')], 'search'),
    ([shell('pytest -q')], 'bash'),
    ([shell('cat src/main.py')], 'read'),
    ([shell('rg pattern src'), shell('cat src/main.py')], 'read'),
    ([shell('rg pattern src'), shell('pytest -q')], 'bash'),
    ([shell('cat src/main.py'), dict(name='apply_patch', arguments='patch')], None),
    ([dict(name='unknown', arguments={})], None),
])
def test_native_inventory_classifies_atomic_group_at_its_last_required_level(tools, expected):
    rw = Rewriter(lambda: CWL(budget=100000))
    body = dict(input=wrapped('c', tools, 'output'))
    result, _ = rw.rewrite_body(body, 's')
    assert CWL.category(rw.sessions['s'].ctx.calls['c']) == expected
    assert result['input'][-2:] == body['input']  # no invented calls or split outputs


@pytest.mark.parametrize('complete,origin,tools', [
    (False, 'c', [shell('cat src/main.py')]),
    (True, 'another-cell', [shell('cat src/main.py')]),
    (True, 'c', [dict(name='exec_command', arguments={'_codex_executed_tool_call_truncated': {}})]),
    (True, 'c', []),
])
def test_incomplete_cross_cell_or_truncated_records_do_not_guess_a_category(complete, origin, tools):
    rw = Rewriter(CWL)
    rw.rewrite_body(dict(input=wrapped('c', tools, 'output', complete=complete, origin=origin)), 's')
    assert CWL.category(rw.sessions['s'].ctx.calls['c']) is None


def test_freeform_input_is_included_in_method_argument_accounting():
    script = 'text("' + 'x' * 8000 + '");'
    rw = Rewriter(CWL)
    rw.rewrite_body(dict(input=wrapped('c', [], 'small', script=script)), 's')
    ctx = rw.sessions['s'].ctx
    assert ctx.calls['c']['args'] == script
    assert CWL.estimate(ctx) > 2000


def test_search_eviction_preserves_read_and_edit_outputs_in_the_same_completed_episode():
    rw = Rewriter(lambda: CWL(budget=2000))
    tool = CWL()
    history = []
    def delimiter(identity, args):
        answer, failed = tool.call_tool('delimiter', args)
        assert not failed
        return [dict(type='function_call', namespace='mcp__ctxpress', name='delimiter',
                     call_id=identity, arguments=json.dumps(args)),
                dict(type='function_call_output', call_id=identity, output=answer)]
    steps = [delimiter('start', dict(action='start', name='work', type='expl')),
        wrapped('search', [shell('rg pattern src')], 'search ' * 700),
        wrapped('read', [shell('cat src/main.py')], 'read ' * 900),
        wrapped('edit', [dict(name='apply_patch', arguments='patch')], 'patched'),
        delimiter('end', dict(action='end', description='fixture context'))]
    for step in steps:
        history.extend(step)
        result, info = rw.rewrite_body(dict(input=list(history)), 's')
    outputs = {item['call_id']: item['output'] for item in result['input'] if item.get('type', '').endswith('_call_output')}
    assert 'search' not in outputs
    assert outputs['read'] == 'read ' * 900 and outputs['edit'] == 'patched'
    assert 'start' in outputs and 'end' in outputs  # did not fall through to whole-episode deletion
    assert info['dropped'] == 2
