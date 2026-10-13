"""Select complete policies using actual matched task outcomes, never replay-generated scores.

Total cost = main + auxiliary. Recovery/repair requests are already inside those
bills and must not be added a second time. External quality tolerance stays fixed.
"""
from __future__ import annotations
import argparse
import copy
import json
import statistics
from pathlib import Path
from ctxpress.core import infinite_policy as policy


def select(policies, rows, *, holdout_groups, quality_tolerance=0):
    policy.number(quality_tolerance, 0, 1)
    policies = {key: policy.load(value) for key, value in policies.items()}
    if not policies or 'keep' in policies:
        raise ValueError('provide named candidate policies; keep is reserved for the reference')
    holdout = set(holdout_groups)
    table, groups = {}, set()
    for row in rows:
        required = {'candidate', 'group', 'task', 'repeat', 'comparison', 'main_cost', 'auxiliary_cost',
                    'total_cost', 'score', 'cost_complete', 'quality_reviewed', 'policy_sha256', 'agent_mode'}
        if not required <= set(row):
            raise ValueError('incomplete measured outcome')
        if row['cost_complete'] is not True or row['quality_reviewed'] is not True:
            raise ValueError('incomplete bills or unreviewed quality cannot train a deployable policy')
        if not all(isinstance(row[k], str) and row[k] for k in ('candidate','group','task','comparison')):
            raise ValueError('group, task and immutable comparison identity required')
        if type(row['repeat']) is not int or row['repeat'] < 0:
            raise ValueError('repeat must be a nonnegative integer')
        for field in ('main_cost','auxiliary_cost','total_cost'):
            policy.number(row[field])
        policy.number(row['score'],0,1)
        if abs(row['total_cost']-row['main_cost']-row['auxiliary_cost']) > 1e-9*max(1,row['total_cost']):
            raise ValueError('total must equal main plus auxiliary, without double-counting recovery')
        candidate=row['candidate']
        if candidate != 'keep':
            if candidate not in policies or policies[candidate]['sha256'] != row['policy_sha256']:
                raise ValueError('outcome not bound to the tested policy')
        if row['agent_mode'] not in ('off','shadow','control'):
            raise ValueError('invalid tested agent mode')
        key=(row['group'],row['task'],row['repeat'],row['comparison'])
        groups.add(row['group'])
        if candidate in table.setdefault(key, {}):
            raise ValueError('duplicate outcome')
        table[key][candidate]=row
    expected=set(policies)|{'keep'}
    if not table or any(set(arms)!=expected for arms in table.values()):
        raise ValueError('every matched task/repeat must include every candidate and keep reference')
    if not holdout or not holdout <= groups or not groups-holdout:
        raise ValueError('separate development and holdout groups required')
    # Frozen statistics may not contain these independent evaluation groups.
    for bundle in policies.values():
        if set(bundle['evidence']['training_groups']) & holdout:
            raise ValueError('holdout leaked into policy training')
    results=[]
    for name,bundle in policies.items():
        modes={arms[name]['agent_mode'] for arms in table.values()}
        if len(modes)!=1:
            raise ValueError('a candidate must use one agent mode across all trials')
        mode=next(iter(modes))
        if bundle['version'] in (3,4) and mode!='control':
            raise ValueError('V3 requires matched outcomes from its actual control strategy')
        if mode!='off' and bundle.get('agent') is None:
            raise ValueError('tested agent mode has no frozen model declaration')
        development=[arms for key,arms in table.items() if key[0] not in holdout]
        ok=all(a[name]['score']+quality_tolerance>=a['keep']['score'] for a in development)
        ratios=[a[name]['total_cost']/max(a['keep']['total_cost'],1e-12) for a in development]
        results.append(dict(candidate=name,agent_mode=mode,eligible=ok,relative_cost=statistics.mean(ratios)))
    feasible=[r for r in results if r['eligible'] and r['relative_cost']<1]
    if not feasible:
        return dict(selected=None,enabled=False,reason='no development candidate improves cost within quality tolerance',candidates=results)
    winner=min(feasible,key=lambda r:r['relative_cost'])
    name=winner['candidate']
    held=[arms for key,arms in table.items() if key[0] in holdout]
    passed=all(a[name]['score']+quality_tolerance>=a['keep']['score'] and
               a[name]['total_cost']<=a['keep']['total_cost'] for a in held)
    frozen=copy.deepcopy(policies[name])
    frozen['evidence']['enabled']=passed
    frozen['measured_selection']=dict(candidate=name,agent_mode=winner['agent_mode'],
        tested_policy_sha256=policies[name]['sha256'],quality_tolerance=quality_tolerance,holdout_groups=sorted(holdout),passed=passed,
        candidates=results,input_sha256=policy.digest(rows),
        comparisons=sorted({r['comparison'] for r in rows}),
        scope='observed matched tasks only; no global optimum or future-quality guarantee')
    return dict(selected=name,enabled=passed,agent_mode=winner['agent_mode'],policy=policy.seal(frozen),
                candidates=results,reason=None if passed else 'selected policy failed untouched holdout')


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input',required=True,help='JSON with policies {candidate: policy path} and outcomes list')
    ap.add_argument('--holdout-group',action='append',required=True)
    ap.add_argument('--quality-tolerance',type=float,default=0)
    ap.add_argument('--output',required=True)
    args=ap.parse_args(argv)
    path=Path(args.input).resolve();data=json.loads(path.read_text())
    if Path(args.output).exists():ap.error('use a new output artifact')
    candidates={k: str(path.parent/v) if isinstance(v,str) else v for k,v in data['policies'].items()}
    result=select(candidates,data['outcomes'],holdout_groups=args.holdout_group,quality_tolerance=args.quality_tolerance)
    from ctxpress.core.artifacts import atomic_json
    destination=Path(args.output).resolve()
    if result.get('policy'):
        deployed=destination.with_name(destination.stem+'.policy.json')
        if deployed.exists():ap.error('deployment policy already exists; use a new output path')
        result['deployment_policy']=policy.save(deployed,result['policy'])
    atomic_json(str(destination),result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('policy','candidates')},ensure_ascii=False))


if __name__=='__main__':main()
