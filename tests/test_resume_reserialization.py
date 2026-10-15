import copy

from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import build


def history(n):
    items = [dict(type='additional_tools', id='at_1', role='developer', tools=[dict(name='exec', description='v1')]),
             dict(type='message', id='m0', role='user', content=[dict(type='input_text', text='task')],
                  internal_chat_message_metadata_passthrough=dict(turn_id='t1', create_time=1.0))]
    for i in range(n):
        items += [dict(type='reasoning', id=f'rs_{i}', summary=[], encrypted_content=f'enc-{i}',
                       internal_chat_message_metadata_passthrough=dict(turn_id='t1')),
                  dict(type='custom_tool_call', id=f'ctc_{i}', status='completed', call_id=f'c{i}', name='exec', input=f'cmd {i}'),
                  dict(type='custom_tool_call_output', id=f'ctco_{i}', call_id=f'c{i}', output=f'output {i} ' + 'x' * 2000)]
    return items


def resumed(items):
    """What a resumed host sends for the same history: new tool declaration, metadata and status re-serialized."""
    out = copy.deepcopy(items)
    out[0].update(id='at_2', tools=[dict(name='exec', description='v2')])
    for item in out:
        if 'internal_chat_message_metadata_passthrough' in item:
            item['internal_chat_message_metadata_passthrough'] = dict(turn_id='t2', create_time=2.0)
        item.pop('status', None)
    return out


def test_resumed_host_reserialization_keeps_the_method_state():
    rw = Rewriter(lambda: build({'class': 'ComplexityTrap', 'args': {'n': 1}}))
    first = history(3)
    rw.rewrite_body(dict(input=first), 's')
    ids = dict(zip(range(len(first)), rw.sessions['s'].ids.values()))
    second = resumed(first) + [dict(type='message', role='user', content=[dict(type='input_text', text='next')])]
    out, info = rw.rewrite_body(dict(input=second), 's')
    assert info['history_rebased'] is False and info['history_epoch'] == 0
    assert info['history_realigned']['first'] == 0
    assert set(info['history_realigned']['fields']) == {'id', 'tools', 'internal_chat_message_metadata_passthrough', 'status'}
    assert list(rw.sessions['s'].ids.values())[:len(first)] == list(ids.values())
    assert out['input'][0]['tools'][0]['description'] == 'v2'                 # what is sent is the host's new serialization


def test_changed_content_or_identity_still_rebases_and_says_where():
    rw = Rewriter(lambda: build({'class': 'ComplexityTrap', 'args': {'n': 1}}))
    first = history(3)
    rw.rewrite_body(dict(input=first), 's')
    edited = copy.deepcopy(first)
    edited[3]['input'] = 'a different command'
    _, info = rw.rewrite_body(dict(input=edited), 's')
    assert info['history_rebased'] is True and info['history_realigned'] is None
    assert info['history_difference'] == dict(index=3, previous_type='custom_tool_call', type='custom_tool_call',
                                              same_identity=False, fields=['input'])
    renamed = copy.deepcopy(edited)
    renamed[2]['id'] = 'rs_other'
    _, info = rw.rewrite_body(dict(input=renamed), 's')
    assert info['history_rebased'] is True and info['history_difference']['fields'] == ['id']
