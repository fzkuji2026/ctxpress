"""Keep method control calls visible as individual Responses call/output pairs.

Code Mode can execute MCP calls whose boundaries disappear inside one exec
output. CWL and DTOC need those boundaries. Advertise their native namespace
directly, without changing the model catalog or the ordinary tool interface.
"""
from __future__ import annotations
import copy, re, threading


class MethodToolRouteError(ValueError):
    pass


class MethodTools:
    def __init__(self, method, *, receipts=None):
        self.receipts = receipts
        from ctxpress.live.mcp import TOOLS
        self.names = {tool['name'] for tool in method.agent_tools}
        self.nested_names = {'mcp__ctxpress__' + name for name in self.names}
        self.pending = {}
        self.cells = {}
        self.lock = threading.Lock()
        self.namespace = dict(type='namespace', name='mcp__ctxpress',
            description='Context management tools. Call these directly, outside functions.exec.',
            tools=[dict(type='function', name=tool['name'], description=tool['description'],
                        strict=False, parameters=copy.deepcopy(tool['inputSchema']))
                   for tool in [*TOOLS, *method.agent_tools]])
        names = ', '.join('mcp__ctxpress.' + name for name in sorted(self.names))
        self.instructions = (f'Call {names} directly as standalone function calls in the '
            'mcp__ctxpress namespace. Do not invoke these method control tools inside '
            'functions.exec, JavaScript, or a parallel wrapper: the context manager '
            'requires a separate call/output boundary for each control operation. '
            'Continue using Code Mode normally for other tools.')

    def adapt(self, body, *, session_key='default', observation=None):
        if not self.names or not isinstance(body.get('input'), list):
            return body
        body = copy.deepcopy(body)
        if self.receipts:
            self.verify_receipts(body)
        items = body['input']
        calls = {item.get('call_id'): item for item in items
                 if item.get('type') in ('custom_tool_call', 'function_call')}
        pending, completed, cells = set(), set(), {}
        incomplete = []
        restored_inventory = []
        for item in items:
            call = calls.get(item.get('call_id'), {})
            if item.get('type') not in ('custom_tool_call_output', 'function_call_output'):
                continue
            if call.get('name') not in ('exec', 'functions.exec', 'wait', 'functions.wait'):
                continue
            if self.receipts:
                try:
                    with self.lock:
                        restored = self.receipts.inventory(call, item)
                    if restored:
                        restored_inventory.append(item.get('call_id'))
                except (OSError, ValueError, TypeError) as error:
                    raise MethodToolRouteError('cannot verify retained Code Mode inventory: '+str(error)) from error
            metadata = item.get('internal_chat_message_metadata_passthrough') or {}
            if not isinstance(metadata, dict):
                raise MethodToolRouteError('Code Mode tool inventory is malformed')
            executed = metadata.get('executed_tool_calls', [])
            if not isinstance(executed, list) or any(not isinstance(tool, dict) or
                    not isinstance(tool.get('name'), str) for tool in executed):
                raise MethodToolRouteError('Code Mode tool inventory is malformed')
            if any(tool['name'] in self.nested_names for tool in executed):
                raise MethodToolRouteError('method control tool executed inside Code Mode; '
                                          'separate call/output boundaries are required')
            origin = metadata.get('cell_id')
            if metadata.get('tool_calls_complete') is True:
                completed.add(origin or item.get('call_id'))
                continue
            from ctxpress.live.rewrite import output_text
            running = re.match(r'^Script running with cell ID ([^\s]+)\n', output_text(item))
            if running:
                # Native yielded exec outputs have no inventory yet. The final
                # wait carries cell_id=the originating exec call_id, not the
                # integer runtime cell id shown in the user-visible output.
                runtime_cell = running.group(1)
                if call.get('name') in ('exec', 'functions.exec'):
                    origin = item.get('call_id')
                    cells[runtime_cell] = origin
                else:
                    with self.lock:
                        origin = origin or cells.get(runtime_cell) or self.cells.get(session_key, {}).get(runtime_cell)
                if isinstance(origin, str):
                    pending.add(origin)
                    continue
            if self.receipts:
                # An ordinary long command can clear host inventory completion.
                # MCP receipts independently account for every control invocation.
                # Preserve the incomplete metadata: CWL must not guess its category.
                incomplete.append(item.get('call_id'))
                if not origin and call.get('name') in ('wait', 'functions.wait'):
                    import json
                    try:
                        args = json.loads(call.get('arguments', '{}'))
                        with self.lock:
                            origin = cells.get(str(args.get('cell_id'))) or self.cells.get(session_key, {}).get(str(args.get('cell_id')))
                    except (ValueError, TypeError, AttributeError):
                        pass
                completed.add(origin or item.get('call_id'))
                continue
            raise MethodToolRouteError('Code Mode tool inventory is incomplete; '
                                      'method control routing cannot be verified')
        with self.lock:
            outstanding = self.pending.setdefault(session_key, set())
            outstanding.update(pending)
            outstanding.difference_update(completed)
            self.cells.setdefault(session_key, {}).update(cells)
            if observation is not None:
                observation.update(method_tool_route='direct_receipts_v1' if self.receipts else 'direct_namespace_v1',
                                   method_tool_pending_cells=sorted(outstanding),
                                   method_tool_incomplete_inventory=incomplete,
                                   method_tool_restored_inventory=restored_inventory)
        # New Codex represents tool declarations as additional_tools input items;
        # older hosts use the top-level tools field. Preserve either wire format.
        result = copy.deepcopy(body)
        catalogs = [item['tools'] for item in result['input']
                    if item.get('type') == 'additional_tools' and isinstance(item.get('tools'), list)]
        catalogs += [result['tools']] if isinstance(result.get('tools'), list) else []
        already_direct = any(tool.get('type') == 'namespace' and tool.get('name') == 'mcp__ctxpress'
                             for catalog in catalogs for tool in catalog)
        if not already_direct:
            if catalogs:
                catalogs[0].append(copy.deepcopy(self.namespace))
            else:
                result['tools'] = [copy.deepcopy(self.namespace)]
        result['input'].insert(0, dict(type='message', role='developer',
            content=[dict(type='input_text', text=self.instructions)]))
        return result

    def verify_receipts(self, body):
        """Strip validated host receipts before policy input and upstream forwarding."""
        from ctxpress.live.control_receipts import MARKER
        from ctxpress.live.rewrite import output_text
        calls = {i.get('call_id'): i for i in body['input']
                 if i.get('type') in ('function_call', 'custom_tool_call')}
        used = set()
        history = []
        try:
            for item in body['input']:
                if item.get('type') not in ('function_call_output', 'custom_tool_call_output'):
                    continue
                call = calls.get(item.get('call_id'), {})
                name = call.get('name', '')
                direct = (call.get('type') == 'function_call' and
                    ((call.get('namespace') == 'mcp__ctxpress' and name in self.names)
                     or name in self.nested_names))
                matches = list(MARKER.finditer(output_text(item)))
                if not direct:
                    if matches:
                        raise ValueError('method control receipt appeared outside a direct call/output pair')
                    continue
                if len(matches) != 1:
                    raise ValueError('direct method control output lacks one receipt')
                match = matches[0]
                identity = match.groups()
                if identity in used:
                    raise ValueError('duplicate method control receipt')
                used.add(identity)
                output = item.get('output')
                if isinstance(output, list):
                    marker_parts = [p for p in output if p.get('text') == match.group(0)]
                    if len(marker_parts) != 1:
                        raise ValueError('method control receipt is not a separate output part')
                    clean = [p for p in output if p is not marker_parts[0]]
                    texts = [p.get('text') for p in clean]
                    if len(texts) == 2 and re.fullmatch(r'Wall time: [^\n]+\nOutput:', texts[0] or ''):
                        text = texts[1]
                    elif len(texts) == 1:
                        text = texts[0]
                    else:
                        raise ValueError('unrecognized method control output envelope')
                elif isinstance(output, str) and output.endswith('\n'+match.group(0)):
                    clean = output[:-(len(match.group(0))+1)]
                    text = re.sub(r'^Wall time: [^\n]+\nOutput:\n?', '', clean)
                else:
                    raise ValueError('unrecognized method control receipt envelope')
                normalized = dict(call, name=name.removeprefix('mcp__ctxpress__'))
                with self.lock:
                    receipt = self.receipts.consume(*identity, normalized, text)
                from ctxpress.live.control_receipts import digest
                import json
                args = normalized.get('arguments')
                history.append(dict(receipt=receipt, name=normalized['name'],
                                    arguments=json.loads(args) if isinstance(args, str) else args, output=digest(text)))
                item['output'] = clean
            with self.lock:
                self.receipts.publish_history(history)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise MethodToolRouteError('invalid method control receipt: '+str(error)) from error
