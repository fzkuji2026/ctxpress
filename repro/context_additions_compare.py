"""Pinned-source checks; no network/model calls or benchmark score claims.

Usage: python repro/context_additions_compare.py --sources PATH
The randomized TACO comparison shares the stdlib record adaptation on both
sides: it verifies filtering, not Pydantic validation equivalence.
"""
import argparse
import ast
import copy
import hashlib
import json
import logging
from pathlib import Path
import random
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ctxpress.methods._paper_sources.taco.dynamic_filter import DynamicCompressionFilter
from ctxpress.methods._paper_sources.taco.models import CompressionRule
from ctxpress.methods._paper_sources.taco.filter_types import BaseFilter, FilterContext
from ctxpress.methods._paper_sources.comem_prompt import compressor_prompt_v5


def definitions(path):
    return {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(path.read_text(encoding='utf-8')).body
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}


def compare(root):
    root=Path(root); project=Path(__file__).resolve().parents[1]
    pins=json.loads((project/'repro/reference_sources.json').read_text())
    verified=0
    for name in ('taco','comem','longseeker','ziprl'):
        for path,digest in pins[name]['sha256'].items():
            assert hashlib.sha256((root/name/path).read_bytes()).hexdigest()==digest, (name,path)
            verified+=1
    local=project/'ctxpress/methods/_paper_sources'
    count=0
    taco=root/'taco/src/harbor/agents/terminus_2/compression'
    for file in ['dynamic_filter.py','planner.py','evolver.py','feedback.py','seed_rules.py']:
        original, adapted=definitions(taco/file),definitions(local/'taco'/file)
        assert original==adapted,file
        count+=len(original)
    for file in ['context.py','compress.py','delete.py','rollback.py','snippet.py','skip.py']:
        original=root/'longseeker'/ ('context.py' if file=='context.py' else 'tools/meta_tool/'+file)
        assert definitions(original)==definitions(local/'longseeker'/file),file
        count+=len(definitions(original))
    for original,adapted in [('verl/context_zip_util/compression_parser.py','ziprl_parser.py'),('verl/prompts/context_zip_prompts.py','ziprl_prompts.py')]:
        assert ast.dump(ast.parse((root/'ziprl'/original).read_text()))==ast.dump(ast.parse((local/adapted).read_text()))
    tree=ast.parse((root/'comem/evaluation/src/r2egym/agenthub/agent/comem_agent_kso_openai.py').read_text())
    prompt=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='compressor_prompt_v5' for t in n.targets))
    assert prompt==compressor_prompt_v5
    # Execute author's filter class without its heavyweight Harbor import graph.
    node=next(n for n in ast.parse((taco/'dynamic_filter.py').read_text()).body if isinstance(n,ast.ClassDef))
    ns=dict(re=re,logger=logging.getLogger('source-check'),BaseFilter=BaseFilter,FilterContext=FilterContext,CompressionRule=CompressionRule)
    exec(compile(ast.Module(body=[node],type_ignores=[]),'author-filter','exec'),ns)
    rng=random.Random(20261012)
    for _ in range(300):
        rule=CompressionRule('test',r'git|pytest',keep_patterns=['vital'],strip_patterns=['noise'],keep_first_n=rng.randrange(5),keep_last_n=rng.randrange(5),max_lines=rng.choice([None,0,3,15]),confidence=rng.choice([0.2,0.3,0.9]))
        text='\n'.join(rng.choice(['noise','vital noise','evidence']) for _ in range(rng.randrange(3,100)))
        ctx=FilterContext(rng.choice(['git fetch','pytest','cat x']),rng.choice([False,True]),'idle',None,len(text))
        a,b=ns['DynamicCompressionFilter'](copy.deepcopy(rule)),DynamicCompressionFilter(copy.deepcopy(rule))
        assert a.should_skip(ctx)==b.should_skip(ctx)
        if not a.should_skip(ctx):assert a.filter(text,ctx)==b.filter(text,ctx)
    return dict(source_files_verified=verified,source_definitions_equal=count,taco_randomized_cases=300,ziprl_parser_and_prompts_equal=True,comem_prompt_equal=True,
                limitations=['Source functions and fake inputs only; not full author runners, trained policies or published scores.', 'TACO comparison uses the same adapted record type on both sides.'])


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--sources',required=True)
    print(json.dumps(compare(parser.parse_args().sources),ensure_ascii=False,indent=2))
