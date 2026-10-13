"""Portable, integrity-checked history-fitted policies. No executable deserialization."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
from ctxpress.core import calibration
from ctxpress.core.policy import canonical, parameter_rows, parameters

SCHEMA = 'ctxpress.infinite-context'
# Bounds describe the search space, not claims about optimal values.
BOUNDS = {
    'trigger_ratio': (.1, 1), 'target_ratio': (.05, .95),
    'work_turns': (0, 64), 'cooldown': (0, 64), 'batch_size': (1, 64),
    'summary_tokens': (32, 8192), 'evidence_tokens': (256, 65536),
    'horizon_scale': (.1, 4), 'risk_scale': (1, 16),
    'prior_strength': (1, 256), 'min_gain': (0, 1),
}
INTS = {'work_turns', 'cooldown', 'batch_size', 'summary_tokens', 'evidence_tokens', 'prior_strength'}


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def number(x, lo=0, hi=float('inf')):
    if type(x) not in (float, int) or not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError('expected a finite number within bounds')
    return x


def settings(value):
    if not isinstance(value, dict) or set(value) != set(BOUNDS):
        raise ValueError('policy must specify every behavioral parameter')
    for k, (lo, hi) in BOUNDS.items():
        number(value[k], lo, hi)
        if k in INTS and type(value[k]) is not int:
            raise ValueError(f'{k} must be integer')
    if value['target_ratio'] >= value['trigger_ratio']:
        raise ValueError('target must be below trigger')
    return value


def validate(bundle):
    if not isinstance(bundle, dict) or bundle.get('schema') != SCHEMA or bundle.get('version') not in (1, 2, 3, 4):
        raise ValueError('unsupported infinite-context policy')
    if digest({k: v for k, v in bundle.items() if k != 'sha256'}) != bundle.get('sha256'):
        raise ValueError('policy integrity check failed')
    settings(bundle['settings'])
    if bundle['version'] in (3, 4):
        validate_v3(bundle['v3'])
        if bundle.get('agent') is None:
            raise ValueError('V3 requires a declared summarization model')
    if bundle['version'] == 4:
        validate_joint(bundle.get('joint_context'))
    calibration.validate(bundle['statistics'])
    params = parameters(bundle['parameters'])
    constraints = bundle['constraints']
    number(constraints['capacity'], 1)
    number(constraints['max_risk'], 0, 1)
    number(constraints['max_replay_silent'])
    if params.get('window') != constraints['capacity']:
        raise ValueError('capacity differs from frozen accounting parameters')
    for key in ('write', 'cached', 'out'):
        number(params.get(key))
    if params.get('write') < params.get('cached'):
        raise ValueError('uncached price cannot be below cached price')
    for k in ('extra_output_tokens', 'repeat_request_cost'):
        number(bundle['observations'][k])
    aux = bundle.get('agent')
    if aux is not None:
        if not isinstance(aux.get('model'), str) or not aux['model'].strip():
            raise ValueError('agent model must be explicit')
        for k in ('input', 'output'):
            number(aux[k])
        if aux.get('unit') != 'same_as_main_per_token':
            raise ValueError('agent rates must use the main policy cost unit per token')
    evidence = bundle['evidence']
    if evidence['scope'] != 'fixed-history replay; not task-quality or causal recovery validation':
        raise ValueError('invalid evidence scope')
    train, test = set(evidence['training_groups']), set(evidence['holdout_groups'])
    if not train or train & test:
        raise ValueError('training and holdout groups overlap')
    if type(evidence.get('enabled')) is not bool:
        raise ValueError('explicit deployment status required')
    return bundle


def seal(bundle):
    bundle = copy.deepcopy(bundle)
    bundle.pop('sha256', None)
    bundle['sha256'] = digest(bundle)
    return validate(bundle)


def load(source):
    return validate(copy.deepcopy(source) if isinstance(source, dict) else json.loads(Path(source).read_text()))


def save(path, bundle):
    from ctxpress.core.artifacts import atomic_json
    validate(bundle)
    atomic_json(str(Path(path).resolve()), bundle)
    return str(Path(path).resolve())


def make(config, stats, params, observations, groups, *, max_risk=.02, max_replay_silent=0,
         agent=None, enabled=True, version=1):
    return seal(dict(schema=SCHEMA, version=version, settings=settings(config), statistics=stats,
        parameters=parameter_rows(params), observations=observations, agent=agent,
        constraints=dict(capacity=params.get('window'), max_risk=max_risk, max_replay_silent=max_replay_silent),
        evidence=dict(scope='fixed-history replay; not task-quality or causal recovery validation',
                      training_groups=sorted(set(groups)), holdout_groups=[], enabled=enabled)))


# Every behavioral control is explicit, sealed and available to candidate search.
V3_BOUNDS = {'long_tokens':(64,16384), 'eviction_k':(1,64), 'compact_m':(1,32),
          'group_tokens':(128,8192), 'similarity':(0,1), 'pressure_seconds':(1,300),
          'max_aux_calls':(1,512), 'annotation_workers':(1,8), 'retry_seconds':(1,60)}


def validate_v3(cfg):
    if not isinstance(cfg,dict) or set(cfg)!=set(V3_BOUNDS):
        raise ValueError('V3 must declare every behavioral parameter')
    for k,(lo,hi) in V3_BOUNDS.items():
        number(cfg[k],lo,hi)
        if k!='similarity' and type(cfg[k]) is not int:
            raise ValueError(k+' must be integer')
    return cfg


def validate_joint(config):
    if not isinstance(config, dict) or set(config) != {'models', 'growth_quantile', 'growth_prior', 'token_ratio_prior',
            'max_aux_cost', 'aux_output_limit', 'proposal_seconds_prior', 'latency_rate', 'planner_beam',
            'retention_ratio_prior','forecast_prior_weight','forecast_quantile','forecast_history_limit'}:
        raise ValueError('explicit joint context configuration required')
    if not isinstance(config['models'], dict) or not config['models']:
        raise ValueError('explicit model capacity mapping required')
    for name, limits in config['models'].items():
        if not isinstance(name, str) or not name:
            raise ValueError('model name required')
        from ctxpress.core.context_budget import ModelCapacity
        ModelCapacity(**limits)
    number(config['growth_quantile'], .5, 1)
    number(config['growth_prior'])
    number(config['token_ratio_prior'], .01)
    for key in ('max_aux_cost', 'proposal_seconds_prior', 'latency_rate'):
        number(config[key])
    if type(config['aux_output_limit']) is not int or config['aux_output_limit']<1:
        raise ValueError('explicit auxiliary output bound required')
    if type(config['planner_beam']) is not int or not 2<=config['planner_beam']<=1024:
        raise ValueError('planner beam must be an integer between 2 and 1024')
    from ctxpress.core.context_forecast import ActionForecast
    ActionForecast(retention_prior=config['retention_ratio_prior'],seconds_prior=config['proposal_seconds_prior'],
        prior_weight=config['forecast_prior_weight'],quantile=config['forecast_quantile'],
        history_limit=config['forecast_history_limit'])
    return config
