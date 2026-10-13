"""Joint capacity/cost planning in provider-token units.

The caller supplies complete, mutually exclusive actions, including no-op.
Estimates are not provider guarantees. This module does not shorten protected
content or infer the model's physical context limit from a fitted threshold.
"""
from dataclasses import dataclass, asdict
import math
from collections import deque
import time


def choose_combinations(budget, occupied, evaluate, *, beam_width, deadline, **costs):
    """Bounded search over non-overlapping whole-history replacements.

    evaluate(indices) must render the complete resulting prompt and return an
    Action. Shared cache invalidation is charged there, never summed per item.
    Beam pruning is explicit: this is not a proof of global optimality.
    """
    if type(beam_width) is not int or beam_width < 2:
        raise ValueError('beam width must be at least two')
    occupied = [frozenset(ids) for ids in occupied]
    if any(not ids for ids in occupied):
        raise ValueError('replacement must cover at least one original item')
    states = {(): frozenset()}
    memo = {}
    pruned = 0
    def score(indices):
        if indices not in memo:
            action = evaluate(indices)
            row = choose(budget, [action], **costs)['candidates'][0]
            memo[indices] = (action, row)
        return memo[indices][1]
    for i, ids in enumerate(occupied):
        if time.monotonic() >= deadline:
            return dict(selected_indices=None, status='planning_deadline', pruned=pruned)
        expanded = dict(states)
        for indices, used in states.items():
            if not ids & used:
                expanded[indices+(i,)] = used | ids
        ranked = []
        for indices in expanded:
            if time.monotonic() >= deadline:
                return dict(selected_indices=None, status='planning_deadline', pruned=pruned)
            row = score(indices)
            ranked.append((not row['feasible'], max(0.,row['input_tokens']-budget.working_limit),
                           row['total_cost'],indices))
        # Feasible plans sort by cost, not by how aggressively they compress.
        ranked.sort(key=lambda v:(v[0], v[1] if v[0] else v[2], v[2], v[3]))
        retained = [v[3] for v in ranked if v[3]][:beam_width-1]
        retained.insert(0,())
        pruned += max(0,len(expanded)-len(retained))
        states = {indices:expanded[indices] for indices in retained}
    actions = [memo[i][0] if i in memo else evaluate(i) for i in states]
    result = choose(budget, actions, **costs)
    by_key = {a.key:i for a,i in zip(actions,states)}
    result.update(selected_indices=by_key.get(result['selected']), pruned=pruned,
                  scope='bounded combination search; global optimality not guaranteed')
    return result


class TokenCalibration:
    """Causal, bounded calibration for one session/model and exact wire prompts.

    The upper estimate is the largest observed ratio or the declared cold-start
    ratio. This is an empirical envelope, not a statistical coverage guarantee.
    """
    def __init__(self, prior_ratio=1., sample_limit=128):
        nonnegative(prior_ratio)
        if prior_ratio == 0 or type(sample_limit) is not int or sample_limit < 1:
            raise ValueError('positive calibration prior and sample limit required')
        self.prior_ratio = prior_ratio
        self.samples = deque(maxlen=sample_limit)
        self.identities = deque(maxlen=sample_limit)

    def observe(self, identity, rendered_tokens, input_tokens):
        nonnegative(rendered_tokens)
        nonnegative(input_tokens)
        if identity in self.identities or rendered_tokens == 0 or input_tokens == 0:
            return False
        self.samples.append(input_tokens / rendered_tokens)
        self.identities.append(identity)
        return True

    def estimate(self, rendered_tokens):
        nonnegative(rendered_tokens)
        ratios = sorted(self.samples)
        ratio = ratios[len(ratios)//2] if ratios else self.prior_ratio
        upper = max([self.prior_ratio, *ratios])
        return dict(tokens=rendered_tokens * ratio,
                    uncertainty=rendered_tokens * (upper-ratio),
                    samples=len(ratios), ratio=ratio, upper_ratio=upper,
                    scope='empirical envelope; no guaranteed provider-token bound')


def nonnegative(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('expected a finite nonnegative number')
    return value


@dataclass(frozen=True)
class Budget:
    context_limit: float
    output_reserve: float
    growth_reserve: float
    uncertainty_reserve: float
    protected_tokens: float

    def __post_init__(self):
        for value in asdict(self).values():
            nonnegative(value)
        if self.context_limit == 0:
            raise ValueError('physical context limit must be positive')

    @property
    def input_limit(self):
        return self.context_limit - self.output_reserve

    @property
    def working_limit(self):
        return self.input_limit - self.growth_reserve - self.uncertainty_reserve


@dataclass(frozen=True)
class ModelCapacity:
    """Explicit service constraint, separate from fitted working thresholds."""
    context_limit: int
    max_output_tokens: int
    default_output_reserve: int
    source: str

    def __post_init__(self):
        for value in (self.context_limit, self.max_output_tokens, self.default_output_reserve):
            if type(value) is not int or value <= 0:
                raise ValueError('positive integer model limits required')
        if not 0 < self.default_output_reserve <= self.max_output_tokens <= self.context_limit:
            raise ValueError('inconsistent model output/context limits')
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError('model capacity provenance required')

    def resolve(self, requested_output, *, growth_tokens, uncertainty_tokens, protected_tokens):
        # No advertised request maximum means use the explicitly declared
        # fallback. This assumption is visible, not inferred from past outputs.
        output = self.default_output_reserve if requested_output is None else requested_output
        if type(output) is not int or not 0 < output <= self.max_output_tokens:
            raise ValueError('request output limit exceeds configured model bound')
        budget = Budget(self.context_limit, output, growth_tokens, uncertainty_tokens, protected_tokens)
        return budget, dict(context_source=self.source,
                            output_source='configured_fallback' if requested_output is None else 'request',
                            output_reserve=output)


@dataclass(frozen=True)
class Action:
    key: str
    input_tokens: float
    auxiliary_cost: float = 0.
    recovery_cost: float = 0.
    cache_rewrite_tokens: float = 0.
    seconds: float = 0.
    preserves_required_facts: bool = True

    def __post_init__(self):
        if not isinstance(self.key, str) or not self.key:
            raise ValueError('action key required')
        if type(self.preserves_required_facts) is not bool:
            raise ValueError('explicit fact-preservation result required')
        for name, value in asdict(self).items():
            if name not in ('key', 'preserves_required_facts'):
                nonnegative(value)
        if self.cache_rewrite_tokens > self.input_tokens:
            raise ValueError('cache rewrite exceeds retained prompt')


def choose(budget, actions, *, remaining_requests, cached_rate, input_rate,
           latency_rate, seconds_available):
    """Minimum estimated cost among supplied feasible actions, with audit rows.

    Remaining requests, prices and latency cost share declared accounting units.
    Latency cost can be zero, but the available time constraint still applies.
    An infeasible result has no selected action; it never relaxes capacity or
    fact preservation to manufacture a successful decision.
    """
    for value in (remaining_requests, cached_rate, input_rate, latency_rate, seconds_available):
        nonnegative(value)
    if remaining_requests < 1 or input_rate < cached_rate:
        raise ValueError('invalid horizon or input pricing')
    actions = list(actions)
    if len({a.key for a in actions}) != len(actions):
        raise ValueError('action keys must be unique')
    rows = []
    for action in actions:
        reasons = []
        if not action.preserves_required_facts:
            reasons.append('required_facts_not_preserved')
        if action.input_tokens < budget.protected_tokens:
            reasons.append('below_protected_floor')
        if action.input_tokens > budget.input_limit:
            reasons.append('context_capacity')
        if action.input_tokens > budget.working_limit:
            reasons.append('insufficient_reserved_headroom')
        if action.seconds > seconds_available:
            reasons.append('time_budget')
        costs = dict(
            retained_input=action.input_tokens * remaining_requests * cached_rate,
            cache_rewrite=action.cache_rewrite_tokens * (input_rate - cached_rate),
            auxiliary=action.auxiliary_cost,
            recovery=action.recovery_cost,
            latency=action.seconds * latency_rate,
        )
        rows.append(dict(key=action.key, feasible=not reasons, reasons=reasons,
                         costs=costs, total_cost=sum(costs.values()),
                         input_tokens=action.input_tokens))
    feasible = [r for r in rows if r['feasible']]
    best = min(feasible, key=lambda r: r['total_cost']) if feasible else None
    return dict(selected=best['key'] if best else None,
                status='feasible' if best else 'no_feasible_action',
                input_limit=budget.input_limit, working_limit=budget.working_limit,
                protected_feasible=budget.protected_tokens <= budget.working_limit,
                candidates=rows,
                scope='minimum estimated cost among supplied actions; not a global optimum or quality guarantee')
