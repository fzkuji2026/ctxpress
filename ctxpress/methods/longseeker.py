"""LongSeeker's five context operations projected onto native host histories.

Host turn IDs are stable (not reused after rollback); native tool pairs and
user corrections are protected. Core block operations are the author source.
"""
import copy
import json

from ctxpress.methods.base import Method
from ctxpress.methods.paper_utils import complete_items, tool_name
from ctxpress.methods._paper_sources.longseeker.context import ContextManager
from ctxpress.methods._paper_sources.longseeker import compress, delete, rollback, snippet, skip


def _tools():
    result = []
    for module in (compress, delete, rollback, snippet, skip):
        for entry in module.TOOL_SCHEMA:
            f = entry['function']
            result.append(dict(name='longseeker_' + f['name'], description=f['description'],
                inputSchema=copy.deepcopy(f['parameters']),
                annotations=dict(readOnlyHint=True, destructiveHint=False, openWorldHint=False)))
    return tuple(result)


class LongSeeker(Method):
    source = 'arXiv 2605.05191'
    name = 'LongSeeker (native context operations)'
    agent_tools = _tools()
    instructions = ('Outputs have LongSeeker step IDs. Use longseeker_compress, longseeker_snippet, '
        'longseeker_delete, longseeker_rollback or longseeker_skip to manage completed context. '
        'Write factual summaries preserving accepted and excluded candidates and evidence. '
        'Rollback edits context only; it does not undo files or external actions. IDs stay stable. '
        'Call these tools separately from workspace tools. User corrections and incomplete calls are protected.')

    def reset(self, sim):
        self.done = set()

    @staticmethod
    def output_text(item, original):
        return f"[LongSeeker step {item['turn']}]\n{original}"

    def call_tool(self, name, args):
        name = tool_name({'name': name})
        spec = next((t for t in self.agent_tools if t['name'] == name), None)
        if spec is None:
            raise KeyError(name)
        schema = spec['inputSchema']
        if not isinstance(args, dict) or any(k not in args for k in schema.get('required', [])):
            return 'Error: missing context operation arguments.', True
        if set(args) - set(schema.get('properties', {})):
            return 'Error: unexpected context operation arguments.', True
        for key, value in args.items():
            kind = schema['properties'][key]['type']
            if (kind == 'integer' and (type(value) is not int or value < 1)
                    or kind == 'string' and (not isinstance(value, str) or not value.strip())):
                return 'Error: invalid context operation arguments.', True
        if name.endswith('_compress') and args['start'] > args['end']:
            return 'Error: start must not exceed end.', True
        return 'recorded', False

    def _apply(self, sim, call, name, args):
        before = sim.ctx[:sim.ctx.index(call)]
        groups = {}
        for s in before:
            if s.get("longseeker_note"):
                continue
            span = tuple(s.get('longseeker_span', (s.get('turn', 0), s.get('turn', 0))))
            groups.setdefault(span, []).append(s)
        eligible = {s['id'] for s in complete_items(sim, before)}
        manager = ContextManager()
        blocked = set()
        for span, items in groups.items():
            if span[0] <= 0 or any(s['id'] not in eligible for s in items):
                blocked.add(span)
                continue
            if len(items) == 1 and items[0].get('longseeker_span'):
                content = items[0].get('longseeker_payload', items[0].get('text', ''))
            else:
                motivation = '\n'.join(sim.current_text(s) or '' for s in items if s['seg'] == 'msg')
                actions = '\n'.join(s.get('text', '') for s in items if s['seg'] == 'call')
                outputs = '\n'.join(sim.current_text(s) or '' for s in items if s['seg'] == 'out')
                content = f'**Motivation**\n{motivation}\n\n**Tool call**\n{actions}\n\n**Tool response**\n{outputs}'
            manager.append(span, content)
        operation = name.removeprefix('longseeker_')
        if operation == 'skip':
            return 'Context unchanged.'
        lo = args.get('start', args.get('step', args.get('step_id', 0)))
        hi = args.get('end', lo) if operation != 'rollback' else call.get('turn', 0)
        if hi >= call.get('turn', 0) and operation != 'rollback':
            return 'Error: cannot edit the current or future step.'
        if any(a <= hi and b >= lo for a, b in blocked):
            return 'Error: operation crosses protected or unfinished history.'
        old = copy.deepcopy(manager.step_dict)
        if operation == 'compress':
            compress.compress(manager, args)
        elif operation == 'delete':
            delete.delete(manager, args)
        elif operation == 'rollback':
            rollback.rollback(manager, args)
        elif operation == 'snippet':
            snippet.snippet(manager, [args])
        if old == manager.step_dict:
            return 'No context change: range unavailable or operation not applicable.'
        if operation == 'compress':
            changed = (args['start'], args['end'])
            selected = [s for span, items in groups.items() if span in old and span not in manager.step_dict
                        for s in items]
            # Compressing one existing block also replaces it.
            if changed in old:
                selected += groups[changed]
            payload = manager.step_dict[changed]['content']
            sim.summarize_segment(selected, text=f'[LongSeeker steps {changed[0]}-{changed[1]}]\n{payload}',
                billed=False, model_written=True, role='assistant', longseeker_span=changed,
                longseeker_payload=payload)
        elif operation == 'rollback':
            selected = [s for span, items in groups.items() if span in old and span not in manager.step_dict for s in items]
            sim.summarize_segment(selected, text=f"[LongSeeker rollback to {lo}] {args['reason']}",
                billed=False, model_written=True, role='assistant', longseeker_note=True)
        else:
            for span, value in manager.step_dict.items():
                if value == old.get(span):
                    continue
                # Native call/response envelopes remain; only the observation is replaced.
                body = value['content'].split('**Tool response**\n', 1)[-1]
                outputs = [s for s in groups[span] if s['seg'] == 'out' or s.get('longseeker_span')]
                for s in outputs:
                    if s.get('longseeker_span'):
                        s['text'] = s['longseeker_payload'] = body
                for i, output in enumerate(outputs):
                    sim.keep_text(output, body if i == 0 else '[Included in preceding LongSeeker observation]')
        return f'Applied {operation} to completed context.'

    def step(self, sim, r):
        calls = {s.get('call_id'): s for s in sim.ctx if s['seg'] == 'call'}
        names = {t['name'] for t in self.agent_tools}
        for output in list(sim.ctx):
            if output['seg'] != 'out' or output['id'] in self.done:
                continue
            self.done.add(output['id'])
            call = calls.get(output.get('call_id'))
            if not call or tool_name(call) not in names:
                continue
            name = tool_name(call)
            try:
                args = json.loads(call.get('args') or '{}')
            except (ValueError, TypeError):
                args = None
            reply, error = self.call_tool(name, args)
            if not error:
                reply = self._apply(sim, call, name, args)
            sim.keep_text(output, reply)
