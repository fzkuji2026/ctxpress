"""Control execution must be accounted for even when native inventories truncate."""
import copy
import json
import uuid
import pytest
from ctxpress.live.control_receipts import ControlReceipts
from ctxpress.live.method_tools import MethodTools, MethodToolRouteError
from ctxpress.live.control_receipts import route_failures
from ctxpress.methods import CWL


def fixture(tmp_path):
    log = tmp_path/'requests.jsonl'
    receipts = ControlReceipts(tmp_path/uuid.uuid4().hex, log)
    args = dict(action='start', name='look', type='expl')
    text = 'start [expl] look'
    marker = receipts.issue('delimiter', args, text, False)
    body = dict(input=[dict(type='function_call', namespace='mcp__ctxpress', name='delimiter',
                           call_id='control', arguments=json.dumps(args)),
                      dict(type='function_call_output', call_id='control', output=[
                          dict(type='input_text', text='Wall time: 0.1 seconds\nOutput:'),
                          dict(type='input_text', text=text), dict(type='input_text', text=marker)])])
    return receipts, body, log


def health(log):
    return route_failures(dict(rewrites=[json.loads(line) for line in log.read_text().splitlines()]))


def test_receipt_is_removed_before_policy_and_provider_and_survives_restart(tmp_path):
    receipts, body, log = fixture(tmp_path)
    saved = copy.deepcopy(body)
    result = MethodTools(CWL(), receipts=receipts).adapt(body)
    assert 'ctxpress-control-receipt:' not in json.dumps(result)
    assert result['input'][-1]['output'][-1]['text'] == 'start [expl] look'
    assert body == saved and health(log) == []
    resumed = ControlReceipts(tmp_path/uuid.uuid4().hex, log)
    assert MethodTools(CWL(), receipts=resumed).adapt(body)['input'] == result['input']
    assert health(log) == []


@pytest.mark.parametrize('change', ['args', 'output', 'call_id', 'missing', 'duplicate', 'unknown'])
def test_changed_or_reused_receipts_fail_closed(tmp_path, change):
    receipts, body, log = fixture(tmp_path)
    adapter = MethodTools(CWL(), receipts=receipts)
    adapter.adapt(body)
    if change == 'args':
        body['input'][0]['arguments'] = '{}'
    elif change == 'output':
        body['input'][1]['output'][1]['text'] = 'different output'
    elif change == 'call_id':
        for item in body['input']: item['call_id'] = 'other'
    elif change == 'missing':
        body['input'][1]['output'].pop()
    elif change == 'duplicate':
        body['input'] += copy.deepcopy(body['input'])
    elif change == 'unknown':
        body['input'][1]['output'][-1]['text'] = '<ctxpress-control-receipt:'+uuid.uuid4().hex+':'+uuid.uuid4().hex+'>'
    with pytest.raises(MethodToolRouteError):
        adapter.adapt(body)


def test_ordinary_truncated_inventory_can_continue_with_independent_receipt_accounting(tmp_path):
    receipts, body, log = fixture(tmp_path)
    body['input'] += [dict(type='custom_tool_call', call_id='long', name='exec'),
        dict(type='custom_tool_call_output', call_id='long', output='Script completed',
             internal_chat_message_metadata_passthrough=dict(cell_id='long', executed_tool_calls=[
                 dict(name='exec_command', arguments={'_codex_executed_tool_call_truncated': {'original_bytes': 9000}})]))]
    observed = {}
    result = MethodTools(CWL(), receipts=receipts).adapt(body, observation=observed)
    assert result['input'][-1] == body['input'][-1]
    assert observed['method_tool_incomplete_inventory'] == ['long']
    assert health(log) == []


def test_nested_unprinted_control_is_invalid_without_any_host_inventory(tmp_path):
    receipts, body, log = fixture(tmp_path)
    body = dict(input=[dict(type='custom_tool_call', call_id='nested', name='exec'),
                       dict(type='custom_tool_call_output', call_id='nested', output='Script completed')])
    MethodTools(CWL(), receipts=receipts).adapt(body)
    # Even a killed launcher without a final audit cannot count this as valid.
    assert len(health(log)) == 1
    assert 'no verified direct' in health(log)[0]['error']
    receipts.audit()
    assert len(health(log)) == 1


def test_nested_printed_receipt_rejected_before_forwarding(tmp_path):
    receipts, body, _ = fixture(tmp_path)
    body['input'][0].update(type='custom_tool_call', name='exec')
    body['input'][1]['type'] = 'custom_tool_call_output'
    with pytest.raises(MethodToolRouteError, match='outside a direct'):
        MethodTools(CWL(), receipts=receipts).adapt(body)


def test_receipt_paths_cannot_follow_symlinks(tmp_path):
    receipts, body, _ = fixture(tmp_path)
    events = receipts.store/'control-receipts'/'events'
    p = next(events.glob('*.json'))
    target = tmp_path/'original.json';p.rename(target)
    p.symlink_to(target)
    with pytest.raises(MethodToolRouteError, match='symlink'):
        MethodTools(CWL(), receipts=receipts).adapt(body)


def test_native_inventory_recovery_requires_exact_call_and_result(tmp_path):
    receipts, _, _ = fixture(tmp_path)
    call = dict(type='custom_tool_call', call_id='shell', name='exec', input='arbitrary JS')
    native = dict(cell_id='shell', executed_tool_calls=[dict(name='exec_command', arguments=dict(cmd='rg key src'))], tool_calls_complete=True)
    output = dict(type='custom_tool_call_output', call_id='shell', output='found key',
                  internal_chat_message_metadata_passthrough=native)
    assert not receipts.inventory(call, output)
    resumed = ControlReceipts(tmp_path/uuid.uuid4().hex)
    restored = dict(output, internal_chat_message_metadata_passthrough=dict(turn_id='new'))
    assert resumed.inventory(call, restored)
    assert restored['internal_chat_message_metadata_passthrough']['executed_tool_calls'] == native['executed_tool_calls']
    for altered_call, altered_output in [(dict(call, input='different JS'), dict(output)),
                                        (dict(call), dict(output, output='different result')),
                                        (dict(call, call_id='reused'), dict(output))]:
        altered_output['internal_chat_message_metadata_passthrough'] = {}
        assert not resumed.inventory(altered_call, altered_output)
        assert not altered_output['internal_chat_message_metadata_passthrough']
    explicitly_incomplete = dict(output, internal_chat_message_metadata_passthrough=dict(tool_calls_complete=False))
    assert not resumed.inventory(call, explicitly_incomplete)
    assert explicitly_incomplete['internal_chat_message_metadata_passthrough'] == dict(tool_calls_complete=False)


def test_parallel_process_telemetry_lines_cannot_interleave(tmp_path):
    import subprocess
    import sys
    log = tmp_path/'parallel.jsonl'
    code = ('import sys; from ctxpress.live.logging import append_record; '
            '[append_record(sys.argv[1], dict(writer=sys.argv[2], index=i, text="x"*16000)) for i in range(24)]')
    children = [subprocess.Popen([sys.executable, '-c', code, str(log), str(i)]) for i in range(3)]
    assert [p.wait(timeout=15) for p in children] == [0, 0, 0]
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == len({(r['writer'], r['index']) for r in rows}) == 72
    assert all(r['text'] == 'x'*16000 for r in rows)


def test_new_mcp_process_rebuilds_author_state_before_accepting_next_control(tmp_path, monkeypatch):
    from ctxpress.live import mcp
    receipts, body, log = fixture(tmp_path)
    adapter = MethodTools(CWL(), receipts=receipts)
    adapter.adapt(body)
    monkeypatch.setenv('CTXPRESS_CONTROL_RECEIPTS', '1')
    monkeypatch.setenv('CTXPRESS_STORE_DIR', str(receipts.store))
    monkeypatch.setenv('CTXPRESS_LOG_PATH', str(log))
    monkeypatch.setenv('CTXPRESS_METHOD_CONFIG', json.dumps(dict(**{'class': 'CWL'})))
    monkeypatch.setattr(mcp, '_METHOD', [])
    monkeypatch.setattr(mcp, '_METHOD_RECEIPTS', [])
    end = dict(action='end', description='looked at source')
    result = mcp.handle(dict(id=1, method='tools/call', params=dict(name='delimiter', arguments=end)))['result']
    assert not result['isError']
    assert result['content'][0]['text'] == 'end [expl] look — looked at source'
    body['input'] += [dict(type='function_call', namespace='mcp__ctxpress', name='delimiter', call_id='end', arguments=json.dumps(end)),
                      dict(type='function_call_output', call_id='end', output=[dict(type='input_text', text=p['text']) for p in result['content']])]
    adapter.adapt(body)
    mcp._METHOD.clear();mcp._METHOD_RECEIPTS.clear()
    action = dict(action='start', name='work', type='act', dependencies=['look'])
    reply = mcp.handle(dict(id=2, method='tools/call', params=dict(name='delimiter', arguments=action)))['result']
    assert not reply['isError'] and reply['content'][0]['text'] == 'start [act] work dep=look'
