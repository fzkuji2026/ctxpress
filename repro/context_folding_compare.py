"""Compare completed branch projections with the author's retained main history.

Only successful explicit returns are compared. No branch generation, tokenizer,
automatic termination, fallback return, RL or trained model is exercised.
"""
import argparse
import ast
import json
import random
import re
from pathlib import Path

from repro.sources import verify_source
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods.context_folding import ContextFoldingTools, clean_response


def compare(root, cases=100):
    ref = verify_source('context_folding', root)
    path = Path(root)/'verl/experimental/agent_loop/FoldAgent/fold_agent_loop.py'
    tree = ast.parse(path.read_text())
    clean = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'clean_response')
    scope = {'re': re}
    exec(compile(ast.Module(body=[clean], type_ignores=[]), str(path), 'exec'), scope)
    for response in ('', 'partial progress', '<function=return>message', '<[assistant]>last message'):
        assert scope['clean_response'](response) == clean_response(response)
    assignments = [n for n in ast.walk(tree) if isinstance(n, ast.Assign) and
                   any(isinstance(t, ast.Name) and t.id == 'branch_message' for t in n.targets)]
    reply = next(n.value for n in assignments if isinstance(n.value, ast.JoinedStr) and
                 any(isinstance(v, ast.Constant) and 'the returned message is:' in str(v.value) for v in n.value.values))
    reply_code = compile(ast.Expression(reply), str(path), 'eval')
    rng = random.Random(12030)
    matched = 0
    for case in range(cases):
        rw = Rewriter(ContextFoldingTools)
        inputs = [dict(role='user', content='fixed task')]
        expected = []
        def pair(cid, name, args, output):
            inputs.extend([dict(type='function_call', call_id=cid, name=name, arguments=json.dumps(args)),
                           dict(type='function_call_output', call_id=cid, output=output)])
        for branch in range(rng.randint(1, 5)):
            cid = f'b{case}-{branch}'
            pair(cid, 'context_branch', dict(description='task', prompt='find evidence'), 'recorded')
            rw.rewrite_body(dict(input=inputs), 's')
            for step in range(rng.randint(0, 7)):
                pair(f'{cid}-w{step}', 'read', {}, f'intermediate {rng.random()}')
                rw.rewrite_body(dict(input=inputs), 's')
            message = f'finding {rng.random()}\nquoted <text>'
            pair(cid+'-return', 'context_return', dict(message=message), 'recorded')
            out, _ = rw.rewrite_body(dict(input=inputs), 's')
            # The source keeps the parent branch call and its final observation,
            # while branch-local tool calls live only in session_message.
            expected += [('function_call', cid, 'context_branch'),
                         ('function_call_output', cid, eval(reply_code, {'branch_message': message}))]
            observed = [(x['type'], x['call_id'], x.get('name') if x['type']=='function_call' else x['output'])
                        for x in out['input'] if x.get('type') in ('function_call', 'function_call_output')]
            assert expected == observed
            matched += 1
    return dict(reference=ref, cases=cases, matched=matched,
                scope='completed branch tool-history projection with author return template; synthetic host tools, not full author loop or trained policy')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    report = compare(args.source)
    Path(args.output).write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({k: report[k] for k in ('cases', 'matched')}))
