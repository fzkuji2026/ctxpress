"""Causal action forecasts with explicit cold-start priors and failures."""
from collections import defaultdict, deque
import math
from ctxpress.core.context_budget import nonnegative


class ActionForecast:
    def __init__(self, *, retention_prior, seconds_prior, prior_weight, quantile, history_limit):
        for value in (retention_prior, seconds_prior, prior_weight, quantile):nonnegative(value)
        if not 0<=retention_prior<=1 or prior_weight<=0 or not .5<=quantile<=1:
            raise ValueError('invalid forecast prior or quantile')
        if type(history_limit) is not int or history_limit<1:
            raise ValueError('positive integer history limit required')
        self.retention_prior=retention_prior;self.seconds_prior=seconds_prior
        self.prior_weight=prior_weight;self.quantile=quantile;self.history_limit=history_limit
        self.history=defaultdict(lambda:deque(maxlen=history_limit))

    def observe(self, operation, *, input_chars, output_chars, seconds, cost_estimate, usable):
        for value in (input_chars,output_chars,seconds,cost_estimate):nonnegative(value)
        if not isinstance(operation,str) or not operation or type(usable) is not bool:
            raise ValueError('operation and explicit result required')
        retained=min(1.,output_chars/max(1.,input_chars)) if usable else 1.
        self.history[operation].append(dict(retained=retained,seconds=seconds,
            cost_estimate=cost_estimate,usable=usable))

    def predict(self,operation):
        rows=list(self.history.get(operation,()))
        n=len(rows);weight=self.prior_weight
        retained=(weight*self.retention_prior+sum(r['retained'] for r in rows))/(weight+n)
        elapsed=sorted(r['seconds'] for r in rows)
        observed=elapsed[max(0,math.ceil(self.quantile*n)-1)] if n else self.seconds_prior
        seconds=(weight*self.seconds_prior+n*observed)/(weight+n)
        return dict(samples=n,retained_ratio=retained,seconds=seconds,
            failures=sum(not r['usable'] for r in rows),
            mean_observed_cost_estimate=sum(r['cost_estimate'] for r in rows)/n if n else None,
            scope='same-session past action estimates; not provider billing or task-quality evidence')
