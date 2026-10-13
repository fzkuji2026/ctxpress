"""Context-Folding branch/return mechanism, adapted to the shared host stream.

Source: MiaoLu3/Context_Folding, 81626e97bb5f4eaa79edca045528829ce8850f5b,
fold_agent_loop.py. This is not FoldGRPO or a reproduction of trained results.
The native host executes a branch inline, then its complete intermediate history
is removed and the branch result replaces the opening tool observation. Budgets
are checked between host requests; they cannot interrupt an in-flight host tool.
None disables each method-internal branch-count, turn-count, or elapsed-time cap.
The separate branch_tokens limit can still request a return and fold on the next
turn if the agent does not return.
"""
from __future__ import annotations

import json
import re
import time

from ctxpress.methods.base import Method


def clean_response(response):
    if '<function=return>' in response:
        return response.split('<function=return>')[-1]
    return re.split(r'<\[[^\]]+\]>', response)[-1]


def tool_name(name):
    return (name or '').rsplit('__', 1)[-1].rsplit('.', 1)[-1]


class ContextFoldingTools(Method):
    name = 'Context-Folding (host branch/return adaptation)'
    source = 'arXiv 2510.11967'
    instructions = (
        'Use context_branch(description, prompt) to start a focused subtask. Work on '
        'that subtask using the normal host tools, then call context_return(message) '
        'with findings, evidence and pending work. Its intermediate history will be '
        'folded into your return message. Branches inherit the current context and '
        'cannot nest. Invoke each control tool alone, never in parallel with other '
        'tools. This adapts Context-Folding with the host model, not FoldGRPO weights.')
    framework = dict(L1='无', L2='分支子任务完成后折叠为返回摘要', L3='无',
                     cross='保留父上下文，拒绝跨用户修正或未完成工具折叠',
                     memory='无', decider='宿主 Agent，未加载 FoldGRPO 策略')
    agent_tools = (
        dict(name='context_branch', description='Begin a focused subtask inheriting the current context.',
             annotations=dict(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
             inputSchema=dict(type='object', properties=dict(description=dict(type='string', minLength=1),
                              prompt=dict(type='string', minLength=1)),
                              required=['description', 'prompt'], additionalProperties=False)),
        dict(name='context_return', description='Finish the current subtask and fold its intermediate history into this message.',
             annotations=dict(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
             inputSchema=dict(type='object', properties=dict(message=dict(type='string', minLength=1)),
                              required=['message'], additionalProperties=False)),
    )

    def __init__(self, max_branches=5, branch_max_turns=64, branch_timeout=5400, branch_tokens=None):
        if max_branches is not None and (type(max_branches) is not int or max_branches < 1):
            raise ValueError('max_branches must be a positive integer or None')
        self.max_branches = max_branches
        if branch_max_turns is not None and (type(branch_max_turns) is not int or branch_max_turns < 1):
            raise ValueError('branch_max_turns must be a positive integer or None')
        if branch_timeout is not None and (type(branch_timeout) not in (int, float) or not 0 < branch_timeout < float('inf')):
            raise ValueError('branch_timeout must be positive and finite or None')
        if branch_tokens is not None and (type(branch_tokens) is not int or branch_tokens < 1):
            raise ValueError('branch_tokens must be a positive integer or None')
        self.branch_max_turns, self.branch_timeout, self.branch_tokens = branch_max_turns, branch_timeout, branch_tokens

    def reset(self, sim):
        self.done = set()
        self.branch = None
        self.started = 0
        self.forced_turn = None

    def call_tool(self, name, args):
        name = tool_name(name)
        if name not in ('context_branch', 'context_return'):
            raise KeyError(name)
        fields = {'description', 'prompt'} if name == 'context_branch' else {'message'}
        if (not isinstance(args, dict) or set(args) != fields or
                any(not isinstance(args[k], str) or not args[k].strip() for k in fields)):
            return 'Error: provide the required nonempty string arguments.', True
        # Authoritative state lives in the proxy, not the separate MCP process.
        return 'Recorded; the context controller will validate and apply this action.', False

    def _return(self, sim, output, args):
        return self._finish(sim, output, 'Branch has finished its task, the returned message is:\n\n' + args['message'])

    def _finish(self, sim, output, message):
        if self.branch is None:
            return 'Error: no active branch.'
        positions = {s['id']: i for i, s in enumerate(sim.ctx)}
        if self.branch not in positions:
            self.branch = None
            return 'Error: branch boundary is unavailable; history was not folded.'
        boundary = sim.ctx[positions[self.branch]]
        selected = sim.ctx[positions[self.branch] + 1:positions[output['id']] + 1]
        if any(s.get('protected') or s.get('has_media') or
               s.get('role') in ('system', 'developer', 'user') for s in selected):
            return 'Error: branch contains protected history; nothing was folded.'
        chosen = {s['id'] for s in selected}
        calls = {s.get('call_id') for s in selected if s['seg'] == 'call'}
        results = {s.get('call_id') for s in selected if s['seg'] == 'out'}
        if calls != results or any(s.get('call_id') in calls and s['id'] not in chosen for s in sim.ctx):
            return 'Error: branch splits an unfinished or crossing tool call; nothing was folded.'
        if not sim.keep_text(boundary, message):
            return 'Error: branch boundary is protected; nothing was folded.'
        sim.delete(selected)
        self.branch = None
        self.forced_turn = None
        return None

    def _check_branch_budget(self, sim):
        if self.branch is None:
            return
        index = next((i for i, s in enumerate(sim.ctx) if s['id'] == self.branch), None)
        if index is None:
            self.branch = None
            return
        work = sim.ctx[index + 1:]
        if not work:
            return
        turn = max(s.get('turn', 0) for s in work)
        age = turn - self.branch_turn
        expired = ((self.branch_max_turns is not None and age >= self.branch_max_turns) or
                   (self.branch_timeout is not None and time.monotonic() - self.branch_started >= self.branch_timeout))
        if expired or (self.forced_turn is not None and turn > self.forced_turn):
            last_turn = [s for s in work if s.get('turn', 0) == turn]
            latest = '\n'.join(sim.current_text(s) or '' for s in last_turn
                               if s['seg'] == 'msg' and s.get('role') == 'assistant')
            if not latest:
                latest = '\n'.join(json.dumps(dict(name=s.get('name'), arguments=s.get('args')), ensure_ascii=False)
                                   for s in last_turn if s['seg'] == 'call')
            reply = 'Branch has finished its task. The last message was:\n\n' + clean_response(latest)
            # Complete-pair/protected-history checks apply to fallback too.
            self._finish(sim, work[-1], reply)
        elif self.branch_tokens is not None and self.forced_turn is None and sum(sim.seg_size(s) for s in work) > self.branch_tokens:
            output = next((s for s in reversed(work) if s['seg'] == 'out' and not s.get('protected') and not s.get('has_media')), None)
            if output is not None:
                sim.keep_text(output, (sim.current_text(output) or '') + '\n\nThe context limit has been exceeded for the branch. Please finish the sub task directly and clearly state the progress made and the pending jobs of the sub task. Only summarize the sub task progress, using the context_return tool.')
                self.forced_turn = turn

    def step(self, sim, r):
        if not hasattr(self, 'done'):
            self.reset(sim)
        calls = {s.get('call_id'): s for s in sim.ctx if s['seg'] == 'call'}
        for output in list(sim.ctx):
            if output['seg'] != 'out' or output['id'] in self.done or output not in sim.ctx:
                continue
            call = calls.get(output.get('call_id'))
            name = tool_name(call.get('name')) if call else ''
            if name not in ('context_branch', 'context_return'):
                continue
            self.done.add(output['id'])
            try:
                args = json.loads(call.get('args') or '{}')
            except (TypeError, ValueError):
                args = None
            reply, error = self.call_tool(name, args)
            if not error and name == 'context_branch':
                if self.branch is not None:
                    reply = 'Error: nested branches are not allowed; return from the active branch first.'
                elif self.max_branches is not None and self.started >= self.max_branches:
                    reply = f'You have reached the limit of {self.max_branches} branch calls. Continue working independently.'
                else:
                    reply = (f"Branch mode: {args['description']}\n{args['prompt']}\n"
                             'Complete only this subtask, then use context_return with your findings.')
                    if sim.keep_text(output, reply):
                        self.branch = output['id']
                        self.branch_turn = output.get('turn', 0)
                        self.branch_started = time.monotonic()
                        self.forced_turn = None
                        self.started += 1
                    continue
            elif not error:
                reply = self._return(sim, output, args)
            if reply is not None:
                sim.keep_text(output, reply)
        self._check_branch_budget(sim)
