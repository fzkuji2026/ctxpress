"""MCP startup contracts and detection of unsuccessful tool execution."""
from pathlib import Path
import copy
import pytest

from ctxpress.hosts.codex import launch
from ctxpress.live import mcp
from ctxpress.hosts.codex.toolcheck import observe, tool_names
from ctxpress.methods import CWL, DTOC


def test_mcp_starts_from_the_package_directory_and_requires_declared_method_tools():
    config = launch.mcp_config(True, {'CTXPRESS_REQUIRED_TOOLS': '1'})
    assert (Path(config['cwd'])/'ctxpress'/'live'/'mcp.py').is_file()
    assert config['required']
    assert not launch.mcp_config(False, {'CTXPRESS_REQUIRED_TOOLS': '1'})['required']
    assert not launch.mcp_config(True)['required']


def test_only_explicit_method_annotations_are_forwarded(monkeypatch):
    for method in (CWL(), DTOC()):
        monkeypatch.setattr(mcp, '_METHOD', [method])
        tools = mcp.tools()
        assert all(t['annotations']['readOnlyHint'] for t in tools)
        assert all(not t['annotations']['destructiveHint'] and not t['annotations']['openWorldHint'] for t in tools)
    class Custom:
        agent_tools = ({'name': 'custom', 'description': 'custom', 'inputSchema': {'type': 'object'}},)
    monkeypatch.setattr(mcp, '_METHOD', [Custom()])
    assert 'annotations' not in mcp.tools()[-1]


def test_catalog_namespace_is_distinct_from_the_function_name():
    assert tool_names([{'type': 'namespace', 'name': 'mcp__ctxpress', 'tools': [
        {'type': 'function', 'name': 'delimiter'}]}]) == ['mcp__ctxpress.delimiter']


def test_fixture_cannot_pass_with_unsupported_or_denied_mcp_calls():
    requests = [{'input': [{'type': 'function_call_output', 'call_id': 'fixture_0',
                            'output': 'MCP tool call requires approval, but approval policy is never'}]}] * 6
    checks = observe('CWL', requests)
    assert checks and not all(checks.values())


def test_code_mode_advertises_method_namespace_without_changing_normal_tools():
    from ctxpress.live.method_tools import MethodTools
    original = dict(model='selected', input=[dict(type='additional_tools', role='developer',
        tools=[dict(type='namespace', name='functions', tools=[dict(type='custom', name='exec')])])])
    saved = copy.deepcopy(original)
    for method, name in [(CWL(), 'delimiter'), (DTOC(), 'manage_context')]:
        body = MethodTools(method).adapt(original)
        catalogs = body['input'][1]['tools']
        assert catalogs[0] == original['input'][0]['tools'][0]
        assert 'mcp__ctxpress.' + name in tool_names(catalogs)
        assert body['model'] == original['model']
        assert original == saved


@pytest.mark.parametrize('method,name', [(CWL, 'delimiter'), (DTOC, 'manage_context')])
def test_executed_nested_control_is_rejected_even_without_printed_output(method, name):
    from ctxpress.live.method_tools import MethodTools, MethodToolRouteError
    body = dict(input=[dict(type='custom_tool_call', name='exec', call_id='a', input='arbitrary JS'),
        dict(type='custom_tool_call_output', call_id='a', output='',
             internal_chat_message_metadata_passthrough=dict(tool_calls_complete=True,
                 executed_tool_calls=[dict(name='mcp__ctxpress__' + name)]))])
    with pytest.raises(MethodToolRouteError, match='inside Code Mode'):
        MethodTools(method()).adapt(body)


def test_unverified_code_mode_inventory_cannot_silently_pass():
    from ctxpress.live.method_tools import MethodTools, MethodToolRouteError
    body = dict(input=[dict(type='custom_tool_call', name='exec', call_id='a'),
                       dict(type='custom_tool_call_output', call_id='a', output='success')])
    with pytest.raises(MethodToolRouteError, match='incomplete'):
        MethodTools(CWL()).adapt(body)


def test_ordinary_code_mode_output_survives_and_duplicate_namespace_is_not_added():
    from ctxpress.live.method_tools import MethodTools
    adapter = MethodTools(CWL())
    inp = [dict(type='custom_tool_call', name='exec', call_id='a'),
        dict(type='custom_tool_call_output', call_id='a', output='file contents',
             internal_chat_message_metadata_passthrough=dict(tool_calls_complete=True,
                 executed_tool_calls=[dict(name='exec_command')]))]
    body = dict(input=inp, tools=[adapter.namespace])
    changed = adapter.adapt(body)
    assert changed['input'][1:] == inp
    assert changed['tools'] == body['tools']


def test_proxy_route_error_is_not_forwarded_as_an_unmodified_request(tmp_path):
    import http.client
    import json
    import threading
    from ctxpress.live.proxy import serve
    log = tmp_path/'proxy.jsonl'
    server, _ = serve(CWL, 0, 'http://127.0.0.1:1', host='127.0.0.1',
                      log=str(log), codex_method_tools=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = dict(input=[dict(type='custom_tool_call', name='exec', call_id='a'),
            dict(type='custom_tool_call_output', call_id='a', output='no execution inventory')])
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        connection.request('POST', '/responses', json.dumps(body), {'Content-Type': 'application/json'})
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())['error']['code'] == 'ctxpress_method_tool_route_failed'
        connection.close()
        rows = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        assert len(rows) == 1 and rows[0]['type'] == 'method_tool_route_failed'
        assert rows[0]['upstream_sent'] is False
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_async_cell_remains_pending_until_matching_native_wait_inventory():
    from ctxpress.live.method_tools import MethodTools
    adapter = MethodTools(CWL())
    running = [dict(type='custom_tool_call', name='exec', call_id='origin'),
        dict(type='custom_tool_call_output', call_id='origin',
             output='Script running with cell ID 1\nWall time 0.0 seconds\nOutput:\n')]
    observation = {}
    adapter.adapt(dict(input=running), session_key='a', observation=observation)
    assert observation['method_tool_pending_cells'] == ['origin']
    done = [dict(type='function_call', namespace='functions', name='wait', call_id='wait'),
        dict(type='function_call_output', call_id='wait', output='Script completed',
             internal_chat_message_metadata_passthrough=dict(cell_id='origin',
                 executed_tool_calls=[], tool_calls_complete=True))]
    adapter.adapt(dict(input=done), session_key='b', observation=observation)
    adapter.adapt(dict(input=[]), session_key='a', observation=observation)
    assert observation['method_tool_pending_cells'] == ['origin']
    adapter.adapt(dict(input=running + done), session_key='a', observation=observation)
    assert observation['method_tool_pending_cells'] == []
