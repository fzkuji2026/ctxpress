import json

import pytest

from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import build
from ctxpress.methods.context_folding import ContextFoldingTools


def append(items, cid, name, args=None, output='recorded'):
    items.extend([dict(type='function_call', call_id=cid, name=name, arguments=json.dumps(args or {})),
                  dict(type='function_call_output', call_id=cid, output=output)])


def setup():
    rw = Rewriter(lambda: build({'class': 'ContextFoldingTools', 'args': {'max_branches': 1}}))
    items = [dict(role='user', content='original task')]
    append(items, 'parent', 'read', output='parent evidence')
    append(items, 'branch', 'context_branch', dict(description='investigate', prompt='find cause'))
    rw.rewrite_body(dict(input=items), 's')
    append(items, 'work', 'read', output='large intermediate result')
    return rw, items


def test_shared_host_folds_only_branch_and_replay_is_idempotent():
    rw, items = setup()
    append(items, 'return', 'context_return', dict(message='concise finding'))
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert 'parent evidence' in str(out) and 'original task' in str(out)
    assert 'large intermediate result' not in str(out)
    assert 'Branch has finished its task, the returned message is:' in str(out)
    assert 'concise finding' in str(out)
    ctx = rw.sessions['s'].ctx
    deleted = ctx.M['op_delete']
    again, _ = rw.rewrite_body(dict(input=items), 's')
    assert again == out and ctx.M['op_delete'] == deleted
    append(items, 'extra', 'context_branch', dict(description='extra', prompt='extra'))
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert 'limit of 1 branch calls' in str(out)


def test_rejects_nested_branch_without_losing_active_branch():
    rw, items = setup()
    append(items, 'nested', 'context_branch', dict(description='nested', prompt='nested'))
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert 'nested branches are not allowed' in str(out)
    append(items, 'return', 'context_return', dict(message='done'))
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert 'large intermediate result' not in str(out) and 'done' in str(out)


def test_user_correction_prevents_destructive_fold():
    rw, items = setup()
    items.append(dict(role='user', content='new requirement'))
    append(items, 'return', 'context_return', dict(message='done'))
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert 'protected history' in str(out)
    assert 'new requirement' in str(out) and 'large intermediate result' in str(out)


def test_incomplete_call_is_not_deleted_by_fold():
    rw, items = setup()
    items.append(dict(type='function_call', call_id='pending', name='read', arguments='{}'))
    append(items, 'return', 'context_return', dict(message='done'))
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert 'unfinished or crossing tool call' in str(out)
    assert any(s.get('call_id') == 'pending' for s in rw.sessions['s'].ctx.ctx)


def test_branch_turn_limit_returns_last_assistant_message_without_dropping_parent():
    rw = Rewriter(lambda: ContextFoldingTools(branch_max_turns=1))
    items = [dict(role='user', content='original task')]
    append(items, 'parent', 'read', output='parent evidence')
    append(items, 'branch', 'context_branch', dict(description='work', prompt='find cause'))
    rw.rewrite_body(dict(input=items), 's')
    items.append(dict(role='assistant', content='partial finding'))
    append(items, 'work', 'read', output='intermediate evidence')
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert 'The last message was:' in str(out) and 'partial finding' in str(out)
    assert 'parent evidence' in str(out) and 'intermediate evidence' not in str(out)
    assert rw.sessions['s'].ctx.method.branch is None


def test_branch_token_limit_requests_return_then_falls_back_on_next_turn():
    rw = Rewriter(lambda: ContextFoldingTools(branch_tokens=10))
    items = [dict(role='user', content='task')]
    append(items, 'branch', 'context_branch', dict(description='work', prompt='find cause'))
    rw.rewrite_body(dict(input=items), 's')
    append(items, 'work', 'read', output='large evidence ' * 50)
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert 'context limit has been exceeded' in str(out)
    again, _ = rw.rewrite_body(dict(input=items), 's')
    assert out == again
    items.append(dict(role='assistant', content='partial progress'))
    append(items, 'extra', 'read', output='extra evidence')
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert 'The last message was:' in str(out) and 'partial progress' in str(out)
    assert 'extra evidence' not in str(out)


def test_branch_timeout_keeps_unfinished_tools(monkeypatch):
    import ctxpress.methods.context_folding as module
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    rw = Rewriter(lambda: ContextFoldingTools(branch_timeout=5))
    items = [dict(role='user', content='task')]
    append(items, 'branch', 'context_branch', dict(description='work', prompt='find cause'))
    rw.rewrite_body(dict(input=items), 's')
    items.append(dict(type='function_call', call_id='pending', name='read', arguments='{}'))
    clock[0] = 6
    out, _ = rw.rewrite_body(dict(input=items), 's')
    # The shared renderer withholds unfinished calls from a new model request;
    # the method must retain it internally until the matching result arrives.
    assert any(x.get('call_id') == 'pending' for x in rw.sessions['s'].ctx.ctx)
    assert rw.sessions['s'].ctx.method.branch is not None


@pytest.mark.parametrize('value', [0, -1, True, 1.5])
def test_invalid_branch_limit(value):
    with pytest.raises(ValueError):
        ContextFoldingTools(max_branches=value)


def test_invalid_arguments_and_unmatched_return():
    method = ContextFoldingTools()
    assert method.call_tool('context_return', {'message': ''})[1]
    assert method.call_tool('context_branch', {'description': 'x'})[1]
    rw = Rewriter(ContextFoldingTools)
    items = [dict(role='user', content='task')]
    append(items, 'r', 'context_return', dict(message='unexpected'))
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert 'no active branch' in str(out)


def test_anthropic_host_folds_branch_and_keeps_valid_tool_pairs():
    from ctxpress.live import anthropic
    from test_anthropic_host import valid
    messages = [dict(role='user', content=[dict(type='text', text='task')])]
    for cid, name, args, text in [
        ('p', 'read', {}, 'parent evidence'),
        ('b', 'context_branch', dict(description='work', prompt='find cause'), 'recorded'),
        ('w', 'read', {}, 'intermediate evidence'),
        ('r', 'context_return', dict(message='final finding'), 'recorded'),
    ]:
        messages.extend([
            dict(role='assistant', content=[dict(type='tool_use', id=cid, name=name, input=args)]),
            dict(role='user', content=[dict(type='tool_result', tool_use_id=cid, content=text)]),
        ])
    body = dict(model='claude-test', max_tokens=1000, messages=messages,
                tools=[dict(name='read', input_schema=dict(type='object'))])
    out, _ = anthropic.rewrite_messages(Rewriter(ContextFoldingTools), body)
    assert valid(out) and out['tools'] == body['tools']
    assert 'parent evidence' in str(out) and 'final finding' in str(out)
    assert 'intermediate evidence' not in str(out)


@pytest.mark.parametrize('name,args', [('ContextFoldingTools', {}), ('ReSumSource', {'interval': 2})])
def test_common_method_check_exercises_new_methods(name, args):
    from ctxpress.harness.checks.method import check
    report = check(name, args=args, turns=6)
    assert report['contract_valid'] and report['triggered']
