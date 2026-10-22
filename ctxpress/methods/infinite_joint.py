"""Capacity-aware successor to V3; old frozen policies retain V3 behavior."""
import math
import time
import hashlib
from ctxpress.methods import infinite_memory as memory
from ctxpress.core.context_budget import ModelCapacity, Action, Budget, choose_combinations
from ctxpress.core import infinite_policy as policy, tokens
from ctxpress.methods.infinite_v3 import InfiniteContextV3, SYSTEM
from ctxpress.core.context_forecast import ActionForecast


validate_config = policy.validate_joint


class InfiniteContextJoint(InfiniteContextV3):
    isolate_history_epochs = True
    def __init__(self, policy=None, agent_mode=None, agent_model=None):
        super().__init__(policy, agent_mode, agent_model)
        self.joint = validate_config(self.policy['joint_context'])
        if self.agent_model=='deepseek-flash' and self.joint['aux_output_limit']!=8192:
            raise ValueError('auxiliary bound must match the DeepSeek transport output limit')

    def reset(self, sim):
        super().reset(sim)
        self.growth_request = None
        self.growth_model = None
        self.joint_metadata = {}
        self.rejected_ceiling = {}
        self.reuse_retry_budget = False
        self.action_forecast = None
        self.retired = False

    def retire(self):
        # In-flight provider calls retain their evidence and may finish, but no
        # follow-up call or forecast can enter a replacement history epoch.
        with self.cache_lock:
            self.retired=True
            if hasattr(self,'budget'):self.budget['deadline']=0.
        for executor in (self.executor,self.io_executor):
            if executor is not None:executor.shutdown(wait=False,cancel_futures=True)

    def _forecasts(self):
        if self.action_forecast is None:
            self.action_forecast=ActionForecast(retention_prior=self.joint['retention_ratio_prior'],
                seconds_prior=self.joint['proposal_seconds_prior'],prior_weight=self.joint['forecast_prior_weight'],
                quantile=self.joint['forecast_quantile'],history_limit=self.joint['forecast_history_limit'])
        return self.action_forecast

    def _ask3(self,sim,body):
        # Thread-local frames distinguish actual admitted calls from cache hits
        # and include nested repair cost/time in the parent operation forecast.
        frames=getattr(self.local,'forecast_frames',None)
        if frames is None:frames=self.local.forecast_frames=[]
        frame=dict(admitted=False,cost=0.)
        frames.append(frame);start=time.monotonic();row=None
        try:
            row=super()._ask3(sim,body)
            return row
        finally:
            frames.pop()
            if frame['admitted']:
                with self.cache_lock:
                    self._forecasts().observe(body['operation'],
                        input_tokens=tokens.count(memory.canonical(body.get('records',[]))),
                        output_tokens=tokens.count(memory.canonical(row)) if row else 0,
                        seconds=time.monotonic()-start,cost_estimate=frame['cost'],
                        usable=row is not None and row['retention']!='persistent')

    def _observe_proposal_estimate(self,prompt,answer):
        frames=getattr(self.local,'forecast_frames',[])
        if not frames or not isinstance(answer,str):return
        estimated=tokens.count(SYSTEM+prompt)*self.policy['agent']['input']
        estimated+=tokens.count(answer)*self.policy['agent']['output']
        reserved=frames[-1].get('own_reservation',0.)
        # Update inclusive parent/repair forecasts; keep the spending reservation
        # untouched. Missing responses retain the conservative reservation.
        for frame in frames:frame['cost']+=estimated-reserved

    def prepare_request(self, body):
        from ctxpress.core.host_contract import HostContractError
        try:
            spec=ModelCapacity(**self.joint['models'][body.get('model')])
            budget,_=spec.resolve(body.get('max_output_tokens'),growth_tokens=0,
                uncertainty_tokens=0,protected_tokens=0)
        except (KeyError,TypeError,ValueError) as error:
            # Configuration failure must not enter the proxy's generic
            # "rewrite failed, forward original" fallback.
            raise HostContractError('joint context model/output configuration is missing or invalid') from error
        return dict(body,max_output_tokens=budget.output_reserve)

    def on_overflow(self, sim, request):
        # A provider rejection outranks our approximate length estimate. Record
        # a conservative observed rejection ceiling, not invented usage tokens.
        model=getattr(sim,'request_model',None)
        before=self._size(sim)
        self.rejected_ceiling[model]=min(self.rejected_ceiling.get(model,float('inf')),
                                         math.nextafter(before,0.))
        self.force_pressure=True
        self.reuse_retry_budget=True
        try:
            self.step(sim,max(0,sim.r-1))
        finally:
            self.force_pressure=False
            self.reuse_retry_budget=False
        return self._size(sim)<before

    def _admit_proposal(self, sim, body, prompt, budget):
        # Reserve an explicit upper estimate before dispatch. Concurrent workers
        # and repair calls share this reservation; failed or missing-usage calls
        # do not magically restore spending capacity. It is not a billed fee.
        if self.retired:
            self.rejections['retired_history_epoch']+=1
            return False
        rate=self.policy['agent']
        estimate=(len((SYSTEM+prompt).encode('utf-8'))*rate['input']+
                  self.joint['aux_output_limit']*rate['output'])
        reserved=budget.get('reserved_cost',0.)
        if reserved+estimate>self.joint['max_aux_cost']:
            self.rejections['auxiliary_cost_budget']+=1
            return False
        prediction=self._forecasts().predict(body['operation'])
        latency=prediction['seconds']
        expected=prediction['mean_observed_cost_estimate']
        if expected is None:
            expected=tokens.count(SYSTEM+prompt)*rate['input']+body['max_tokens']*rate['output']
        expected=min(estimate,expected)
        if latency>max(0.,budget['deadline']-time.monotonic()):
            self.rejections['predicted_time_budget']+=1
            return False
        snapshot=budget['joint_snapshot']
        if snapshot['protected_tokens']>snapshot['working_limit']:
            self.rejections['protected_floor_infeasible']+=1
            return False
        before=snapshot['before']
        pressure=before>snapshot['working_limit'] or snapshot['overflow']
        # Predict this operation's reduction from past same-operation outcomes,
        # capped by eligible history. It is an estimate, not proof of savings.
        eligible=max(0.,before-snapshot['protected_tokens'])
        predicted_saved=tokens.count(memory.canonical(body.get('records',[])))*snapshot['ratio']*(1-prediction['retained_ratio'])
        gross=min(eligible,predicted_saved)*sim.CACHED*self._horizon(sim.r)
        budget.setdefault('forecast_decisions',[]).append(dict(operation=body['operation'],
            prediction=prediction,estimated_gross_saving=gross,reserved_call_cost=estimate,
            expected_call_cost=expected,pressure=pressure))
        if not pressure and expected+latency*self.joint['latency_rate']>=gross:
            self.rejections['proposal_predicted_unprofitable']+=1
            return False
        budget['reserved_cost']=reserved+estimate
        frames=getattr(self.local,'forecast_frames',[])
        for frame in frames:frame['cost']+=estimate
        if frames:
            frames[-1]['admitted']=True
            frames[-1]['own_reservation']=estimate
        return True

    def _calibration(self, sim):
        model = getattr(sim, 'request_model', None)
        if model not in self.joint['models']:
            raise ValueError('request model lacks an explicit physical context configuration')
        return model, getattr(sim, 'sizes', None)

    def _working_limits(self, sim, floor):
        model, fit = self._calibration(sim)
        # Sizes are estimated input tokens (ctxpress.core.sizes). The uncertainty reserve is the largest recent
        # shortfall of the estimate against the bill; before any answered request, the declared cold-start share
        # (token_ratio_prior - 1) of the estimate.
        tokens = self._size(sim)
        shortfall, samples = fit.envelope() if fit is not None else (0.0, 0)
        uncertainty = shortfall if samples else max(0., self.joint['token_ratio_prior']-1)*tokens
        estimate = dict(tokens=tokens, ratio=1.0, upper_ratio=1.0+(uncertainty/tokens if tokens else 0.),
                        uncertainty=uncertainty, samples=samples,
                        scope='largest recent shortfall of the size estimate; no guaranteed provider-token bound')
        samples = sorted(self.growth[-128:])
        observed = samples[max(0, math.ceil(self.joint['growth_quantile']*len(samples))-1)] if samples else 0.
        growth = max(self.joint['growth_prior'], observed) * estimate['upper_ratio']
        spec = ModelCapacity(**self.joint['models'][model])
        budget, provenance = spec.resolve(getattr(sim, 'request_output_limit', None),
            growth_tokens=growth, uncertainty_tokens=estimate['uncertainty'],
            protected_tokens=floor*estimate['ratio'])
        provenance['output_source']=getattr(sim,'request_output_source',provenance['output_source'])
        rejected=self.rejected_ceiling.get(model)
        effective_input=budget.input_limit
        if rejected is not None:
            effective_input=min(effective_input,rejected*estimate['ratio'])
            budget=Budget(budget.context_limit,budget.output_reserve,budget.growth_reserve,
                budget.uncertainty_reserve+budget.input_limit-effective_input,budget.protected_tokens)
        self.joint_budget = budget
        self.joint_ratio = estimate['ratio']
        self.joint_metadata = dict(model=model, context_limit=spec.context_limit,
            input_limit=budget.input_limit, working_limit=budget.working_limit,
            growth_reserve=growth, uncertainty_reserve=estimate['uncertainty'],
            calibration=estimate, growth_samples=len(samples), **provenance)
        self.joint_metadata.update(estimated_input_ceiling=effective_input,
            upstream_rejection_rendered_ceiling=rejected)
        # Admission workers retain their request's planning snapshot even if a
        # following request updates the controller's current capacity estimate.
        self.budget['joint_snapshot']=dict(before=estimate['tokens'],
            working_limit=budget.working_limit,protected_tokens=budget.protected_tokens,
            overflow=bool(getattr(self,'force_pressure',False)),ratio=estimate['ratio'])
        ratio = estimate['ratio']
        return effective_input/ratio, budget.working_limit/ratio, budget.working_limit/ratio, growth/ratio

    def _execute_joint_candidates(self,sim,annotations,task,task_version,pressure):
        candidates=[]
        positions={s['id']:i for i,s in enumerate(sim.ctx)}
        def add(items,row,kind):
            value=dict(kind=kind,children=[s['memory_key'] for s in items],state=row,
                task_version=task_version,sources=[s['id'] for s in items],
                resources=sorted({p for s in items for p in s.get('res',[])}))
            key=hashlib.sha256(memory.canonical(value).encode()).hexdigest()
            text=memory.canonical(row)+self._reference(sim,key)
            if self._preview(sim,items,text)>=self._size(sim):return
            risk=max((self._risk(s,sim.r,'agent')[0] for s in items if s['seg']=='out'),default=1.)
            candidates.append(dict(items=items,row=row,kind=kind,text=text,
                ids={s['id'] for s in items},recovery=risk*self.policy['observations']['repeat_request_cost']))
        for items,row in annotations:
            if not any(s['seg']=='summary' for s in items):add(items,row,'unit')
        # Group alternatives overlap their unit alternatives. Search may choose
        # either, never both; protected gaps are never crossed.
        groups=[]
        for unit,row in annotations:
            if groups:
                prev=groups[-1][-1]
                if (positions[prev[0][-1]['id']]+1==positions[unit[0]['id']] and
                        (pressure or self._similar(prev[1],row)) and len(groups[-1])<self.cfg['batch_size']):
                    groups[-1].append((unit,row));continue
            groups.append([(unit,row)])
        for group in groups:
            if len(group)<2 or time.monotonic()>=self.deadline:continue
            row=self._ask3(sim,dict(operation='pressure_compact' if pressure else 'compact',task=task,
                records=[row for _,row in group],max_tokens=self.v3['group_tokens']))
            if row is not None and row['retention']!='persistent' and (pressure or row['completed']):
                add([s for unit,_ in group for s in unit],row,'stage')
        def evaluate(indices):
            chosen=[candidates[i] for i in indices]
            replacements={s_id:c for c in chosen for s_id in c['ids']}
            rendered=[];inserted=set()
            for s in sim.ctx:
                candidate=replacements.get(s['id'])
                if candidate is None:rendered.append(s);continue
                first=candidate['items'][0]['id']
                if first not in inserted:
                    text=candidate['text'];inserted.add(first)
                    rendered.append(dict(id=-900000000-len(rendered),seg='summary',form='full',
                        text=text,size=sim.message_tokens(text),role='user',born=sim.r,last=sim.r))
            after=sim.measure_history(rendered)
            # The rewritten suffix is charged once for the whole combination.
            cache_tokens=0.
            if chosen:
                first=min(positions[c['items'][0]['id']] for c in chosen)
                suffix=sum(sim.seg_size(s) for s in sim.ctx[first:])
                cache_tokens=min(after,max(0.,suffix-(self._size(sim)-after)))
            return Action('keep' if not indices else ','.join(map(str,indices)),after*self.joint_ratio,
                recovery_cost=sum(c['recovery'] for c in chosen),
                cache_rewrite_tokens=cache_tokens*self.joint_ratio)
        result=choose_combinations(self.joint_budget,[c['ids'] for c in candidates],evaluate,
            beam_width=self.joint['planner_beam'],deadline=self.deadline,
            remaining_requests=self._horizon(sim.r),cached_rate=sim.CACHED,input_rate=sim.WRITE,
            latency_rate=0,seconds_available=max(0.,self.deadline-time.monotonic()))
        self.last_decision['combination_plan']=result
        selected=result['selected_indices']
        if selected is None:return
        committed=0
        for i in selected:
            c=candidates[i]
            # The whole combination has already passed cost/capacity selection.
            # Keep V3's final task-version, deadline, provenance and host checks.
            if not super()._commit(sim,c['items'],c['row'],task_version,True,0.,c['kind']):break
            committed+=1
        result['committed']=committed
        result['commit_complete']=committed==len(selected)

    def step(self, sim, r):
        if not self.policy['evidence']['enabled']:
            return
        model = getattr(sim, 'request_model', None)
        if self.growth_model != model:
            self.growth = []
            self.previous_after = None
            self.growth_request = None
            self.growth_model = model
        # Sample the incoming request once, including paths that do no work.
        # Internal passes and upstream retries of the same request are not new
        # growth observations. Preserve the sample set around legacy V3 passes.
        if self.growth_request != r:
            if self.previous_after is not None:
                self.growth.append(max(0., self._size(sim)-self.previous_after))
                self.growth = self.growth[-128:]
            self.growth_request = r
        samples = list(self.growth)
        self.previous_after = None
        try:
            super().step(sim, r)
        finally:
            self.growth = samples
            self.previous_after = self._size(sim)
        self.last_decision.update(version=4, target_basis='model_capacity_minus_output_growth_uncertainty',
                                  joint_context=self.joint_metadata,
                                  auxiliary_reserved_cost=self.budget.get('reserved_cost',0.),
                                  forecast_decisions=list(self.budget.get('forecast_decisions',[])),
                                  auxiliary_budget_scope='conservative byte-length estimate plus declared maximum output; not a bill')

    def _step_once(self, sim, r, continuing=False):
        # Prevent the legacy loop from injecting compression passes into the
        # history-growth distribution before this request has finished.
        self.previous_after = None
        super()._step_once(sim, r, continuing or self.reuse_retry_budget)
        self.previous_after = None
