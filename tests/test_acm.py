import json

from ctxpress.live.rewrite import Rewriter
from ctxpress.methods.acm import ACM


class Service:
    def __init__(self):
        self.calls = []
        self.fail = False
    def summarize_prompt(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        if self.fail:
            raise RuntimeError('summary failed')
        return 'The earlier finding was ALPHA.'


def start(store):
    service = Service()
    r = Rewriter(lambda: ACM(summary_model='cheap-model'), store_dir=str(store) if store else None, summarizer=service)
    items = [dict(type='message', role='user', content='Solve the task'),
             dict(type='additional_tools', tools=[])]
    append(items, 'a', 'read', {}, 'ALPHA details from file')
    r.rewrite_body(dict(input=items), 's')
    return r, service, items


def append(items, cid, name, args, output='recorded'):
    items.extend([dict(type='function_call', call_id=cid, name=name, arguments=json.dumps(args)),
                  dict(type='function_call_output', call_id=cid, output=output)])


def test_archive_compress_query_and_repeated_history_are_transactional(tmp_path):
    r, service, items = start(tmp_path)
    append(items, 'm', 'manage_context', {})
    out, info = r.rewrite_body(dict(input=items), 's')
    assert not any(x.get('call_id') == 'a' for x in out['input'])
    assert '[summary_id: 1]' in next(x['output'] for x in out['input'] if x.get('call_id') == 'm' and 'output' in x)
    archives = list(tmp_path.rglob('acm-summary-*.json'))
    assert len(archives) == 1 and 'ALPHA' in archives[0].read_text()
    assert service.calls[0][2]['model'] == 'cheap-model'
    r.rewrite_body(dict(input=items), 's')
    assert len(service.calls) == 1
    append(items, 'q', 'query_memory', {'summary_id': 1, 'query': 'What was the finding?'})
    out, _ = r.rewrite_body(dict(input=items), 's')
    assert 'ALPHA details' in service.calls[-1][1]
    assert '[query_memory: summary_id=1]' in str(out)
    # A second compression keeps the first checkpoint, and uses a separate immutable archive.
    append(items, 'b', 'read', {}, 'BETA')
    append(items, 'm2', 'manage_context', {})
    out, _ = r.rewrite_body(dict(input=items), 's')
    assert '[summary_id: 1]' in str(out) and '[summary_id: 2]' in str(out)
    assert len(list(tmp_path.rglob('acm-summary-*.json'))) == 2


def test_missing_disk_or_failed_summary_retains_history(tmp_path):
    for store, fail in ((None, False), (tmp_path, True)):
        r, service, items = start(store)
        service.fail = fail
        append(items, 'm', 'manage_context', {})
        out, _ = r.rewrite_body(dict(input=items), 's')
        assert any(x.get('call_id') == 'a' for x in out['input'])
        assert 'Error:' in str(out)
        assert not list(tmp_path.rglob('acm-summary-*.json'))


def test_corrupt_archive_is_not_sent_to_model(tmp_path):
    r, service, items = start(tmp_path)
    append(items, 'm', 'manage_context', {})
    r.rewrite_body(dict(input=items), 's')
    next(tmp_path.rglob('acm-summary-*.json')).write_text('CORRUPTED')
    append(items, 'q', 'query_memory', {'summary_id': 1, 'query': 'detail'})
    out, _ = r.rewrite_body(dict(input=items), 's')
    assert len(service.calls) == 1 and 'missing or changed' in str(out)


def test_session_isolation_and_invalid_arguments(tmp_path):
    r, service, items = start(tmp_path)
    append(items, 'm', 'manage_context', {})
    r.rewrite_body(dict(input=items), 's')
    other = [dict(type='message', role='user', content='Other task')]
    append(other, 'q', 'query_memory', {'summary_id': 1, 'query': 'detail'})
    out, _ = r.rewrite_body(dict(input=other), 'other')
    assert 'unknown summary_id' in str(out) and len(service.calls) == 1
    for args in ({'summary_id': True, 'query': 'x'}, {'summary_id': 1, 'query': ''}, []):
        assert ACM().call_tool('query_memory', args)[1]


def test_host_compaction_keeps_existing_archive_ids_queryable(tmp_path):
    r, service, items = start(tmp_path)
    append(items, 'm', 'manage_context', {})
    r.rewrite_body(dict(input=items), 's')
    revised = [dict(type='message', role='user', content='Solve the task'),
               dict(type='compaction', encrypted_content='opaque')]
    append(revised, 'q', 'query_memory', {'summary_id': 1, 'query': 'What was the finding?'})
    out, info = r.rewrite_body(dict(input=revised), 's')
    assert info['history_rebased']
    assert 'ALPHA details from file' in service.calls[-1][1]
    assert '[query_memory: summary_id=1]' in str(out)
    append(revised, 'b', 'read', {}, 'NEW DATA')
    append(revised, 'm2', 'manage_context', {})
    out, _ = r.rewrite_body(dict(input=revised), 's')
    assert '[summary_id: 2]' in str(out) and len(list(tmp_path.rglob('acm-summary-*.json'))) == 2
