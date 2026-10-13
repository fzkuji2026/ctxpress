"""ZipRL inference compression port; native tools replace its search-only loop."""
import json

from ctxpress.methods.base import Method
from ctxpress.methods.paper_utils import complete_items, tool_name
from ctxpress.methods._paper_sources.ziprl_parser import parse_model_output
from ctxpress.methods._paper_sources.ziprl_prompts import SYSTEM_PROMPT


class ZipRL(Method):
    source = 'arXiv 2605.28069'
    name = 'ZipRL (native inference adaptation)'
    instructions = (
        SYSTEM_PROMPT.split('## 【Compression Level Strategy】', 1)[1].split('## 【Output Format Example】')[0]
        + '\nUse zip_context to replace completed history with the summary you write at your chosen level. '
        'Keep the original task, evidence and source citations. Continue with the host tools. '
        'Call zip_context separately from other tools. Do not include hidden reasoning in the summary.')
    agent_tools = (dict(name='zip_context', description='Compress completed interaction history at relevance level 1–5.',
        annotations=dict(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
        inputSchema=dict(type='object', properties=dict(summary_level=dict(type='integer', minimum=1, maximum=5),
            summary=dict(type='string', minLength=1)), required=['summary_level', 'summary'], additionalProperties=False)),)

    def reset(self, sim):
        self.done = set()

    def call_tool(self, name, args):
        if tool_name({'name': name}) != 'zip_context':
            raise KeyError(name)
        if (not isinstance(args, dict) or set(args) != {'summary_level', 'summary'}
                or type(args.get('summary_level')) is not int or not 1 <= args['summary_level'] <= 5
                or not isinstance(args.get('summary'), str) or not args['summary'].strip()):
            return 'Error: supply summary_level 1–5 and nonempty summary.', True
        return 'recorded', False

    def _commit(self, sim, boundary, level, summary):
        before = sim.ctx[:sim.ctx.index(boundary)]
        selected = complete_items(sim, before)
        if not selected:
            return 'Error: no complete history to compress.'
        sim.summarize_segment(selected, text=summary, billed=False, model_written=True,
                              role='assistant', ziprl_summary=True, ziprl_level=level)
        return f'Compressed completed history at level {level}.'

    def step(self, sim, r):
        calls = {s.get('call_id'): s for s in sim.ctx if s['seg'] == 'call'}
        for item in list(sim.ctx):
            if item['id'] in self.done or item not in sim.ctx or item.get('ziprl_summary'):
                continue
            self.done.add(item['id'])
            if item['seg'] == 'out':
                call = calls.get(item.get('call_id'))
                if not call or tool_name(call) != 'zip_context':
                    continue
                try:
                    args = json.loads(call.get('args') or '{}')
                except (TypeError, ValueError):
                    args = None
                reply, error = self.call_tool('zip_context', args)
                if not error:
                    reply = self._commit(sim, call, args['summary_level'], args['summary'])
                sim.keep_text(item, reply)
            elif item['seg'] == 'msg' and item.get('role') == 'assistant':
                # Accept the author's visible structured summary output too.
                parsed = parse_model_output(item.get('text', ''))
                if parsed.summary and parsed.summary_level:
                    self._commit(sim, item, parsed.summary_level, parsed.summary)
