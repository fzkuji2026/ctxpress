"""No-compaction host contract and current Codex /responses compaction routing."""
import json
import pytest
from ctxpress.hosts.codex.launch import codex_command
from ctxpress.live.telemetry import summary
from ctxpress.harness.results.mechanism import observe
from ctxpress.live.usage import analyze
from ctxpress.live.proxy import is_native_compaction
from ctxpress.methods import NoCompaction, CodexAutoCompact
from ctxpress.methods.wrappers import PinRequirements, Composed
from test_native_compaction import fixture


@pytest.mark.parametrize('transport', ['legacy', 'header', 'body', 'prompt'])
def test_no_compaction_stops_before_upstream_without_unknown_bill(tmp_path, transport):
    log = tmp_path/'requests.jsonl'
    body = {'model':'fixture', 'input':[]}
    headers = {}
    metadata = json.dumps({'request_kind':'compaction','private-extra':'do-not-log'})
    path = '/responses'
    if transport == 'legacy': path += '/compact?fixture=true'
    if transport == 'header': headers['x-codex-turn-metadata'] = metadata
    if transport == 'body': body['client_metadata'] = {'x-codex-turn-metadata':metadata}
    if transport == 'prompt': body['input'] = [{'role':'user','content':NoCompaction.codex_config['compact_prompt']}]
    with fixture(log, method=NoCompaction) as (send, got, rw):
        status, response = send(path, json.dumps(body).encode(), headers)
        assert status == 400 and json.loads(response)['error']['code'] == 'ctxpress_compaction_disabled'
        assert got == [] and rw.sessions == {}
        # The stop belongs to the affected native turn, not other sessions using the proxy.
        status, _ = send('/responses', b'{"model":"fixture","input":[]}')
        assert status == 200 and len(got) == 1
    rows = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
    assert rows[0]['type'] == 'native_compaction_blocked' and rows[0]['upstream_sent'] is False
    telemetry = summary(str(log))
    assert telemetry['native_compaction_calls'] == 0 and telemetry['native_compaction_blocked'] == 1
    bill = analyze(dict(requests=1, usage=telemetry, rewrites=rows))
    assert bill['complete'] and bill['usage_by_model']['fixture']['requests'] == 1 and bill['missing_api_usage'] == 0
    assert observe(dict(usage=telemetry,rewrites=rows))['blocked_native_compactions'] == 1
    assert observe(dict(usage=telemetry,rewrites=rows))['native_compaction_metadata_coverage'] is True
    assert 'do-not-log' not in log.read_text(encoding='utf-8')


@pytest.mark.parametrize('transport', ['header', 'body'])
def test_streaming_native_compaction_bypasses_method_and_counts_separately(tmp_path, transport):
    log = tmp_path/'requests.jsonl'
    metadata = json.dumps({'request_kind':'compaction'})
    body = {'model':'fixture','input':[{'role':'user','content':'private task'}]}
    headers = {}
    if transport == 'header': headers['x-codex-turn-metadata'] = metadata
    else: body['client_metadata'] = {'x-codex-turn-metadata':metadata}
    raw = json.dumps(body).encode()
    with fixture(log, method=CodexAutoCompact) as (send,got,rw):
        status, _ = send('/responses', raw, headers)
        assert status == 200 and got[0][1] == raw and rw.sessions == {}
        send('/responses', b'{"model":"fixture","input":[]}')
    rows = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 2 and rows[0]['type'] == 'native_compaction' and rows[0]['completed']
    telemetry = summary(str(log))
    assert telemetry['requests'] == 1 and telemetry['native_compaction_calls'] == 1
    bill = analyze(dict(requests=1, usage=telemetry, rewrites=rows))
    assert bill['complete'] and bill['usage_by_model']['fixture']['main_requests'] == 1 and bill['native_compaction_calls'] == 1


@pytest.mark.parametrize('raw', ['broken', '[]', 'null', '"compaction"', '{"request_kind":"turn"}'])
def test_regular_history_and_malformed_metadata_are_not_compaction(raw):
    body = {'client_metadata':{'x-codex-turn-metadata':raw},
            'input':[{'role':'user','content':'Please explain request_kind compaction.'}]}
    assert not is_native_compaction(body, raw)


def test_old_zero_count_does_not_claim_metadata_coverage():
    result = {'usage':{'native_compaction_calls':0},'rewrites':[{'request':1,'status':200}]}
    assert observe(result)['native_compaction_logging_available']
    assert not observe(result)['native_compaction_metadata_coverage']


def test_wrappers_preserve_native_stop_and_command_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME',str(tmp_path))
    for method in [NoCompaction(), PinRequirements(NoCompaction()), Composed([NoCompaction()])]:
        assert method.allow_native_compaction is False
        cmd = codex_command('codex', 1234, ['-c','model_auto_compact_token_limit=230000','exec','fixture'],
                            write=False, tools=False, codex_config=method.codex_config)
        text = '\n'.join(cmd)
        assert 'model_auto_compact_token_limit=2147483647' in text
        assert 'model_auto_compact_token_limit_scope="body_after_prefix"' in text
        assert 'model_post_turn_compact_threshold_percent=0' in text
        assert 'model_context_window=' not in text and 'model_catalog_json=' not in text
