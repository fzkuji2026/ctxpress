"""Compare pinned author functions to the registered adapter, with fake model IO."""
import argparse
import ast
import json
import re
import types
from pathlib import Path

from repro.sources import verify_source
from ctxpress.methods.resum_source import summary_prompt, parse_summary, should_summarize, continuation


def compare(root):
    root = Path(root)
    ref = verify_source('resum', root)
    scope = {'re': re, 'RESUM_TOOL_NAME': 'fake', 'RESUM_TOOL_URL': 'fake'}
    prompt_tree = ast.parse((root / 'prompt.py').read_text())
    for node in prompt_tree.body:
        if isinstance(node, ast.Assign) and node.targets[0].id.startswith('QUERY_SUMMARY_PROMPT'):
            scope[node.targets[0].id] = ast.literal_eval(node.value)
    functions = [n for n in ast.parse((root / 'summary_utils.py').read_text()).body if isinstance(n, ast.FunctionDef)]
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<pinned author functions>', 'exec'), scope)
    original_call = scope['call_resum_server']
    matches = 0
    for previous in [None, '', '<summary>prior evidence</summary>']:
        for question in ['task', '任务', '{{{last_summary}}}']:
            for messages in [[], [{'role': 'assistant', 'content': 'evidence'}], [{'role': 'user', 'content': "quote ' and newline\n"}]]:
                scope['call_resum_server'] = lambda query, **kw: query
                assert scope['summarize_conversation'](question, messages, previous) == summary_prompt(question, messages, previous)
                matches += 1
    for content in ['', 'plain', '<think>private\nreason</think> facts', '<summary>x</summary>', '<summary>x', '<summary></summary>', '<think>only</think>']:
        response = types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'choices': [{'message': {'content': content}}]})
        scope['requests'] = types.SimpleNamespace(post=lambda *a, **kw: response)
        assert original_call('fake') == parse_summary(content)
        matches += 1
    tree = ast.parse((root / 'react_agent.py').read_text())
    assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'should_summarize' for t in n.targets))
    expression = compile(ast.Expression(assignment.value), '<author trigger>', 'eval')
    for context in [1, 32, 128]:
        threshold = (context * 1024 - 1000) * .9
        for tokens in [0, threshold - 1, threshold, threshold + 1]:
            for turn in [1, 39, 40, 80]:
                for available in [0, 1]:
                    for enabled in [False, True]:
                        expected = eval(expression, {'RESUM': enabled, 'token_count': tokens, 'max_tokens': context * 1024 - 1000,
                                                     'round': turn, 'summary_iteration': 40, 'num_llm_calls_available': available})
                        assert bool(expected) == should_summarize(tokens, turn, context, 40, available, enabled)
                        matches += 1
    assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'new_observation' for t in n.targets))
    expected = eval(compile(ast.Expression(assignment.value), '<author continuation>', 'eval'), {'question': 'task', 'summary_response': '<summary>facts</summary>'})
    assert expected == continuation('task', '<summary>facts</summary>')
    return dict(matches=matches + 1, reference=ref, model='deterministic substitutes',
                scope='prompt, parsing, trigger boundaries, continuation only; not full trajectory or trained model equivalence')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = compare(args.source)
    Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
