"""Offline leave-one-session-out screening of a portable live cost policy."""
from __future__ import annotations
import copy, hashlib
from concurrent.futures import ProcessPoolExecutor, as_completed
from ctxpress.core import calibration, engine, policy
from ctxpress.core.params import DEFAULT
from ctxpress.methods import CostModel, CodexAutoCompact
from ctxpress.core import metrics


def _statistics(traces, args):
    method = CostModel(traces, **args)
    provenance = dict(method.provenance,
        input_sha256=[hashlib.sha256(policy.canonical(trace).encode()).hexdigest() for trace in traces],
        scope='offline statistics; no real task-quality guarantee')
    return calibration.pack(method.curves, method._train_lengths, method.cov_rates, method.budget, provenance)


def _fold(job):
    traces, index, lambdas, args, params, kind, reference = job
    training = [trace for i, trace in enumerate(traces) if i != index]
    frozen = _statistics(training, args)
    held = traces[index]
    baseline = engine.run(held, CodexAutoCompact(**reference.get('args', {})), params)
    rows = []
    for lam in lambdas:
        result = engine.run(held, CostModel(profile=frozen, lam=lam, **args), params)
        rows.append(dict(lam=lam, session=held['name'], cost=result['cost'], reference_cost=baseline['cost'],
            perf=metrics.perf(result, kind, params.get('harm_other')),
            reference_perf=metrics.perf(baseline, kind, params.get('harm_other')),
            ok=metrics.ok(result, baseline, kind, params.get('eps'), params.get('harm_other'))))
    return index, rows


def tune(traces, path, lambdas, *, params=DEFAULT, method_args=None, kind='strict', reference=None,
         workers=1, on_fold=None):
    traces = list(traces)
    names = [trace.get('name') for trace in traces]
    if len(traces) < 2 or not all(isinstance(n, str) and n for n in names) or len(set(names)) != len(names):
        raise ValueError('tune needs at least two uniquely named historical sessions')
    if any(len(trace.get('reqs', [])) < 2 for trace in traces):
        raise ValueError('historical sessions need at least two requests')
    if kind not in metrics.KINDS or type(workers) is not int or workers < 1:
        raise ValueError('invalid screening proxy or worker count')
    lambdas = sorted(set(calibration._number(value) for value in lambdas))
    if not lambdas:
        raise ValueError('provide a nonempty lambda grid')
    args = copy.deepcopy(method_args or {})
    if set(args) & {'lam', 'profile', 'traces_for_params', 'type_fn', 'item_model', 'cov_rates'}:
        raise ValueError('tune selects lambda and fits statistics itself')
    if isinstance(args.get('harm'), str):
        args['harm'] = copy.deepcopy(metrics.HARM[args['harm']])
    policy.numbers(args)
    reference = copy.deepcopy(reference or {'class':'CodexAutoCompact', 'args':{'t':230000}})
    if reference.get('class') != 'CodexAutoCompact' or set(reference.get('args', {})) - {'t', 'size', 'keep_user'}:
        raise ValueError('choose native Codex compaction as the fallback')
    limit = reference.get('args', {}).get('t', 230000)
    if type(limit) is not int or limit < 1:
        raise ValueError('native fallback threshold must be a positive integer')
    if params.get('window') != limit:
        params = params.override(window=limit)
    CodexAutoCompact(**reference.get('args', {}))
    # Fail before workers start for invalid method arguments or parameter tables.
    statistics = _statistics(traces, args)
    policy.parameters(policy.parameter_rows(params))
    jobs = [(traces, i, lambdas, args, params, kind, reference) for i in range(len(traces))]
    folds = {}
    def record(result):
        index, rows = result
        folds[index] = rows
        if on_fold:
            on_fold(len(folds), len(traces))
    if workers == 1:
        for job in jobs:
            record(_fold(job))
    else:
        with ProcessPoolExecutor(max_workers=min(workers, len(traces))) as pool:
            for future in as_completed([pool.submit(_fold, job) for job in jobs]):
                record(future.result())
    candidates = [dict(lam=lam, folds=[{k:v for k,v in folds[i][j].items() if k != 'lam'}
                                     for i in range(len(traces))]) for j, lam in enumerate(lambdas)]
    chosen = next((row['lam'] for row in candidates if all(fold['ok'] for fold in row['folds'])), None)
    bundle = policy.seal(dict(schema='ctxpress.cost-policy', version=1, statistics=statistics,
        parameters=policy.parameter_rows(params), method_args=args, reference=reference,
        screening=dict(evidence='replay proxy; not real task-quality validation', kind=kind, eps=params.get('eps'),
            sessions=names, candidates=candidates, selected_lambda=chosen, fallback=chosen is None)))
    output = policy.save(path, bundle)
    return dict(policy=output, sha256=bundle['sha256'], sessions=len(traces), folds=len(folds), candidates=len(lambdas),
        selected_lambda=chosen, fallback=chosen is None, evidence=bundle['screening']['evidence'])
