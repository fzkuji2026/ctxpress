"""Materialize experiment-only policy candidates; never start a model or modify a queue."""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
from ctxpress.core import infinite_policy as policy
from ctxpress.core.policy import parameters
from ctxpress.replay.infinite_fit import statistics_bundle, observations


def export(traces, fitted, directory, agent=None):
    fitted=policy.load(fitted)
    root=Path(directory).resolve()
    if root.exists():raise ValueError('candidate directory must be new')
    train_names=set(fitted['statistics']['provenance']['sessions'])
    train=[copy.deepcopy(t) for t in traces if t['name'] in train_names]
    # Bind reloaded histories to original fitting inputs, not merely their names.
    expected=dict(zip(fitted['statistics']['provenance']['sessions'],fitted['statistics']['provenance']['input_sha256']))
    if {t['name'] for t in train}!=train_names or any(policy.digest(t)!=expected[t['name']] for t in train):
        raise ValueError('historical inputs differ from fitted policy')
    params=parameters(fitted['parameters'])
    stats={};bundles=[]
    for i,row in enumerate(fitted['evidence']['candidates']):
        cfg=row['settings'];budget=cfg['summary_tokens']
        if budget not in stats:stats[budget]=statistics_bundle(train,budget)
        candidate=policy.make(cfg,stats[budget],params,observations(train,params),
            fitted['evidence']['training_groups'],max_risk=fitted['constraints']['max_risk'],
            max_replay_silent=fitted['constraints']['max_replay_silent'],agent=agent,version=fitted['version'])
        candidate['evidence'].update(experiment_only=True,parent_fit_sha256=fitted['sha256'],
                                    enable_reason='explicitly exported for controlled experiments, not validated deployment')
        bundles.append((i,policy.seal(candidate)))
    root.mkdir(parents=True)
    methods=[dict(label='keep',**{'class':'NoCompaction'})]
    for i,candidate in bundles:
        path=policy.save(root/f'candidate-{i:03d}.json',candidate)
        for mode in (('off','shadow','control') if agent else ('off',)):
            args=dict(policy=path,agent_mode=mode)
            if mode!='off':args['agent_model']=agent['model']
            methods.append(dict(label=f'candidate-{i:03d}-{mode}',**{'class':'infinite-context'},args=args))
    from ctxpress.core.artifacts import atomic_json
    atomic_json(str(root/'methods.json'),methods)
    return dict(directory=str(root),policies=len(bundles),methods=len(methods),experiments_started=0,
                note='experiment-only; integrate methods into an existing frozen evaluation plan')


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--manifest',required=True);ap.add_argument('--groups',required=True)
    ap.add_argument('--fit',required=True);ap.add_argument('--directory',required=True)
    ap.add_argument('--agent',help='explicit model and same-unit per-token input/output rates JSON')
    args=ap.parse_args(argv)
    from ctxpress.replay.corpus import load_manifest
    traces=copy.deepcopy(load_manifest(args.manifest));groups=json.loads(Path(args.groups).read_text())
    for t in traces:t['group']=groups[t['name']]
    agent=json.loads(Path(args.agent).read_text()) if args.agent else None
    print(json.dumps(export(traces,args.fit,args.directory,agent),ensure_ascii=False))


if __name__=='__main__':main()
