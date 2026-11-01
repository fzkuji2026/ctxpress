"""ReadLess: entry-time views of long reads, searches and test logs; repeats get the full text."""
import json
from ctxpress.live.rewrite import Rewriter, output_text
from ctxpress.methods import ReadLess

SOURCE = "\n".join([f"def f{i}(x):" if i % 20 == 0 else f"    y = x + {i}  # body line {i}" for i in range(400)])


def shell_pair(cid, cmd, out):
    return [dict(type='function_call', call_id=cid, name='shell', arguments=json.dumps(dict(cmd=cmd))),
            dict(type='function_call_output', call_id=cid, output=out)]


def code_mode_pair(cid, cmds_outs):
    script = ";".join(f'await tools.exec_command({{cmd: {json.dumps(c)}}})' for c, _ in cmds_outs)
    body = "Script completed\nWall time 0.1 seconds\nOutput:\n\n" + "\n".join(
        json.dumps(dict(chunk_id=str(i), exit_code=0, output=o)) for i, (_, o) in enumerate(cmds_outs))
    meta = dict(cell_id=cid, tool_calls_complete=True,
                executed_tool_calls=[dict(name='exec_command', arguments=dict(cmd=c)) for c, _ in cmds_outs])
    return [dict(type='custom_tool_call', call_id=cid, name='exec', input=script),
            dict(type='custom_tool_call_output', call_id=cid, output=body, internal_chat_message_metadata_passthrough=meta)]


def head():
    return [dict(type='message', role='user', content='Fix the bug')]


def sent(out, cid):
    return output_text(next(x for x in out['input'] if x.get('call_id') == cid and 'output' in x))


def test_long_cat_becomes_an_outline_with_line_numbers_and_repeat_is_full():
    rw = Rewriter(ReadLess)
    items = head() + shell_pair('a', 'cat src/m.py', SOURCE)
    out, _ = rw.rewrite_body(dict(input=items), 's')
    text = sent(out, 'a')
    assert 'def f20(x):' in text and '    21\t' in text.replace('    21\tdef', '    21\tdef')
    assert 'body line 100' not in text and "sed -n 'A,Bp' src/m.py" in text
    assert len(text) < len(SOURCE) / 3
    items += shell_pair('b', 'cat src/m.py', SOURCE)
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert sent(out, 'b') == SOURCE                      # the agent asked again: full text
    assert sent(out, 'a') == text                        # the earlier view stays as it was (cache)


def test_short_outputs_and_other_commands_are_untouched():
    rw = Rewriter(ReadLess)
    short = "\n".join(SOURCE.split("\n")[:60])
    items = head() + shell_pair('a', 'cat src/m.py', short) + shell_pair('b', 'ls -la', SOURCE)
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert sent(out, 'a') == short and sent(out, 'b') == SOURCE


def test_code_mode_chunks_are_viewed_with_their_own_commands():
    rw = Rewriter(ReadLess)
    items = head() + code_mode_pair('c', [('git status --short', ' M src/m.py'), ('nl -ba src/m.py', "\n".join(
        f"{i + 1:>6}\t{l}" for i, l in enumerate(SOURCE.split("\n"))))])
    out, _ = rw.rewrite_body(dict(input=items), 's')
    text = sent(out, 'c')
    body = text.split("Output:\n", 1)[1]
    chunks = [json.loads(l) for l in body.split("\n") if l.strip()]
    assert chunks[0]['output'] == ' M src/m.py'
    assert 'ctxpress read view' in chunks[1]['output'] and 'body line 100' not in chunks[1]['output']
    assert '   401\t' not in chunks[1]['output'] and '    21\tdef f20(x):' in chunks[1]['output']


def test_long_search_keeps_first_matches_per_file():
    listing = "\n".join(f"src/f{i % 4}.py:{i}: match {i}" for i in range(400))
    rw = Rewriter(ReadLess)
    out, _ = rw.rewrite_body(dict(input=head() + shell_pair('a', 'rg match src', listing)), 's')
    text = sent(out, 'a')
    assert text.count('src/f0.py:') == 4 and '(+97 more matches)' in text


def test_method_can_be_deep_copied():
    import copy
    from ctxpress.methods import build
    m = build({'class': 'Composed', 'args': {'methods': [{'class': 'ReadLess'}, {'class': 'HostDefault'}]}})
    copy.deepcopy(m)
