"""TACO author planner/evolver/filter adapted to the common host and billing.

The default pool is task-local to keep evaluation independent. An explicit
rules_file supplies a frozen prior pool. Terminal screen cleaning and Harbor's
other filters are not applied to arbitrary host messages.
"""
import json
import re

from ctxpress.methods.base import Method
from ctxpress.methods.paper_utils import tool_name, positive_int
from ctxpress.methods._paper_sources.taco.models import CompressionRule
from ctxpress.methods._paper_sources.taco.seed_rules import get_seed_rules
from ctxpress.methods._paper_sources.taco.planner import CompressionPlanner
from ctxpress.methods._paper_sources.taco.evolver import RuleEvolver
from ctxpress.methods._paper_sources.taco.feedback import FeedbackCollector
from ctxpress.methods._paper_sources.taco.dynamic_filter import DynamicCompressionFilter
from ctxpress.methods._paper_sources.taco.filter_types import FilterContext
from ctxpress.methods._paper_sources.taco.errors import has_error


class _MeteredClient:
    def __init__(self, sim, model):
        self.sim, self.model = sim, model

    def chat_with_usage(self, user_content):
        result = self.sim.model_text('', user_content, purpose='reflect', model=self.model)
        if result is None:
            raise RuntimeError('TACO auxiliary model failed')
        # Author-local counters are unused. The shared transport records real
        # usage and missing usage; this empty dict is never used as a bill.
        return result, {}


class TACO(Method):
    context_role = 'local'  # edits tool outputs only; the host keeps its native compaction
    source = 'arXiv 2604.19572'
    name = 'TACO (author rule compression)'
    requires_summary = True
    instructions = ('TACO may compress successful terminal outputs using learned filtering rules. '
        'Compressed outputs carry an output_id. If needed information is missing, call taco_feedback '
        'with that ID and feedback="need_full_output" to restore it and revise the rule. '
        'Do not report complaints merely because an output is shorter.')
    agent_tools = (dict(name='taco_feedback', description='Restore a TACO-compressed output and report missing information.',
        annotations=dict(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
        inputSchema=dict(type='object', properties=dict(output_id=dict(type='string'),
            feedback=dict(type='string', enum=['need_full_output'])),
            required=['output_id', 'feedback'], additionalProperties=False)),)

    def __init__(self, summary_model=None, freeze_rules=False, uncovered_threshold=3000,
                 task_category='software-engineering', rules_file=None):
        if type(freeze_rules) is not bool:
            raise ValueError('freeze_rules must be boolean')
        self.summary_model, self.freeze_rules = summary_model, freeze_rules
        self.uncovered_threshold = positive_int('uncovered_threshold', uncovered_threshold)
        self.task_category, self.rules_file = task_category, rules_file

    def reset(self, sim):
        self.done, self.records = set(), {}
        self.planned = False
        self.rules = get_seed_rules()
        if self.rules_file:
            with open(self.rules_file, encoding='utf-8') as f:
                values = json.load(f)
            self.rules = [CompressionRule(**v) for v in values]

    @staticmethod
    def output_text(item, original):
        if item.get('taco_compressed'):
            return f"[TACO output_id={item['id']}; use taco_feedback if details are missing]\n{original}"
        return original

    def call_tool(self, name, args):
        if tool_name({'name': name}) != 'taco_feedback':
            raise KeyError(name)
        if (not isinstance(args, dict) or set(args) != {'output_id', 'feedback'}
                or not isinstance(args.get('output_id'), str) or not args['output_id']
                or args.get('feedback') != 'need_full_output'):
            return 'Error: supply output_id and feedback=need_full_output.', True
        return 'recorded', False

    def step(self, sim, r):
        instruction = '\n'.join(s.get('text', '') for s in sim.sent() if s.get('role') == 'user')
        client = _MeteredClient(sim, self.summary_model)
        evolver = RuleEvolver(client)
        if not self.planned:
            self.planned = True
            if not self.freeze_rules:
                plan = CompressionPlanner(client).generate_plan(instruction, self.task_category, '', self.rules)
                self.rules = [s for s in self.rules if s.rule_id in plan.selected_rule_ids] + plan.all_active_rules()
        calls = {s.get('call_id'): s for s in sim.ctx if s['seg'] == 'call'}
        for item in list(sim.ctx):
            if item['seg'] != 'out' or item['id'] in self.done:
                continue
            self.done.add(item['id'])
            call = calls.get(item.get('call_id'))
            if not call:
                continue
            if tool_name(call) == 'taco_feedback':
                try:
                    args = json.loads(call.get('args') or '{}')
                except (TypeError, ValueError):
                    args = None
                reply, error = self.call_tool('taco_feedback', args)
                record = self.records.get(args['output_id']) if not error else None
                target = next((s for s in sim.ctx if not error and str(s['id']) == args['output_id']), None)
                if record and target is not None:
                    collector = FeedbackCollector()
                    collector.record_compression(**record)
                    signal = collector.detect_complaint(r, '', compression_feedback='need_full_output')
                    sim.keep_text(target, record['raw_output'])
                    target['taco_compressed'] = False
                    if signal and not self.freeze_rules:
                        for rule in list(self.rules):
                            if rule.rule_id in signal.rules_applied:
                                replacement = evolver.spawn_replacement(rule, signal)
                                if replacement:
                                    self.rules.append(replacement)
                    reply = 'Restored original output; compression feedback recorded.'
                elif not error:
                    reply = 'Error: output unavailable in this context.'
                sim.keep_text(item, reply)
                continue
            if (item.get('protected') or item.get('has_media') or call.get('protected')
                    or call.get('has_media')):
                continue
            raw = sim.current_text(item) or item.get('text', '')
            terminal = self._terminal(call, raw)
            if terminal is None or item.get('is_error'):
                continue
            command, payload, wrap = terminal
            if has_error(payload) or re.search(r'Process exited with code (?!0\b)\d+', payload):
                continue
            context = FilterContext(command, False, 'idle', None, len(raw))
            text, applied = payload, []
            for rule in sorted(self.rules, key=lambda x: x.priority):
                filt = DynamicCompressionFilter(rule)
                if not filt.should_skip(context):
                    previous = rule.times_applied
                    text = filt.filter(text, context)
                    if rule.times_applied > previous:
                        applied.append(rule.rule_id)
            if text != payload and len(text) < len(payload):
                item['taco_compressed'] = True
                sim.keep_text(item, self.output_text(item, wrap(text)))
            self.records[str(item['id'])] = dict(episode=r, command=command, raw_output=raw,
                compressed_output=wrap(text), rules_applied=applied)
            if not self.freeze_rules:
                signal = FeedbackCollector().detect_uncovered(r, command, raw, applied,
                                                              threshold=self.uncovered_threshold)
                if signal:
                    rule = evolver.spawn_new(signal, instruction)
                    if rule:
                        self.rules.append(rule)
                elif applied:
                    evolver.boost_confidence(self.rules, applied)

    @staticmethod
    def _terminal(call, raw):
        """Only unwrap a single, completely recorded synchronous shell result."""
        name = tool_name(call).lower()
        wrap = lambda text: text
        payload = raw
        if name == 'exec':
            nested = call.get('executed_tool_calls')
            if call.get('executed_tool_calls_complete') is not True or not isinstance(nested, list) or len(nested) != 1:
                return None
            call = nested[0]
            name = tool_name(call).lower()
            lines = raw.splitlines(keepends=True)
            results = []
            for index, line in enumerate(lines):
                try:
                    result = json.loads(line)
                except ValueError:
                    continue
                if isinstance(result, dict) and 'output' in result:
                    results.append((index, result))
            if len(results) != 1:
                return None
            index, result = results[0]
            if result.get('exit_code') != 0 or result.get('session_id') or not isinstance(result['output'], str):
                return None
            payload = result['output']
            def wrap(text):
                rewritten = list(lines)
                rewritten[index] = json.dumps(dict(result, output=text), ensure_ascii=False) + ('\n' if lines[index].endswith('\n') else '')
                return ''.join(rewritten)
        if name not in {'bash', 'shell', 'exec_command', 'run_shell_command', 'terminal'}:
            return None
        args = call.get('arguments', call.get('args', '{}'))
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except (ValueError, TypeError):
            return None
        if not isinstance(args, dict):
            return None
        command = args.get('cmd', args.get('command'))
        if not isinstance(command, str) or not command.strip():
            return None
        return command, payload, wrap
