"""Synthetic method checks. No model/network calls; not benchmark evidence."""
from collections import Counter
import copy
import json
from tempfile import TemporaryDirectory
from contextlib import ExitStack

from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import build

# Only implementations whose IO is supplied by the harness, never remote pruners.
SUPPORTED = {'TACO', 'CoMem', 'LongSeeker', 'ZipRL', 'NoCompaction', 'CodexAutoCompact', 'ComplexityTrap', 'ComplexityTrapSummary',
             'ComplexityTrapHybrid', 'KeepLastTokens', 'Pichay', 'ClawVM', 'ARC',
             'AgentDiet', 'CWL', 'DTOC', 'SlidingWindow', 'CliffCompaction', 'ACM',
             'AgentFoldTools', 'TokenPilotLifecycle', 'ACONSource', 'ContextFoldingTools', 'ReSumSource', 'AgentFoldSource', 'FoldActSource'}


class FakeSummary:
    def summarize_prompt(self, system, user, purpose='history', **kwargs):
        if purpose == 'reflect' and 'selected_rule_ids' in user:
            from ctxpress.methods._paper_sources.taco.seed_rules import get_seed_rules
            return json.dumps({'selected_rule_ids': [r.rule_id for r in get_seed_rules()]})
        if purpose == 'lifecycle':
            data = json.loads(user)
            return json.dumps(dict(baseVersion=data['baseVersion'], taskUpdates=[dict(
                taskId='synthetic-task', objective='synthetic fixture', lifecycle='evictable',
                coveredTurnAbsIds=list(dict.fromkeys(s['turn'] for s in data['delta'])),
                completionEvidence=['synthetic completion'], unresolvedQuestions=[])]))
        return 'Synthetic summary: preserve the task, earlier findings, and continue using workspace tools.'

    def summarize(self, items, **kwargs):
        return self.summarize_prompt('', '')

    def summarize_with_guidance(self, items, **kwargs):
        return self.summarize_prompt('', '')


def check(name, args=None, turns=60, output_chars=4096):
    if name not in SUPPORTED:
        raise ValueError('no network-free mechanism fixture for this method; external services are not called')
    if type(turns) is not int or not 2 <= turns <= 500 or type(output_chars) is not int or not 64 <= output_chars <= 100000:
        raise ValueError('turns must be 2..500 and output_chars 64..100000')
    catalog = dict(type='additional_tools', tools=[dict(type='function', name='read', parameters=dict(type='object'))])
    inputs = [dict(type='message', role='system', content='Keep these instructions.'),
              dict(type='message', role='user', content='Find and fix the issue.'), catalog]
    samples = []
    with TemporaryDirectory(prefix='ctxpress-method-check-') as store, ExitStack() as resources:
        rewriter = Rewriter(lambda: build({'class': name, 'args': args or {}}),
                            store_dir=store, summarizer=FakeSummary())
        resources.callback(rewriter.close)
        def send(session, history, label):
            body = dict(model='synthetic', input=copy.deepcopy(history))
            before = copy.deepcopy(body)
            output, info = rewriter.rewrite_body(body, session)
            if before != body:
                raise ValueError('rewriter mutated the supplied history')
            samples.append(dict(scenario=label, **info))
            return output
        for index in range(turns):
            call_name, arguments = 'read', '{}'
            if name == 'CWL' and index == 0:
                call_name, arguments = 'delimiter', json.dumps(dict(action='start', name='fixture', type='expl'))
            if name == 'CWL' and index == turns - 1:
                call_name, arguments = 'delimiter', json.dumps(dict(action='end', description='Synthetic findings.'))
            if name == 'FoldActSource' and index == turns // 2:
                inputs.append(dict(role='assistant', content='<information_summary>Synthetic findings.</information_summary>'))
            if name == 'ContextFoldingTools' and index == 0:
                call_name = 'context_branch'
                arguments = json.dumps(dict(description='synthetic subtask', prompt='inspect evidence'))
            if name == 'ContextFoldingTools' and index == turns - 1:
                call_name = 'context_return'
                arguments = json.dumps(dict(message='Synthetic branch findings.'))
            if name in ('AgentFoldTools', 'AgentFoldSource') and index == turns - 2:
                call_name = 'fold_context'
                arguments = json.dumps(dict(start_step=1, end_step=index, summary='Synthetic completed steps.'))
            if name == 'ACM' and index in (turns // 2, turns - 2):
                call_name = 'manage_context'
            if name == 'ACM' and index == turns - 1:
                call_name, arguments = 'query_memory', '{"summary_id":1,"query":"earlier findings"}'
            if name == 'DTOC' and index == turns - 2:
                call_name, arguments = 'manage_context', '{"enable":[],"disable":["tk_001"]}'
            if name == 'TACO':
                call_name, arguments = 'exec_command', '{"cmd":"git fetch"}'
            if name == 'LongSeeker' and index == turns - 2:
                call_name, arguments = 'longseeker_compress', json.dumps(dict(start=1, end=index, summary='Synthetic findings.'))
            if name == 'ZipRL' and index == turns - 2:
                call_name, arguments = 'zip_context', json.dumps(dict(summary_level=3, summary='Synthetic findings.'))
            fixture_output = ('remote: Counting objects: 100% (5/5)\n' * (output_chars // 38) if name == 'TACO'
                              else 'recorded' if call_name != 'read' else 'data ' * (output_chars // 5))
            inputs += [dict(type='function_call' , call_id=f'c{index}', name=call_name, arguments=arguments),
                       dict(type='function_call_output', call_id=f'c{index}', output=fixture_output)]
            output = send('growing', inputs, 'incremental')
            if not any(i.get('call_id') == f'c{index}' for i in output['input']):
                # Retaining the last output isn't every method's policy; report it, don't redefine the method.
                samples[-1]['latest_pair_retained'] = False
            else:
                samples[-1]['latest_pair_retained'] = True
        send('resumed', inputs, 'frozen_history_first_request')
        send('growing', inputs[:3] + inputs[-2:], 'host_history_revision')
    ops = Counter()
    for row in samples:
        ops.update(row.get('operations') or {})
    return dict(schema='ctxpress.method.check', version=1, method=name, arguments=args or {},
        fixture=dict(turns=turns, output_chars=output_chars), contract_valid=True,
        triggered=any(n > 0 for n in ops.values()), operations=dict(ops), samples=samples,
        limitations=['Synthetic deterministic summaries, not real model output or quality evidence.',
                     'The fixture validates render contracts and history transitions, not execution by a real CLI.',
                     'No trigger means not exercised by this fixture; native host compaction is not simulated.'])
