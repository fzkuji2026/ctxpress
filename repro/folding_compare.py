"""Pinned author function checks; synthetic arrays/history, no model execution."""
import argparse
import ast
import copy
import json
import random
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List

from repro.sources import verify_source
from ctxpress.methods.agentfold_source import update_and_sort_steps, format_previous_steps, extract_tags
from ctxpress.methods.foldact_source import select_turns


def functions(path, names, scope):
    nodes = [n for n in ast.walk(ast.parse(Path(path).read_text()))
             if isinstance(n, ast.FunctionDef) and n.name in names]
    if len(nodes) != len(names):
        raise ValueError('author function inventory changed')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), scope)
    return scope


def agentfold(root, cases=500):
    reference = verify_source('agentfold', root)
    scope = functions(Path(root)/'infer.py', {'update_and_sort_steps', 'format_previous_steps', 'extract_tags'}, {'re': re})
    rng = random.Random(42)
    for text in ('<compress>{"compress_range":[0,1],"compress_text":"finding"}</compress>',
                 '<motivation> inspect\nfiles </motivation>', '<answer>incomplete', 'no tags'):
        for tag in ('compress', 'motivation', 'answer'):
            assert scope['extract_tags'](text, tag) == extract_tags(text, tag)
    for _ in range(cases):
        original = [dict(start=i, end=i, content=f'original {i}') for i in range(rng.randint(1, 30))]
        for round_ in range(4):
            a, b = sorted(rng.choices(range(30), k=2))
            info = dict(compress_range=[a, b], compress_text=f'fold {round_}')
            expected = scope['update_and_sort_steps'](copy.deepcopy(original), info, 29)
            actual = update_and_sort_steps(copy.deepcopy(original), info, 29)
            assert expected == actual
            assert scope['format_previous_steps'](copy.deepcopy(expected)) == format_previous_steps(copy.deepcopy(actual))
            original = actual
    return dict(reference=reference, matched=cases * 4, scope='step replacement and formatting; synthetic summaries; not host serialization or trained policy')


def foldact(root, cases=500):
    import numpy as np
    reference = verify_source('foldact', root)
    class Array(np.ndarray):
        def clone(self):
            return self.copy()
    def array(values):
        return np.asarray(values).view(Array)
    scope = dict(Dict=Dict, List=List, torch=SimpleNamespace(Tensor=np.ndarray, tensor=array),
                 ResponseType=SimpleNamespace(information_summary=SimpleNamespace(value=4),
                                              think_summary=SimpleNamespace(value=5)))
    functions(Path(root)/'search_r1/llm_agent/generation.py',
              {'_apply_sliding_window', '_check_turn_has_summary'}, scope)
    runner = SimpleNamespace(config=SimpleNamespace(enable_debug_logs=False),
                             tensor_fn=SimpleNamespace(concatenate_with_padding=lambda xs: np.concatenate(xs, axis=1)))
    runner._check_turn_has_summary = lambda turn: scope['_check_turn_has_summary'](runner, turn)
    rng = random.Random(73)
    for _ in range(cases):
        limit = rng.choice([1001, 1020, 1100, 16384])
        runner.config.max_model_len = limit
        initial_len = rng.randint(1, 30)
        initial = array([[0] * initial_len])
        raw, normalized = [], []
        for i in range(rng.randint(0, 25)):
            response_len, obs_len = rng.randint(1, 20), rng.randint(0, 40)
            summary, info = rng.choice([False, True]), rng.choice([False, True])
            # Observation-side summary token IDs must not trigger summary detection.
            types = [4 if summary else 0] * response_len + [5] * obs_len
            raw.append(dict(responses=array([[i+1] * response_len]), observations=array([[i+1] * obs_len]),
                            responses_types=array([types]), has_real_info=info))
            normalized.append(dict(length=response_len+obs_len, has_summary=summary, has_info=info))
        expected = scope['_apply_sliding_window'](runner, raw, initial).tolist()[0]
        indices = select_turns(normalized, initial_len, limit)
        actual = [0] * initial_len + [i+1 for i in indices for _ in range(normalized[i]['length'])]
        assert expected == actual
    return dict(reference=reference, matched=cases, scope='single-trajectory selection with NumPy tensor stand-in; not tokenizer, observation truncation, training or model outputs')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agentfold', required=True)
    parser.add_argument('--foldact', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    report = dict(agentfold=agentfold(args.agentfold), foldact=foldact(args.foldact))
    Path(args.output).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v['matched'] for k, v in report.items()}))
