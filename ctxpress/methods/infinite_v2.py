"""V2: capacity-feasible, recoverable tool-output summaries; v1 remains reproducible.

Reuse probability prices recovery; it is NOT a calibrated probability of task failure.
Under pressure, reviewed archived summaries can incur cost to preserve headroom.
No guarantee is possible when protected content alone fills the host capacity.
"""
import json
import math
from collections import Counter
from ctxpress.core import infinite_policy
from ctxpress.core import tokens
from ctxpress.core.trace import content_type
from ctxpress.methods.infinite_context import InfiniteContext, PROMPT


class InfiniteContextV2(InfiniteContext):
    def reset(self, sim):
        super().reset(sim)
        self.growth = []
        self.previous_after = None
        self.rejections = Counter()
        self.invalid_proposals = set()

    def _size(self, sim):
        return sim.request_tokens()

    def _horizon(self, r):
        lengths = self.policy['statistics']['lengths']
        # A pooled geometric tail regularizes finite empirical support. This is
        # extrapolation, explicitly logged, not evidence that a task must continue.
        tail = sum(lengths)/max(1, len(lengths)+1)
        remaining = [n-r for n in lengths if n > r]
        return max(1., (sum(remaining)+tail)/(len(remaining)+1)*self.cfg['horizon_scale'])

    def _limits(self, sim):
        cap = self.policy['constraints']['capacity']
        samples = sorted(self.growth[-32:])
        observed = samples[max(0, math.ceil(.9*len(samples))-1)] if samples else 0
        reserve = max(observed, self.policy['observations']['extra_output_tokens'])
        trigger = max(1., min(cap*self.cfg['trigger_ratio'], cap-reserve))
        target = max(1., min(cap*self.cfg['target_ratio'], trigger-reserve))
        return cap, trigger, target, reserve

    def _candidate(self, sim, item, summary, op, r):
        text = self._text(sim, item, summary)
        if text is None:
            self.rejections['archive_unavailable'] += 1
            return None
        suffix = f'\n[Full output stored at {self._source(sim, item)}.]'
        size = tokens.count(text + suffix) + sim.overhead(item)
        saved = sim.seg_size(item)-size
        if saved <= 0 or size >= sim.seg_size(item):
            self.rejections['no_size_reduction'] += 1
            return None
        risk, life, coverage = self._risk(item, r, op)
        unit = (sim.CACHED*self._size(sim)+sim.WRITE*item['size']+
                sim.OUT*self.policy['observations']['extra_output_tokens'])
        warm = r-max(item['born'], item['last']) <= sim.P.get('w_warm')
        exploration = (sim.e if warm else sim.ec)*(1-coverage)
        recovery = (risk+exploration)*max(unit, self.policy['observations']['repeat_request_cost'])
        gain = sim.CACHED*saved*self._horizon(r)-recovery
        return dict(item=item, text=text, size=size, saved=saved, risk=risk,
                    recovery=recovery, gain=gain, op=op,
                    version=infinite_policy.digest(item.get('text', '')))

    def _ask(self, sim, body):
        key = infinite_policy.digest(body)
        if key in self.invalid_proposals:
            self.rejections['cached_invalid_proposal'] += 1
            return None, 0.
        if key in self.proposals:
            return self.proposals[key], 0.
        prompt = json.dumps(body, ensure_ascii=False)
        agent = self.policy['agent']
        instructions = PROMPT+'\nEach summary MUST be at most summary_tokens tokens. If essential details cannot fit, set retention=persistent and provide a short reason as summary.'
        answer = sim.model_text(instructions, prompt, purpose='infinite_context_compression', model=self.agent_model)
        sim.M['infinite_agent_calls'] += 1
        # Cache only successful parsed proposals. A transient outage must not
        # poison all future opportunities for the same archived record.
        fee = agent['input']*tokens.count(prompt+instructions)
        fee += agent['output']*(tokens.count(answer) if isinstance(answer, str) else self.cfg['summary_tokens'])
        try:
            rows = json.loads(answer)['records']
            if not isinstance(rows, list) or len(rows) != 1:
                raise ValueError()
            row = rows[0]
            if (type(row['id']) is not int or row['id'] != body['records'][0]['id'] or
                    row['retention'] not in ('persistent', 'working', 'ephemeral') or
                    not isinstance(row['summary'], str) or not row['summary'].strip() or
                    tokens.count(row['summary']) > body['summary_tokens']):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            if isinstance(answer, str):
                self.invalid_proposals.add(key)
            sim.M['infinite_agent_invalid'] += 1
            self.rejections['agent_invalid_or_failed'] += 1
            return None, fee
        self.proposals[key] = row
        return row, fee

    def _review(self, sim, item, task, task_version):
        # Every original character is reviewed. Large records are split into
        # bounded chunks; no head/tail excerpt can stand for unread content.
        width = self.cfg['evidence_tokens']
        text = item.get('text', '')
        chunks = tokens.split(text, width) or ['']
        pieces, fee = [], 0.
        for index, chunk in enumerate(chunks):
            body = dict(task=tokens.tail(task, width), task_truncated=tokens.count(task)>width,
                        records=[dict(id=item['id'], kind=content_type(item), text=chunk)],
                        part=index+1, parts=len(chunks), summary_tokens=self.cfg['summary_tokens'])
            row, cost = self._ask(sim, body)
            fee += cost
            if row is None:
                return None, fee
            if row['retention'] == 'persistent':
                if self.agent_mode == 'control':
                    self.agent_persistent.setdefault(task_version, set()).add(item['id'])
                self.rejections['agent_keep_original'] += 1
                return None, fee
            pieces.append(row['summary'])
        # Retain all part summaries rather than recursively dropping details.
        # More chunks can mean a longer retained summary; the actual size is
        # checked before commit and may make this candidate unprofitable.
        return '\n'.join(pieces), fee

    def _choose(self, sim, candidates, fee, pressure):
        if not candidates:
            return [], 0.
        positions = {s['id']: i for i, s in enumerate(sim.ctx)}
        cap, trigger, target, reserve = self._limits(sim)
        deficit = max(0., self._size(sim)-target)
        # Under pressure maximize usable headroom first, then minimize expected
        # cost per removed token. Ordinary opportunistic edits must still pay.
        ordered = sorted(candidates, key=lambda c: (c['recovery']/c['saved'], -c['saved'])) if pressure else sorted(candidates, key=lambda c: -c['gain'])
        batch, best, score = [], [], 0.
        for c in ordered[:self.cfg['batch_size']]:
            if not pressure and sum(x['risk'] for x in batch)+c['risk'] > self.policy['constraints']['max_risk']:
                self.rejections['opportunistic_reuse_gate'] += 1
                continue
            batch.append(c)
            first = min(positions[x['item']['id']] for x in batch)
            retained = sum(sim.seg_size(s) for s in sim.ctx[first:])-sum(x['saved'] for x in batch)
            ttl = sim.P.get('cache_ttl')
            warm = sim._prev is not None and not (ttl is not None and sim.t is not None and sim._last_t is not None and sim.t-sim._last_t > ttl)
            rebuild = max(0., sim.WRITE-sim.CACHED)*max(0., retained) if warm else 0.
            net = sum(x['gain'] for x in batch)-rebuild-fee
            if pressure or net > max(score, self.cfg['min_gain']*(rebuild+fee)):
                best, score = list(batch), net
            else:
                self.rejections['negative_net_saving'] += 1
            if pressure and sum(x['saved'] for x in batch) >= deficit:
                break
        return best, score

    def _run(self, sim, r, force=False):
        if not self.policy or not self.policy['evidence']['enabled']:
            return False
        self.rejections = Counter()
        before = self._size(sim)
        cap, trigger, target, reserve = self._limits(sim)
        pressure = force or before >= trigger
        self.last_decision = dict(version=2, request=r, mode=self.agent_mode, pressure=pressure,
            host_tokens_before=before, trigger=trigger, target=target, reserve=reserve,
            horizon=self._horizon(r), horizon_extrapolated=r>=max(self.policy['statistics']['lengths']),
            selected=[], auxiliary_cost_estimate=0., net_saving_estimate=0.,
            policy_sha256=self.policy['sha256'], evidence='estimated costs and reuse, not calibrated task-loss probability')
        if self.agent_mode != 'off' and not pressure:
            # Defer paid review until its output is actionable. The trigger
            # already reserves observed growth for the following request.
            self.last_decision.update(host_tokens_after=before, capacity_feasible=before<=cap,
                                      rejections={'below_trigger': 1})
            return False
        if self.last_attempt is not None and r-self.last_attempt < self.cfg['cooldown'] and not pressure:
            self.last_decision['rejections'] = {'cooldown': 1}
            return False
        self.last_attempt = r
        task_rows = [(s['id'], s.get('text', '')) for s in sim.sent() if s.get('role') in ('user','system','developer')]
        task_version = infinite_policy.digest(task_rows)
        task = json.dumps(task_rows, ensure_ascii=False)
        items = self._eligible(sim)
        self.last_decision['eligible_records'] = len(items)
        items = [s for s in items if s['id'] not in self.agent_persistent.get(task_version, set())]
        # A step may use several batches when necessary, bounded by the original
        # eligible set. Failed, vetoed and committed records are never retried here.
        items.sort(key=lambda s: (-sim.seg_size(s), s['id']))
        changed = False
        for offset in range(0, len(items), self.cfg['batch_size']):
            batch = items[offset:offset+self.cfg['batch_size']]
            fee, candidates = 0., []
            if self.agent_mode in ('control', 'shadow'):
                for item in batch:
                    agent = self.policy['agent']
                    n = max(1, math.ceil(tokens.count(item.get('text', ''))/self.cfg['evidence_tokens']))
                    # each part: the prompt, the task excerpt and the part (evidence_tokens each), one summary out
                    estimate = n*(agent['input']*(tokens.count(PROMPT)+2*self.cfg['evidence_tokens'])+agent['output']*self.cfg['summary_tokens'])
                    possible = sim.CACHED*sim.seg_size(item)*self._horizon(r)
                    if not pressure and possible <= estimate*(1+self.cfg['min_gain']):
                        self.rejections['agent_not_worth_calling'] += 1
                        continue
                    summary, cost = self._review(sim, item, task, task_version)
                    fee += cost
                    if summary is not None:
                        candidate = self._candidate(sim, item, summary, 'agent', r)
                        if candidate:
                            candidates.append(candidate)
            if self.agent_mode != 'control':
                candidates = self._rule_candidates(sim, batch, r)
            chosen, net = self._choose(sim, candidates, fee, pressure)
            self.last_decision['auxiliary_cost_estimate'] += fee
            current_task = infinite_policy.digest([(s['id'], s.get('text', '')) for s in sim.sent() if s.get('role') in ('user','system','developer')])
            eligible = {s['id'] for s in self._eligible(sim)}
            if current_task != task_version or any(c['item']['id'] not in eligible or c['version'] != infinite_policy.digest(c['item'].get('text','')) for c in chosen):
                self.rejections['stale_proposal'] += 1
                break
            for c in chosen:
                item = c['item']
                if getattr(sim, 'store_dir', None):
                    from pathlib import Path
                    import hashlib
                    from ctxpress.core.artifacts import atomic_json
                    try:
                        atomic_json(str(Path(sim.store_dir)/f'{item["id"]}.infinite.json'), dict(id=item['id'], kind=item.get('kind'), resources=item.get('res',[]), sha256=hashlib.sha256(item['text'].encode()).hexdigest()))
                    except OSError:
                        self.rejections['archive_write_failed'] += 1
                        continue
                sim.keep_text(item, c['text'], form='structured' if c['op'] != 'truncated' else 'truncated')
                if item.get('form', 'full') == 'full':
                    self.rejections['commit_no_reduction'] += 1
                    continue
                item['kept'] = c['size']
                self.removed[item['id']] = dict(kind=item.get('kind','other'), path=self._source(sim,item))
                sim.M['infinite_compacted_records'] += 1
                changed = True
                self.last_decision['selected'].append(dict(id=item['id'], op=c['op'], saved=c['saved'], recovery_probability=c['risk'], recovery_cost=c['recovery']))
            self.last_decision['net_saving_estimate'] += net
            if not pressure or self._size(sim) <= target:
                break
        self.last_decision.update(host_tokens_after=self._size(sim), rejections=dict(self.rejections),
                                  capacity_feasible=self._size(sim)<=cap)
        return changed

    def step(self, sim, r):
        before = self._size(sim)
        if self.previous_after is not None:
            self.growth.append(max(0., before-self.previous_after))
        super().step(sim, r)
        self.previous_after = self._size(sim)
