"""Joint behavioral-parameter search with grouped validation and an untouched holdout.

Replay ranks only implemented deterministic operators. Agent content and causal
recovery need actual branch outcomes; those are never fabricated from a fixed trace.
"""
from __future__ import annotations
import argparse
import copy
import json
import random
import statistics
import math
from pathlib import Path
from ctxpress.core import calibration, engine, infinite_policy as policy, reuse
from ctxpress.core.params import DEFAULT, P
from ctxpress.methods.infinite_context import InfiniteContext
from ctxpress.methods.native import NoCompaction


def observations(traces, params):
    outputs, repeats = [], []
    for tr in traces:
        seen = set()
        for q in tr['reqs']:
            outputs.append(q.get('out_tokens', 0))
            paths = {p for s in q['before'] if s['seg'] == 'call' for p in s.get('res', [])}
            if paths & seen:
                tokens = q.get('input', 0)
                cached = min(tokens, q.get('cached', 0))
                repeats.append(cached*params.get('cached')+(tokens-cached)*params.get('write')+
                               q.get('out_tokens', 0)*params.get('out'))
            seen |= paths
    return dict(extra_output_tokens=statistics.mean(outputs) if outputs else 0,
                repeat_request_cost=statistics.mean(repeats) if repeats else 0,
                repeat_requests=len(repeats),
                note='observed same-path requests; association, not compression-caused recovery')


def statistics_bundle(traces, budget):
    curves = reuse.Curves(traces)
    coverage = reuse.coverage_rates(traces, budget)
    return calibration.pack(curves, [len(t['reqs']) for t in traces], coverage, budget,
        dict(sessions=[t['name'] for t in traces], groups=sorted({t['group'] for t in traces}),
             input_sha256=[policy.digest(t) for t in traces], scope='training groups only'))


def search_space(traces, capacity, trials=32, seed=0, version=1):
    if type(trials) is not int or trials < 1:
        raise ValueError('trials must be positive')
    # Data-derived scales use development histories only, never the outer holdout.
    sizes = sorted(s['size'] for t in traces for q in t['reqs'] for s in q['before'] if s['seg'] == 'out')
    lengths = sorted(len(t['reqs']) for t in traces)
    if not sizes:
        raise ValueError('no tool-output history')
    budget = max(32, min(4096, int(statistics.median(sizes)/4)))
    rhythm = max(1, min(16, int(statistics.median(lengths)**.5)))
    grid = dict(trigger_ratio=[.4, .7, .9], target_ratio=[.2, .35], work_turns=[0, 1, 3, 6],
        cooldown=sorted({0, rhythm, min(64, rhythm*2)}), batch_size=[1, 4, 16],
        summary_tokens=sorted({budget, min(8192, budget*2)}),
        evidence_tokens=sorted({max(256, budget*4), min(65536, max(256, budget*16))}),
        horizon_scale=[.5, 1., 2.], risk_scale=[1., 2., 4.], prior_strength=[4, 16, 64], min_gain=[0., .1, .5])
    if version == 2:
        # Evidence must cover typical large records; summary size is selected
        # independently from the old median-output/4 heuristic.
        p90 = sizes[max(0, math.ceil(.9*len(sizes))-1)]
        evidence = max(1024, min(32768, int(p90)))
        grid.update(trigger_ratio=[.4, .6, .75], target_ratio=[.2, .35],
                    batch_size=[4, 8, 16], summary_tokens=[128, 256, 512],
                    evidence_tokens=sorted({evidence, min(65536,evidence*2)}))
    rng = random.Random(seed)
    rows, seen = [], set()
    # All knobs are searchable; finite budget does not imply a global optimum.
    while len(rows) < trials:
        cfg = {k: rng.choice(v) for k, v in grid.items()}
        key = policy.digest(cfg)
        if key not in seen:
            seen.add(key); rows.append(policy.settings(cfg))
    return rows


def _evaluate(trace, bundle):
    from ctxpress.core.metrics import perf
    method = InfiniteContext(policy=bundle)
    accounted = copy.deepcopy(trace)
    catalog_tokens = len(json.dumps(method.agent_tools, ensure_ascii=False))/4
    accounted['prefix'] += catalog_tokens
    result = engine.run(accounted, method)
    # Replay's recorded outputs are identical across arms; include them explicitly in the total.
    output_cost = sum(q.get('out_tokens', 0) for q in trace['reqs'][:-1])*method.parameters.get('out')
    return dict(session=trace['name'], group=trace['group'],
        added_tool_catalog_estimated_tokens=catalog_tokens,
        cost=result.get('cost', 0)+output_cost, replay_silent=perf(result, 'strict'),
        recovery=result.get('recover', 0), reexplore=result.get('reexplore', 0),
        changed=result.get('infinite_compacted_records', 0),
        unresolved_capacity=result.get('infinite_capacity_unresolved', 0))


def fit(traces, *, params, holdout_groups, candidates=None, trials=32, seed=0,
        max_risk=.02, max_replay_silent=0, agent=None, progress=None, version=1):
    traces = copy.deepcopy(list(traces))
    names = [t.get('name') for t in traces]
    if len(names) != len(set(names)) or any(not n for n in names):
        raise ValueError('unique session names required')
    if any(len(t.get('reqs', [])) < 2 or not t.get('group') for t in traces):
        raise ValueError('each session needs at least two requests and an explicit project/task group')
    # Identical history cannot appear in different folds under a renamed session.
    signatures = [policy.digest(t['reqs']) for t in traces]
    if len(signatures) != len(set(signatures)):
        raise ValueError('duplicate histories across sessions')
    holdout = set(holdout_groups)
    groups = {t['group'] for t in traces}
    if not holdout or not holdout <= groups or len(groups-holdout) < 2:
        raise ValueError('need two development groups and at least one separate holdout group')
    development = [t for t in traces if t['group'] not in holdout]
    test = [t for t in traces if t['group'] in holdout]
    candidates = candidates or search_space(development, params.get('window'), trials, seed, version=version)
    candidates = [policy.settings(c) for c in candidates]
    if not candidates:
        raise ValueError('empty search')
    for k in ('cached', 'write', 'out'):
        policy.number(params.get(k))
    policy.number(max_risk, 0, 1); policy.number(max_replay_silent)
    cache = {}
    def bundle(train, cfg):
        key = (tuple(t['name'] for t in train), cfg['summary_tokens'])
        if key not in cache:
            cache[key] = statistics_bundle(train, cfg['summary_tokens'])
        observed = observations(train, params)
        fitted_params = params.override(extra_out=observed['extra_output_tokens'])
        fitted_params.extra_out = P(observed['extra_output_tokens'], 'fitted', 'Mean output tokens of training requests; recovery proxy, not causal measurement')
        return policy.make(cfg, cache[key], fitted_params, observed,
            [t['group'] for t in train], max_risk=max_risk,
            max_replay_silent=max_replay_silent, agent=agent, version=version)
    baselines = {}
    for t in traces:
        result = engine.run(t, NoCompaction(), params)
        out = sum(q.get('out_tokens', 0) for q in t['reqs'][:-1])*params.get('out')
        baselines[t['name']] = result['cost']+out
    rows = []
    for index, cfg in enumerate(candidates):
        folds = []
        for group in sorted(groups-holdout):
            train = [t for t in development if t['group'] != group]
            frozen = bundle(train, cfg)
            for t in development:
                if t['group'] == group:
                    row = _evaluate(t, frozen)
                    row['reference_cost'] = baselines[t['name']]
                    folds.append(row)
        eligible = all(r['replay_silent'] <= max_replay_silent and not r['unresolved_capacity'] for r in folds)
        rows.append(dict(settings=cfg, folds=folds, eligible=eligible,
                         relative_cost=statistics.mean(r['cost']/max(r['reference_cost'], 1e-12) for r in folds)))
        if progress:
            progress(index+1, len(candidates))
    eligible = [r for r in rows if r['eligible'] and r['relative_cost'] < 1]
    selected = min(eligible, key=lambda r: r['relative_cost']) if eligible else rows[0]
    frozen = bundle(development, selected['settings'])
    # Holdout is evaluated once for the selected policy. No retuning or refit with it.
    held = [_evaluate(t, frozen) for t in test]
    for row in held:
        row['reference_cost'] = baselines[row['session']]
    passes = bool(eligible) and sum(r['cost'] for r in held) < sum(r['reference_cost'] for r in held) and all(r['replay_silent'] <= max_replay_silent and not r['unresolved_capacity']
                                  and r['cost'] <= r['reference_cost'] for r in held)
    frozen['evidence'].update(holdout_groups=sorted(holdout), enabled=passes,
        candidates=rows, parameter_identifiability={k: ('requires real agent trials' if k == 'evidence_tokens' else 'bounded replay search') for k in policy.BOUNDS}, selected_by='minimum grouped-development mean relative total replay cost',
        holdout=held, seed=seed, agent_evaluated=False,
        not_identified=['task quality', 'causal recovery/repair cost', 'agent summary quality',
                        'agent-only evidence_tokens optimum'],
        fallback_reason=None if passes else 'no validated cost improvement; keep original history')
    return policy.seal(frozen)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--groups', required=True, help='JSON map session name -> project/task group; related checkpoints must share a group')
    ap.add_argument('--holdout-group', action='append', required=True)
    ap.add_argument('--params', required=True, help='JSON file explicitly declaring cached/write/out prices and window')
    ap.add_argument('--agent', help='optional JSON: model, input/output per-token rates, unit same_as_main_per_token')
    ap.add_argument('--output', required=True)
    ap.add_argument('--trials', type=int, default=32)
    ap.add_argument('--version', type=int, choices=(1,2), default=1)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--max-risk', type=float, default=.02)
    ap.add_argument('--max-replay-silent', type=float, default=0)
    args = ap.parse_args(argv)
    from ctxpress.replay.corpus import load_manifest
    traces = copy.deepcopy(load_manifest(args.manifest))
    grouping = json.loads(Path(args.groups).read_text())
    if set(grouping) != {t['name'] for t in traces}:
        ap.error('group map must cover exactly every loaded session')
    for t in traces:
        t['group'] = grouping[t['name']]
    values = json.loads(Path(args.params).read_text())
    if not {'cached', 'write', 'out', 'window'} <= set(values):
        ap.error('explicit prices and capacity required')
    destination = Path(args.output)
    if destination.exists():
        ap.error('output already exists; use a new versioned artifact')
    import sys
    params = DEFAULT.override(values)
    for key in ('cached', 'write', 'out'):
        setattr(params, key, P(values[key], 'price', f'Explicit per-token price file sha256={policy.digest(values)}'))
    params.window = P(values['window'], 'constraint', 'Externally declared capacity; never optimized')
    result = fit(traces, params=params, holdout_groups=args.holdout_group,
        trials=args.trials, seed=args.seed, version=args.version, max_risk=args.max_risk, max_replay_silent=args.max_replay_silent,
        agent=json.loads(Path(args.agent).read_text()) if args.agent else None,
        progress=lambda n, total: print(f'infinite-context fit: {n}/{total}', file=sys.stderr, flush=True))
    path = policy.save(destination, result)
    print(json.dumps(dict(policy=path, enabled=result['evidence']['enabled'],
        settings=result['settings'], holdout=result['evidence']['holdout'],
        limitations=result['evidence']['not_identified']), ensure_ascii=False))


if __name__ == '__main__':
    main()
