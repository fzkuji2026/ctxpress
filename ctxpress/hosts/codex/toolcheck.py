"""Real Codex/MCP/proxy checks using fake credentials and a loopback model fixture."""
from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from ctxpress.hosts.codex import launch
from ctxpress.core.processes import detach_options
from ctxpress.live.rewrite import content_text


def steps(method, scenario='standard'):
    if scenario == 'hidden-control':
        return [('exec', 'await tools.mcp__ctxpress__delimiter({action:"start",name:"hidden",type:"expl"});')]
    if scenario == 'resume':
        command = 'printf ' + 'x' * 12000
        code = 'text(await tools.exec_command(' + json.dumps(dict(cmd=command, max_output_tokens=50)) + '));'
        return steps(method) + [('exec', code)]
    if scenario == 'graduated':
        if method != 'CWL':
            raise ValueError('graduated fixture is specific to CWL')
        def code(command):
            return 'text(await tools.exec_command(' + json.dumps(dict(cmd=command, max_output_tokens=3000)) + '));'
        return [('delimiter', dict(action='start', name='levels', type='expl')),
                ('exec', code('grep needle search-fixture.txt')),
                ('exec', code('cat read-fixture.txt')),
                ('ctxpress_status', {}), ('delimiter', dict(action='end', description='fixture context'))]
    if scenario != 'standard':
        raise ValueError('unknown method-tool fixture scenario')
    if method == 'LongSeeker':
        return [('ctxpress_status', {}), ('longseeker_compress', dict(start=1, end=1, summary='fixture retained finding'))]
    if method == 'ZipRL':
        return [('ctxpress_status', {}), ('zip_context', dict(summary_level=3, summary='fixture retained finding'))]
    if method == 'CoMem':
        return [('exec', 'text("comem fixture evidence");')] * 3
    if method == 'TACO':
        command = "printf 'remote: Counting objects: 100%% (5/5)\\n%.0s' {1..80} # git fetch"
        code = 'text(await tools.exec_command(' + json.dumps(dict(cmd=command, max_output_tokens=4000)) + '));'
        return [('exec', code), ('taco_feedback', dict(output_id='$latest_taco_output', feedback='need_full_output'))]
    if method == 'CWL':
        return [('delimiter', {'action': 'start', 'name': 'look', 'type': 'expl'}), ('ctxpress_status', {}),
                ('delimiter', {'action': 'end', 'description': 'fixture context'}),
                ('delimiter', {'action': 'start', 'name': 'work', 'type': 'act', 'dependencies': ['look']}),
                ('delimiter', {'action': 'end'})]
    if method == 'DTOC':
        return [('ctxpress_status', {}), ('manage_context', {'enable': [], 'disable': ['tk_001']}),
                ('manage_context', {'enable': ['tk_001'], 'disable': []})]
    if method == 'ReSumSource':
        return [('exec', 'text("resum fixture evidence");')] * 3
    if method == 'ACM':
        return [('ctxpress_status', {}), ('manage_context', {}), ('query_memory', dict(summary_id=1, query='earlier findings'))]
    if method == 'AgentFoldSource':
        return [('ctxpress_status', {}), ('fold_context', dict(start_step=1, end_step=1, summary='fixture retained finding'))]
    if method == 'ContextFoldingTools':
        return [('ctxpress_status', {}), ('context_branch', dict(description='fixture', prompt='inspect status')),
                ('ctxpress_status', {}), ('context_return', dict(message='fixture branch finding'))]
    raise ValueError('offline method-tool fixture does not support ' + method)


def tool_names(tools):
    """Support ordinary function tools and namespaced function catalogs."""
    names = []
    for tool in tools:
        if tool.get('type') == 'namespace':
            for name in tool_names(tool.get('tools', [])):
                names.append(tool['name'] + '.' + name)
        else:
            name = tool.get('name') or (tool.get('function') or {}).get('name')
            if name:
                names.append(name)
    return names


def command_result(item):
    """Decode the real shell result, including DTOC's outer result envelope."""
    for line in content_text(item.get('output')).splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and 'exit_code' in value and 'output' in value:
            return value
        if isinstance(value, dict) and isinstance(value.get('tool_result'), str):
            nested = command_result(dict(output=value['tool_result']))
            if nested:
                return nested
    return {}


def observe(method, requests, scenario='standard'):
    views = [{item.get('call_id'): content_text(item.get('output')) for item in request.get('input', [])
              if item.get('type') in ('function_call_output', 'custom_tool_call_output')} for request in requests]
    checks = {}
    if method in ('LongSeeker', 'ZipRL') and len(views) >= 3:
        return dict(tool_seen=bool(views[1].get('fixture_0')), old_output_removed='fixture_0' not in views[2],
                    summary_retained='fixture retained finding' in json.dumps(requests[2]),
                    acknowledged=('Applied compress' if method == 'LongSeeker' else 'Compressed completed') in views[2].get('fixture_1', ''))
    if method == 'CoMem' and len(views) >= 4:
        return dict(initial_tool_executed=any(marker in views[1].get('fixture_0', '') for marker in ('comem fixture evidence', 'comem-fixture-evidence')),
                    old_output_removed='fixture_0' not in views[3], latest_retained='fixture_2' in views[3],
                    summary_retained='Synthetic archived finding.' in json.dumps(requests[3]))
    if method == 'TACO' and len(views) >= 3:
        return dict(compressed='[TACO output_id=' in views[1].get('fixture_0', ''),
                    restored='Restored original output' in views[2].get('fixture_1', ''),
                    full_lines_restored=views[2].get('fixture_0','').count('remote: Counting objects:') == 80)
    if scenario == 'graduated':
        if len(views) < 6:
            return checks
        return dict(search_present_before_end='needle ' in views[4].get('fixture_1', ''),
                    search_evicted='fixture_1' not in views[5],
                    read_preserved='read ' * 100 in views[5].get('fixture_2', ''),
                    other_tool_preserved='method: CWL' in views[5].get('fixture_3', ''),
                    episode_preserved='fixture_0' in views[5] and 'fixture_4' in views[5])
    if method == 'CWL' and len(views) >= 6:
        checks = dict(start_ack='start [expl] look' in views[1].get('fixture_0', ''),
                      status_reached_model='method: CWL' in views[2].get('fixture_1', ''),
                      completed_exploration_evicted='fixture_1' not in views[3],
                      dependency_restored='method: CWL' in views[4].get('fixture_1', ''),
                      action_ack='start [act] work dep=look' in views[4].get('fixture_3', ''))
    if method == 'DTOC' and len(views) >= 4:
        def output(round, cid):
            try:
                return json.loads(views[round].get(cid, ''))
            except ValueError:
                return {}
        first, hidden, restored = [output(r, 'fixture_0') for r in (1, 2, 3)]
        checks = dict(registered=first.get('tool_key') == 'tk_001' and 'method: DTOC' in first.get('tool_result', ''),
                      hidden=hidden.get('status') == 'hidden' and 'tool_result' not in hidden,
                      disable_ack='Disabled: tk_001' in output(2, 'fixture_1').get('tool_result', ''),
                      restored=restored.get('tool_result') == first.get('tool_result') and bool(first.get('tool_result')),
                      enable_ack='Re-enabled: tk_001' in output(3, 'fixture_2').get('tool_result', ''))
    if method == 'ReSumSource' and len(views) >= 4:
        checks = dict(initial_tool_seen='resum fixture evidence' in views[1].get('fixture_0', ''),
                      old_history_removed='fixture_0' not in views[2],
                      continuation='Below is a summary of the previous conversation.' in json.dumps(requests[2]),
                      summary_text='<summary>Synthetic archived finding.</summary>' in json.dumps(requests[2]))
    if method == 'ACM' and len(views) >= 4:
        checks = dict(status_reached_model='method: ACM' in views[1].get('fixture_0', ''),
                      archived='fixture_0' not in views[2],
                      summary_recorded='[summary_id: 1]' in views[2].get('fixture_1', ''),
                      archive_queried='[query_memory: summary_id=1]' in views[3].get('fixture_2', ''))
    if method == 'AgentFoldSource' and len(views) >= 3:
        checks = dict(status_reached_model='method: AgentFoldSource' in views[1].get('fixture_0', ''),
                      old_output_folded='fixture_0' not in views[2],
                      fold_ack='Folded steps 1-1.' in views[2].get('fixture_1', ''),
                      summary_retained='fixture retained finding' in json.dumps(requests[2]))
    if method == 'ContextFoldingTools' and len(views) >= 5:
        checks = dict(parent_retained='method: ContextFoldingTools' in views[4].get('fixture_0', ''),
                      branch_started='Branch mode: fixture' in views[2].get('fixture_1', ''),
                      branch_work_observed='method: ContextFoldingTools' in views[3].get('fixture_2', ''),
                      branch_work_removed='fixture_2' not in views[4],
                      return_projected='Branch has finished its task, the returned message is:\n\nfixture branch finding' in views[4].get('fixture_1', ''))
    return checks


def check(binary, directory, method, timeout=45, *, model="offline-fixture", model_catalog=None, scenario='standard'):
    if scenario in ('graduated', 'resume', 'hidden-control') and not model_catalog:
        raise ValueError('graduated check requires the selected Code Mode model catalog')
    if method in ('ReSumSource', 'CoMem', 'TACO') and not model_catalog:
        raise ValueError('ReSum fixture uses native Code Mode tools and requires a model catalog')
    probe_code_mode = bool(model_catalog) and method not in ('ReSumSource', 'CoMem', 'TACO', 'LongSeeker', 'ZipRL')
    from ctxpress.hosts.codex.doctor import inspect_binary
    inspected = inspect_binary(binary)
    if not inspected['supported']:
        raise ValueError('selected Codex lacks required standalone/profile/config-check capabilities')
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    requests, blocked, errors = [], [], []
    script = steps(method, scenario)
    class API(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_CONNECT(self):
            blocked.append(True)
            self.send_response(403); self.send_header('Content-Length', '0'); self.end_headers()
        def do_GET(self):
            raw = b'{"models":[],"data":[],"object":"list"}'
            self.send_response(200); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
            if method in ('ACM', 'ReSumSource', 'CoMem', 'TACO') and not request.get('tools') and not any(x.get('type') == 'additional_tools' for x in request.get('input', [])):
                auxiliary_text = 'Synthetic archived finding.'
                if method == 'TACO':
                    from ctxpress.methods._paper_sources.taco.seed_rules import get_seed_rules
                    auxiliary_text = json.dumps({'selected_rule_ids': [r.rule_id for r in get_seed_rules()]})
                response = dict(id='resp_aux_fixture' , object='response', status='completed', model=model,
                    output=[dict(type='message', role='assistant', content=[dict(type='output_text', text=auxiliary_text)])],
                    usage=dict(input_tokens=50, output_tokens=5, input_tokens_details=dict(cached_tokens=0)))
                raw = json.dumps(response).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
                return
            index = len(requests)
            requests.append(request)
            resumed_steps = ([('delimiter', dict(action='start', name='resumed', type='act', dependencies=['look'])),
                              ('delimiter', dict(action='end'))] if method == 'CWL' else
                             [('manage_context', dict(enable=[], disable=['tk_001'])),
                              ('manage_context', dict(enable=['tk_001'], disable=[]))])
            if index < len(script) or (scenario == 'resume' and len(script)+4 <= index < len(script)+6):
                name, args = script[index] if index < len(script) else resumed_steps[index-len(script)-4]
                if name == 'taco_feedback':
                    import re
                    match = re.search(r'\[TACO output_id=([^;]+);', json.dumps(request))
                    args = dict(args, output_id=match.group(1) if match else 'missing')
                found = next((candidate for candidate in tool_names(request.get('tools', []) + [t for item in request.get('input', [])
                                  if item.get('type') == 'additional_tools' for t in item.get('tools', [])])
                              if candidate == name or candidate.endswith('__' + name) or candidate.endswith('.' + name)), None)
                if found is None:
                    errors.append('required MCP tool absent from model request: ' + name)
                item = dict(id='fc_fixture_' + str(index), type='function_call', status='completed',
                            call_id='fixture_' + str(index), name=found or 'mcp__ctxpress__' + name,
                            arguments=json.dumps(args))
                if found and '.' in found:
                    item['namespace'], item['name'] = found.rsplit('.', 1)
                if name == 'exec' and isinstance(args, str):
                    item = dict(id='ct_fixture_' + str(index), type='custom_tool_call', status='completed',
                                call_id='fixture_' + str(index), name='exec', input=args)
            elif probe_code_mode and index == len(script):
                item = dict(id='ct_fixture_exec', type='custom_tool_call', status='completed',
                    call_id='fixture_exec', name='exec',
                    input='text(await tools.exec_command({cmd:"pwd",max_output_tokens:100}));')
            elif probe_code_mode and index == len(script) + 1:
                item = dict(id='ct_fixture_yield', type='custom_tool_call', status='completed',
                    call_id='fixture_yield', name='exec',
                    input='// @exec: {"yield_time_ms": 1}\nawait new Promise(resolve => setTimeout(resolve, 100)); text("done");')
            elif probe_code_mode and index == len(script) + 2:
                import re
                output = next(x for x in request['input'] if x.get('type') == 'custom_tool_call_output'
                              and x.get('call_id') == 'fixture_yield')
                # DTOC wraps even yielded outputs in its author-compatible envelope.
                text = content_text(output.get('output'))
                if method == 'DTOC':
                    text = json.loads(text)['tool_result']
                match = re.search(r'cell ID ([^\s]+)', text)
                cell = match.group(1) if match else 'missing-cell'
                item = dict(id='fc_fixture_wait', type='function_call', status='completed',
                    call_id='fixture_wait', namespace='functions', name='wait',
                    arguments=json.dumps(dict(cell_id=cell, yield_time_ms=1000)))
            else:
                item = dict(id='msg_fixture', type='message', status='completed', role='assistant',
                            content=[dict(type='output_text', text='fixture completed', annotations=[])])
            response = dict(id='resp_fixture_' + str(index), object='response', created_at=1, status='completed',
                            model=model, output=[item], usage=dict(input_tokens=50, output_tokens=2,
                            total_tokens=52, input_tokens_details=dict(cached_tokens=0)))
            events = [dict(type='response.created', response=dict(response, status='in_progress', output=[])),
                      dict(type='response.output_item.added', output_index=0, item=dict(item, status='in_progress')),
                      dict(type='response.output_item.done', output_index=0, item=item),
                      dict(type='response.completed', response=response)]
            raw = ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n' for event in events).encode()
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.send_header('Content-Length', str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
    server = HTTPServer(('127.0.0.1', 0), API)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f'http://127.0.0.1:{server.server_port}'
    try:
        with tempfile.TemporaryDirectory(prefix='codex-tools-', dir=root) as home:
            env = dict(os.environ, CODEX_HOME=home, CTXPRESS_HOME=home+'/ctxpress', OPENAI_API_KEY='ctxpress-offline-fixture',
                       CODEX_API_KEY='ctxpress-offline-fixture', NO_PROXY='localhost,127.0.0.1', no_proxy='localhost,127.0.0.1')
            for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
                env[key] = endpoint
            workspace = Path(home)/'workspace'; workspace.mkdir()
            if scenario == 'graduated':
                (workspace/'search-fixture.txt').write_text('needle ' * 700 + '\n')
                (workspace/'read-fixture.txt').write_text('read ' * 900 + '\n')
            entry = {'class': method, 'args': {'budget': 2000 if scenario == 'graduated' else 50} if method == 'CWL' else ({'interval': 2} if method == 'ReSumSource' else {})}
            config = root/(Path(home).name+'.json')
            config.write_text(json.dumps(dict(method=entry, upstream=endpoint, workspace=str(workspace),
                scenario=scenario, model=model, model_catalog=str(Path(model_catalog).resolve()) if model_catalog else None)), encoding='utf-8')
            try:
                process = subprocess.Popen([sys.executable, '-m', 'ctxpress.hosts.codex.toolcheck', str(config), inspected['binary']],
                    env=env, cwd=Path(__file__).resolve().parents[3], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **detach_options())
                timed_out = False
                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, check=True)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate(timeout=10)
                checks = observe(method, requests, scenario)
                if probe_code_mode:
                    outputs = [item for request in requests for item in request.get('input', [])
                               if item.get('call_id') == 'fixture_exec' and item.get('type') == 'custom_tool_call_output']
                    checks['ordinary_code_mode_observed'] = bool(outputs) and all(
                        (item.get('internal_chat_message_metadata_passthrough') or {}).get('tool_calls_complete') is True
                        for item in outputs)
                    checks['ordinary_command_succeeded'] = bool(outputs) and all(
                        command_result(item).get('exit_code') == 0 and str(workspace) in command_result(item).get('output', '')
                        for item in outputs)
                logs = list(Path(home).glob('ctxpress/runs/*/requests.jsonl'))
                rows = [json.loads(line) for path in logs for line in path.read_text(encoding='utf-8').splitlines()]
                if probe_code_mode:
                    routing = [row for row in rows if row.get('method_tool_route')]
                    checks['async_cell_resolved'] = (any(row.get('method_tool_pending_cells') for row in routing)
                        and bool(routing) and routing[-1].get('method_tool_pending_cells') == []
                        and not any(row.get('type') == 'method_tool_route_failed' for row in rows))
                if probe_code_mode:
                    issued = {row['receipt'] for row in rows if row.get('type') == 'method_tool_receipt_issued'}
                    observed_receipts = {row['receipt'] for row in rows if row.get('type') == 'method_tool_receipt_observed'}
                    checks['control_receipts_accounted'] = bool(issued) and issued <= observed_receipts
                    checks['receipts_not_sent_to_model'] = '<ctxpress-control-receipt:' not in json.dumps(requests)
                if scenario == 'resume':
                    checks['new_launch_resumed'] = len(logs) == 2 and len(requests) == len(script) + 7
                    resumed_outputs = [content_text(item.get('output')) for request in requests for item in request.get('input', [])
                                       if item.get('call_id') == 'fixture_' + str(len(script)+4) and item.get('type') == 'function_call_output']
                    checks['resumed_control_state'] = bool(resumed_outputs) and all(
                        ('start [act] resumed dep=look' if method == 'CWL' else 'Disabled: tk_001') in text for text in resumed_outputs)
                    checks['truncated_inventory_observed'] = any(row.get('method_tool_incomplete_inventory') for row in rows)
                    checks['resumed_native_inventory_restored'] = any(row.get('method_tool_restored_inventory') for row in rows)
                    checks['resumed_control_receipts_observed'] = sum(row.get('type') == 'method_tool_receipt_observed' for row in rows) > len(issued)
                    long_outputs = [item for request in requests for item in request.get('input', [])
                                    if item.get('call_id') == 'fixture_' + str(len(script)-1)
                                    and item.get('type') == 'custom_tool_call_output']
                    checks['long_command_succeeded'] = bool(long_outputs) and all(
                        command_result(item).get('exit_code') == 0 and 'xxx' in command_result(item).get('output', '')
                        for item in long_outputs)
                expected_exit = process.returncode == 0
                if scenario == 'hidden-control':
                    from ctxpress.live.control_receipts import route_failures
                    outputs = [item for request in requests for item in request.get('input', [])
                               if item.get('call_id') == 'fixture_0' and item.get('type') == 'custom_tool_call_output']
                    checks = dict(control_executed=bool(issued), no_direct_boundary=not observed_receipts,
                        output_discarded=bool(outputs) and all('start [expl]' not in content_text(item.get('output')) for item in outputs),
                        host_inventory_unavailable=bool(outputs) and all(not (item.get('internal_chat_message_metadata_passthrough') or {}).get('executed_tool_calls') for item in outputs),
                        receipt_audit_rejected=any('no verified direct' in f['error'] for f in route_failures(dict(rewrites=rows))),
                        receipts_not_sent_to_model='<ctxpress-control-receipt:' not in json.dumps(requests))
                    expected_exit = process.returncode != 0
                cleaned = not list(Path(home).glob('ctxpress-*.config.toml'))
                result = dict(schema='ctxpress.hosts.codex.offline-tools-check', version=1, method=method, scenario=scenario, test_only=True,
                    installed=inspected, model=model, model_catalog_sha256=(__import__('hashlib').sha256(
                        Path(model_catalog).read_bytes()).hexdigest() if model_catalog else None), passed=expected_exit and not timed_out and not errors and bool(checks) and all(checks.values()) and cleaned,
                    returncode=process.returncode, timed_out=timed_out, requests=len(requests), checks=checks, errors=errors,
                    owned_profiles_cleaned=cleaned, blocked_auxiliary_connections=len(blocked),
                    evidence='real installed CLI and MCP/proxy on a synthetic loopback API; no paid API or task-quality evidence')
                # These contain only deterministic fixture input/output and fake credentials.
                (root/(method.lower()+'-requests.json')).write_text(json.dumps(requests, ensure_ascii=False, indent=2), encoding='utf-8')
                (root/(method.lower()+'-proxy.json')).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
                (root/(method.lower()+'-cli.log')).write_text(stdout+'\n'+stderr, encoding='utf-8')
                return result
            finally:
                config.unlink(missing_ok=True)
    finally:
        server.shutdown(); server.server_close()


def main():
    value = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    catalog_args = ['-c', 'model_catalog_json=' + json.dumps(value['model_catalog'])] if value.get('model_catalog') else []
    scenario = value.get('scenario', 'standard')
    common = ['--strict-config', '--skip-git-repo-check', '--model', value.get('model', 'offline-fixture'), *catalog_args, '--json']
    args = ['exec', *common, '--ignore-rules', '--sandbox', 'read-only', '--cd', value['workspace']]
    if scenario != 'resume':
        args.append('--ephemeral')
    if scenario == 'hidden-control':
        # Deliberate offline fault injection: prove receipt accounting works even
        # when the host supplies no nested inventory and JS discards the result.
        original_command = launch.codex_command
        def no_inventory(*positional, **keyword):
            keyword['codex_config'] = dict(keyword.get('codex_config') or {}, **{'features.executed_tool_call_metadata': False})
            return original_command(*positional, **keyword)
        launch.codex_command = no_inventory
    args.append('Complete the deterministic MCP fixture sequence. Do not use shell tools.')
    _, result = launch.run(value['method'], codex_bin=sys.argv[2], upstream=value['upstream'], tools=True, codex_args=args)
    if scenario == 'resume' and result[0] == 0:
        # A new proxy and MCP process use the same isolated CODEX_HOME and store
        # parent, exactly as a real harness resume. No user profile is touched.
        _, result = launch.run(value['method'], codex_bin=sys.argv[2], upstream=value['upstream'], tools=True,
            codex_args=['exec', '--sandbox', 'read-only', '--cd', value['workspace'],
                        'resume', '--last', *common, 'Continue the deterministic fixture.'])
    raise SystemExit(result[0])


if __name__ == '__main__':
    main()
