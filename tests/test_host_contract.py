import copy

import pytest

from ctxpress.live.contract import HostContractError, validate
from ctxpress.harness.method_check import check
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods.base import Method


@pytest.mark.parametrize('item', [
    dict(type='additional_tools', tools=[]),
    dict(type='compaction', encrypted_content='opaque'),
    dict(type='message', role='developer', content='keep'),
    dict(type='message', role='user', content=[dict(type='input_image', image_url='data:image/png;base64,xx')]),
])
def test_missing_or_modified_host_state_rejected(item):
    assert validate([item], [copy.deepcopy(item)])['valid']
    with pytest.raises(HostContractError):
        validate([item], [])
    with pytest.raises(HostContractError):
        validate([item, item], [item])


def test_unpaired_render_rejected_and_whole_pair_deletion_allowed():
    pair = [dict(type='function_call', call_id='a'), dict(type='function_call_output', call_id='a')]
    with pytest.raises(HostContractError):
        validate(pair, pair[:1])
    assert validate(pair, [])['valid']


def test_method_cannot_bypass_protection_by_editing_context_directly():
    class Broken(Method):
        def step(self, sim, r):
            sim.ctx.clear()
    with pytest.raises(HostContractError):
        Rewriter(Broken).rewrite_body(dict(input=[dict(type='additional_tools', tools=[])]), 's')


@pytest.mark.parametrize('name,args', [('ComplexityTrap', {'n': 1}), ('ComplexityTrapSummary', {'n': 2, 'm': 1}),
                                      ('DTOC', {}), ('ACM', {})])
def test_synthetic_trigger_continuation_resume_and_revision(name, args):
    result = check(name, args, turns=8, output_chars=256)
    assert result['triggered'] and result['contract_valid']
    assert {s['scenario'] for s in result['samples']} == {'incremental', 'frozen_history_first_request', 'host_history_revision'}
    assert all(s['host_contract']['valid'] for s in result['samples'])


def test_nontrigger_is_explicit_and_remote_methods_never_called():
    assert check('NoCompaction', turns=2)['triggered'] is False
    with pytest.raises(ValueError, match='network-free'):
        check('SWEPruner', {'url': 'http://localhost:1'})


def test_proxy_blocks_contract_failure_before_upstream_and_marks_execution_invalid(tmp_path):
    import http.client
    import json
    import threading
    from ctxpress.live.proxy import ThreadingHTTPServer, make_handler
    from ctxpress.harness.execution_health import observe
    class Broken(Method):
        def step(self, sim, r):
            sim.ctx.clear()
    log = tmp_path/'requests.jsonl'
    # Unreachable upstream: receiving our explicit 422 proves no forwarding fallback occurred.
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(Rewriter(Broken), 'http://127.0.0.1:1', None, str(log)))
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    try:
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        client.request('POST', '/responses', body=json.dumps(dict(model='fake', input=[dict(type='additional_tools', tools=[])])),
                       headers={'Content-Type': 'application/json'})
        response = client.getresponse(); assert response.status == 422; response.read(); client.close()
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=2)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert rows[-1]['type'] == 'host_contract_failed' and rows[-1]['upstream_sent'] is False
    assert observe(dict(rewrites=rows))['execution_invalid']
