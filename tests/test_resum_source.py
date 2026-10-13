from types import SimpleNamespace
import json

import pytest

from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import build
from ctxpress.methods.resum_source import ReSumSource


class Summary:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def summarize_prompt(self, system, user, **kwargs):
        self.calls.append((user, kwargs))
        if self.fail:
            raise RuntimeError('service unavailable')
        return '<think>private</think><summary>kept evidence</summary>'


def pair(cid):
    return [dict(type='function_call', call_id=cid, name='read', arguments='{}'),
            dict(type='function_call_output', call_id=cid, output='original evidence ' * 100)]


def test_shared_rewriter_summary_continuation_and_auxiliary_service():
    model = Summary()
    rw = Rewriter(lambda: build({'class': 'ReSumSource', 'args': {'interval': 1, 'summary_model': 'aux'}}), summarizer=model)
    items = [dict(role='user', content='TASK')] + pair('a')
    rw.rewrite_body(dict(input=items), 's')
    items += pair('b')
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert 'kept evidence' in str(out) and 'private' not in str(out)
    assert 'original evidence' not in str(out) and 'TASK' in str(out)
    assert model.calls[-1][1] == dict(purpose='history', model='aux')
    items += pair('c')
    rw.rewrite_body(dict(input=items), 's')
    assert 'Last Summary' in model.calls[-1][0]
    assert '<summary>kept evidence</summary>' in model.calls[-1][0]
    assert sum(bool(s.get('resum_summary')) for s in rw.sessions['s'].ctx.ctx) == 1


def test_author_empty_system_prompt_reaches_real_responses_transport():
    from ctxpress.live.summarize import ResponsesSummarizer
    sent, records = [], []
    class Connection:
        def request(self, *args, **kwargs):
            sent.append(json.loads(kwargs['body']))
        def getresponse(self):
            raw = json.dumps(dict(status='completed', model='aux', output=[dict(type='message',
                content=[dict(type='output_text', text='<summary>kept evidence</summary>')])])).encode()
            return SimpleNamespace(status=200, read=lambda: raw)
        def close(self):
            pass
    service = ResponsesSummarizer(Connection, '/responses', {}, {'model': 'aux'}, records.append)
    rw = Rewriter(lambda: ReSumSource(interval=1), summarizer=service)
    items = [dict(role='user', content='TASK')] + pair('a')
    rw.rewrite_body(dict(input=items), 's')
    items += pair('b')
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert 'kept evidence' in str(out) and 'original evidence' not in str(out)
    assert len(sent) == 1 and 'instructions' not in sent[0]
    assert records[0]['completed'] is True


def test_failed_summary_does_not_lose_history():
    model = Summary(fail=True)
    rw = Rewriter(lambda: ReSumSource(interval=1), summarizer=model)
    items = [dict(role='user', content='TASK')] + pair('a')
    rw.rewrite_body(dict(input=items), 's')
    items += pair('b')
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert 'original evidence' in str(out)
    assert not any(s.get('resum_summary') for s in rw.sessions['s'].ctx.ctx)


def test_success_preserves_user_correction():
    rw = Rewriter(lambda: ReSumSource(interval=1), summarizer=Summary())
    items = [dict(role='user', content='TASK')] + pair('a')
    rw.rewrite_body(dict(input=items), 's')
    items += [dict(role='user', content='NEW REQUIREMENT')] + pair('b')
    out, _ = rw.rewrite_body(dict(input=items), 's')
    assert 'NEW REQUIREMENT' in str(out)
    assert 'kept evidence' in str(out)


@pytest.mark.parametrize('args', [dict(interval=0), dict(interval=True), dict(context_k=0), dict(context_k=1.5)])
def test_invalid_config(args):
    with pytest.raises(ValueError):
        ReSumSource(**args)


def test_method_does_not_consume_incomplete_or_protected_pairs():
    items = [dict(id='a', seg='call', call_id='ok', text='read'),
             dict(id='b', seg='out', call_id='ok', text='data'),
             dict(id='c', seg='call', call_id='pending', text='read'),
             dict(id='d', seg='call', call_id='media', text='image'),
             dict(id='e', seg='out', call_id='media', text='image', has_media=True)]
    consumed = []
    sim = SimpleNamespace(ctx=items, size=lambda: 100, sent=lambda: items,
                          current_text=lambda s: s['text'], model_text=lambda *a, **kw: 'summary',
                          summarize_segment=lambda selected, **kw: consumed.extend(selected))
    ReSumSource(interval=1).step(sim, 1)
    assert [s['id'] for s in consumed] == ['a', 'b']
