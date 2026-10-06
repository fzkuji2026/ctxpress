"""Compare proactive and reactive Cliff steps with the local authors' engine.

Synthetic Responses histories only: no provider, Docker, task grade or quality
claims. Each reactive request and the following appended-history request must
match the author's outgoing JSON exactly.
"""
from __future__ import annotations
import argparse,copy,json,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def fixture(head=4):
    items=[dict(type='message',role='user',content='task '+('x'*head))]
    for i in range(8):
        items.extend([
            dict(type='reasoning',id=f'r{i}',summary=[dict(type='summary_text',text=f'thinking {i}\n'*120)]),
            dict(type='message',role='assistant',content=f'assistant explanation {i}\n'*200),
            dict(type='function_call',name='shell',call_id=str(i),arguments=f'cat file{i}.py'),
            dict(type='function_call_output',call_id=str(i),output='source line\n'*400)])
    return dict(model='synthetic',input=items,tools=[],stream=True)


def compare(original):
    sys.path.insert(0,str(original))
    from cliffcompaction.config import Config
    from cliffcompaction.engine import Engine
    from cliffcompaction.dialects.openai_responses import DIALECT
    from ctxpress.methods import CliffCompaction
    from ctxpress.live.rewrite import Rewriter
    rows=[]
    for threshold in (100000,12000,4000,1000):
        for head in (4,9000):
            engine=Engine(Config(threshold_tokens=threshold))
            rw=Rewriter(lambda:CliffCompaction(t=threshold))
            body=fixture(head)
            ctx=engine.prepare(copy.deepcopy(body),DIALECT)
            outgoing,info=rw.rewrite_body(copy.deepcopy(body),'fixture')
            assert outgoing==ctx.outgoing_body(), ('proactive',threshold,head)
            proactive_rung=ctx.rung
            assert rw.sessions['fixture'].ctx.method._rung==ctx.rung
            retries,rungs=0,[]
            for _ in range(5):
                changed=engine.reactive(ctx)
                ours=rw.retry_body(body,outgoing,info)
                assert changed==(ours is not None), ('reactive availability',threshold,head,retries,ctx.rung)
                assert rw.sessions['fixture'].ctx.method._rung==ctx.rung
                if not changed:
                    break
                outgoing,info=ours
                assert outgoing==ctx.outgoing_body(), ('reactive request',threshold,head,retries,ctx.rung)
                retries+=1;rungs.append(ctx.rung)
            else:
                raise AssertionError('reactive ladder did not terminate')
            assert retries<=4
            next_body=copy.deepcopy(body)
            next_body['input'].extend([dict(type='function_call',name='shell',call_id='new',arguments='cat next.py'),
                                      dict(type='function_call_output',call_id='new',output='new source\n'*200)])
            expected=engine.prepare(copy.deepcopy(next_body),DIALECT).outgoing_body()
            actual,_=rw.rewrite_body(next_body,'fixture')
            assert expected==actual, ('appended history',threshold,head)
            rows.append(dict(threshold=threshold,head_chars=head,retries=retries,proactive_rung=proactive_rung,rungs=rungs,matched=True))
    return dict(schema='ctxpress.repro.cliff-reactive',version=1,test_only=True,cases=rows,
                evidence='synthetic histories and local author engine; no real provider or task execution')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--orig',type=Path,default=ROOT.parent/'data/repro/cliffcompaction/src')
    ap.add_argument('--out',type=Path,default=ROOT/'runs/repro/cliff-reactive.json')
    args=ap.parse_args()
    result=compare(args.orig)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
