"""Frozen deployment policies selected by offline screening; no task-quality guarantee."""
from __future__ import annotations
import copy, hashlib, json, math, os
from ctxpress.core import calibration
from ctxpress.core.params import DEFAULT, P, Params


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def numbers(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('policy values must be finite')
    if isinstance(value, dict):
        for child in value.values():
            numbers(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            numbers(child)


def parameter_rows(params):
    return [dict(name=name, value=copy.deepcopy(value), source=source, note=note)
            for name, value, source, note in sorted(params.table())]


def parameters(rows):
    if not isinstance(rows, list):
        raise ValueError('policy parameters must be a table')
    result, seen = Params(), set()
    required = {name for name, *_ in DEFAULT.table()}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'name', 'value', 'source', 'note'}:
            raise ValueError('invalid policy parameter row')
        name, value = row['name'], row['value']
        if not isinstance(name, str) or name in seen:
            raise ValueError('invalid or duplicate policy parameter')
        seen.add(name)
        if not isinstance(row['source'], str) or not isinstance(row['note'], str):
            raise ValueError('invalid parameter provenance')
        parts = name.split('/')
        if len(parts) == 3 and parts[0] in ('q', 'coverage') and all(parts[1:]):
            calibration._number(value, high=1)
            getattr(result, parts[0])[tuple(parts[1:])] = P(value, row['source'], row['note'])
        elif len(parts) == 1 and isinstance(getattr(result, name, None), P):
            original = getattr(result, name).value
            if isinstance(original, bool):
                if type(value) is not bool:
                    raise ValueError('invalid boolean policy parameter')
            elif isinstance(original, str):
                if not isinstance(value, str):
                    raise ValueError('invalid text policy parameter')
            elif value is not None or original is not None:
                calibration._number(value)
            setattr(result, name, P(value, row['source'], row['note']))
        else:
            raise ValueError('unknown policy parameter')
    if not required <= seen or result.get('cache_mode') not in ('lcp', 'checkpoint') or result.get('window') < 1:
        raise ValueError('incomplete or invalid policy parameters')
    return result


def same_parameters(left, right):
    return {name: value for name, value, *_ in left.table()} == {name: value for name, value, *_ in right.table()}


def validate(bundle):
    if not isinstance(bundle, dict) or bundle.get('schema') != 'ctxpress.cost-policy' or bundle.get('version') != 1:
        raise ValueError('unsupported cost policy')
    content = {key: value for key, value in bundle.items() if key != 'sha256'}
    if hashlib.sha256(canonical(content).encode()).hexdigest() != bundle.get('sha256'):
        raise ValueError('cost policy has changed')
    numbers(bundle)
    calibration.validate(bundle['statistics'])
    params = parameters(bundle['parameters'])
    reference = bundle['reference']
    if reference.get('class') != 'CodexAutoCompact' or set(reference.get('args', {})) - {'t', 'size', 'keep_user'}:
        raise ValueError('policy fallback must be native Codex compaction')
    limit = reference.get('args', {}).get('t', 230000)
    if type(limit) is not int or limit < 1:
        raise ValueError('invalid policy fallback threshold')
    if params.get('window') != limit:
        raise ValueError('screened window and native fallback threshold differ')
    screening = bundle['screening']
    if screening.get('evidence') != 'replay proxy; not real task-quality validation' or screening.get('kind') not in ('strict', 'harm', 'measured'):
        raise ValueError('invalid screening evidence')
    eps = calibration._number(screening['eps'])
    if eps != params.get('eps'):
        raise ValueError('screening tolerance differs from the frozen parameters')
    names = screening.get('sessions')
    if not isinstance(names, list) or len(names) < 2 or len(set(names)) != len(names) or not all(isinstance(n, str) and n for n in names):
        raise ValueError('policy selection needs independent named historical sessions')
    rows = screening.get('candidates')
    if not isinstance(rows, list) or not rows:
        raise ValueError('missing policy candidates')
    lambdas, eligible = [], []
    for row in rows:
        lam = calibration._number(row['lam'])
        lambdas.append(lam)
        folds = row['folds']
        if len(folds) != len(names) or {fold['session'] for fold in folds} != set(names):
            raise ValueError('candidate does not cover every historical session')
        passes = []
        for fold in folds:
            for key in ('cost', 'reference_cost', 'perf', 'reference_perf'):
                calibration._number(fold[key])
            ok = fold['perf'] <= fold['reference_perf'] + eps
            if type(fold.get('ok')) is not bool or fold['ok'] != ok:
                raise ValueError('screening constraint evidence is inconsistent')
            passes.append(ok)
        if all(passes):
            eligible.append(lam)
    if lambdas != sorted(set(lambdas)):
        raise ValueError('policy lambdas must be unique and increasing')
    selected = min(eligible) if eligible else None
    if screening.get('selected_lambda') != selected or screening.get('fallback') is not (selected is None):
        raise ValueError('policy selection differs from its constraint evidence')
    if bundle['statistics'].get('provenance', {}).get('sessions') != names:
        raise ValueError('policy statistics and screening sessions differ')
    args = bundle['method_args']
    if not isinstance(args, dict) or set(args) & {'lam', 'profile', 'traces_for_params', 'type_fn', 'item_model', 'cov_rates'}:
        raise ValueError('policy method settings cannot replace selected lambda or frozen statistics')
    return bundle


def seal(bundle):
    bundle = copy.deepcopy(bundle)
    bundle.pop('sha256', None)
    bundle['sha256'] = hashlib.sha256(canonical(bundle).encode()).hexdigest()
    return validate(bundle)


def load(path):
    with open(os.path.expanduser(os.fspath(path)), encoding='utf-8') as stream:
        return validate(json.load(stream))


def save(path, bundle):
    from ctxpress.core.artifacts import atomic_json
    validate(bundle)
    path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    atomic_json(path, bundle)
    return path
