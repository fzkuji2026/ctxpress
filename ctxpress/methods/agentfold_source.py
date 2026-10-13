"""Pinned AgentFold step replacement/formatting through shared host tools.

Alibaba-NLP/DeepResearch@f72f75d8c3eb842f2bbbab096a12206ff66e270f,
WebAgent/AgentFold/infer.py (Apache-2.0). Host tools replace the author's
four-block output protocol; trained policy and full prompt rebuilding differ.
"""
import json
import re

from ctxpress.methods.agentfold_tools import AgentFoldTools


def extract_tags(text, tag):
    match = re.search(r'<{TAG}>(.*?)</{TAG}>'.format(TAG=tag), text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.split('<answer>')[-1].strip() if tag == 'answer' else ''


def compression_action(text):
    """Read the author's action block; never inspect hidden host reasoning."""
    action = text.split('</think>')[-1]
    if '<answer>' in action and '</answer>' in action:
        return None  # Author final answers bypass compression.
    raw = extract_tags(action, 'compress')
    if not raw:
        return None
    info = json.loads(raw)
    span = info.get('compress_range') if isinstance(info, dict) else None
    if (not isinstance(span, list) or not span or any(type(i) is not int or i < 0 for i in span)
            or span[0] > span[-1] or not isinstance(info.get('compress_text'), str)
            or not info['compress_text'].strip()):
        raise ValueError('invalid AgentFold compression block')
    # Author steps start at 0; native host steps start at 1.
    return dict(start_step=span[0]+1, end_step=span[-1]+1, summary=info['compress_text'])


def update_and_sort_steps(step_list, compression_info, latest_step_id):
    steps_to_compress = compression_info['compress_range']
    start_step, end_step = steps_to_compress[0], steps_to_compress[-1]
    updated = [s for s in step_list if not start_step <= s['start'] <= end_step]
    updated.append(dict(start=start_step, end=end_step, content=compression_info['compress_text']))
    return sorted(updated, key=lambda s: s['start'])


def format_previous_steps(step_list):
    step_list.sort(key=lambda s: s['start'])
    max_id = max(s['start'] for s in step_list)
    result = ''
    for s in step_list:
        start, end = s['start'], s['end']
        if start == end:
            header = f'[Step {start}]' if start == max_id else f'[Compressed Step {start}]'
        else:
            header = f'[Compressed Step {start} to {end}]'
        result += f"**{header}**\n{s['content']}\n\n"
    return result


class AgentFoldSource(AgentFoldTools):
    name = 'AgentFold (author step formatter, host folding tools)'
    instructions = AgentFoldTools.instructions + (
        ' Compressed steps use the author step-header format. Fold complete step ranges; '
        'the host protects instructions and complete tool pairs before applying the fold. '
        'Alternatively emit a visible <compress>{"compress_range":[0,1],"compress_text":"findings"}</compress> '
        'block before your native tool call. This author format uses zero-based step indices '
        '(host step 1 is author step 0). Do not use both compression forms for the same action.')

    def reset(self, sim):
        super().reset(sim)
        self.blocks_done = set()

    def step(self, sim, r):
        super().step(sim, r)
        for item in list(sim.ctx):
            if (item['seg'] != 'msg' or item.get('role') != 'assistant' or
                    item.get('protected') or item.get('has_media') or item['id'] in self.blocks_done):
                continue
            self.blocks_done.add(item['id'])
            try:
                args = compression_action(sim.current_text(item) or '')
            except (ValueError, TypeError):
                # Malformed author blocks retain history, never partially fold it.
                continue
            if args is not None:
                self._fold(sim, item, args)

    def _fold_text(self, lo, hi, summary, items=()):
        # The host validates and selects the complete interval first. The author
        # operation produces the replacement step; other native items stay in place.
        spans = {}
        for item in items:
            span = self._span(item)
            spans.setdefault(span, []).append(item.get('kept_text') or item.get('text') or '')
        original = [dict(start=a, end=b, content='\n'.join(parts)) for (a, b), parts in spans.items()]
        steps = update_and_sort_steps(original, dict(compress_range=[lo, hi], compress_text=summary), hi)
        return format_previous_steps(steps)
