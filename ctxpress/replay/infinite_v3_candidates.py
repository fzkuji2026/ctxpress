"""Export explicit V3 search candidates; no off-mode proxy and no paid execution.

A grid may vary every base and V3 parameter. Search bounds, model and protocol
are declared constraints, not fitted quantities. Actual control histories and
matched task outcomes must be evaluated before infinite-select enables a policy.
"""
import argparse
import copy
import itertools
import json
from pathlib import Path
from ctxpress.core import infinite_policy as policy
from ctxpress.methods.infinite_v3 import BOUNDS, DEFAULTS, validate_config


def export(base, grid, directory):
    base=policy.load(base)
    if not base.get('agent'):raise ValueError('explicit model and pricing required')
    joint_keys=set(base.get('joint_context',{}))-{'models','aux_output_limit'} if base['version']==4 else set()
    allowed=set(policy.BOUNDS)|set(BOUNDS)|joint_keys
    if base['version']==4:
        allowed-={'trigger_ratio','target_ratio','min_gain'}
    if not isinstance(grid,dict) or set(grid)-allowed:raise ValueError('unknown search parameter')
    if any(not isinstance(v,list) or not v for v in grid.values()):raise ValueError('nonempty candidate values required')
    count=1
    for v in grid.values():count*=len(v)
    if count>256:raise ValueError('declare a bounded grid of at most 256 candidates')
    root=Path(directory)
    if root.exists():raise ValueError('new candidate directory required')
    bundles=[]
    for i,values in enumerate(itertools.product(*grid.values())):
        b=copy.deepcopy(base);b.pop('measured_selection',None);b['version']=4 if base['version']==4 else 3
        b['v3']=copy.deepcopy(base.get('v3',DEFAULTS))
        for k,v in zip(grid,values):
            (b['joint_context'] if k in joint_keys else b['settings'] if k in policy.BOUNDS else b['v3'])[k]=v
        policy.settings(b['settings']);validate_config(b['v3'])
        b['evidence'].update(enabled=True,experiment_only=True,agent_evaluated=False,
            parent_fit_sha256=base['sha256'],enable_reason='explicit V3 control experiment; not validated deployment',
            runtime_mode='control',candidate_grid=grid,
            fitted_claim='Prior history calibrates reuse only. V3 behavior requires same-control evaluation and matched outcomes.')
        bundles.append(policy.seal(b))
    root.mkdir(parents=True)
    methods=[]
    for i,b in enumerate(bundles):
        path=policy.save(root/f'candidate-{i:03d}.json',b)
        methods.append({'class':'infinite-context','label':f'infinite-v{b["version"]}-{i:03d}',
                        'args':dict(policy=path,agent_mode='control',agent_model=b['agent']['model'])})
    (root/'methods.json').write_text(json.dumps(methods,indent=2)+'\n')
    return dict(candidates=len(bundles),experiments_started=0,mode='control',directory=str(root.resolve()))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--base',required=True);ap.add_argument('--grid',required=True);ap.add_argument('--directory',required=True)
    a=ap.parse_args();print(json.dumps(export(a.base,json.loads(Path(a.grid).read_text()),a.directory)))


if __name__=='__main__':main()
