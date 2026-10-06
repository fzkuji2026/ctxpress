"""Prepare PolyBench's own grading schema without optional imports or downloads."""
from __future__ import annotations
import ast, json, re
from pathlib import Path
from .poly_bench import initial_state
from .swe_bench import rows


def instance(task):
    inputs=[item for item in task['inputs'] if item['role']=='grading' and Path(item['path']).suffix in ('.json','.jsonl')]
    if len(inputs)!=1:raise ValueError('PolyBench requires one frozen instance dataset')
    source=inputs[0];found,digest=rows(Path(source['path']))
    if digest!=source['sha256']:raise ValueError('frozen PolyBench dataset changed')
    selected=[row for row in found if row.get('instance_id')==task['id']]
    if len(selected)!=1 or initial_state(selected[0])!=task['initial_state']:
        raise ValueError('PolyBench grading instance differs from Agent task binding')
    return selected[0]


def test_ids(row,key):
    value=row.get(key)
    if isinstance(value,str):
        try:value=ast.literal_eval(value)
        except (ValueError,SyntaxError):raise ValueError('invalid PolyBench '+key) from None
    if not isinstance(value,list) or any(not isinstance(item,str) or not item for item in value):
        raise ValueError('PolyBench requires official '+key+' test IDs')
    return value


def prepared_instance(task,model):
    row=instance(task)
    for key in ('patch','test_patch','Dockerfile','test_command','modified_nodes'):
        if not isinstance(row.get(key),str) or key in ('test_command','Dockerfile') and not row[key].strip():
            raise ValueError('PolyBench grading requires '+key)
    nodes=json.loads(row['modified_nodes'])
    if not isinstance(nodes,list) or any(not isinstance(node,str) for node in nodes):
        raise ValueError('invalid PolyBench modified_nodes')
    f2p=test_ids(row,'F2P');p2p=test_ids(row,'P2P')
    if not f2p:raise ValueError('PolyBench requires nonempty F2P')
    return model(instance_id=task['id'],model_patch='',patch=row['patch'],test_patch=row['test_patch'],
        repo=row['repo'],base_commit=row['base_commit'],language=row['language'],dockerfile=row['Dockerfile'],
        f2p=f2p,p2p=p2p,test_command=row['test_command'],modified_nodes=nodes)


def requirements(config,tasks,lock):
    if not lock:return ['PolyBench: frozen polybench/dependencies trees, Linux Python and prepared Agent/verifier images']
    missing=[];runtime=lock.get('runtime') or {};version=re.match(r'(\d+)\.(\d+)',runtime.get('version',''))
    if runtime.get('platform')!='linux' or not version or tuple(map(int,version.groups()))<(3,10):
        missing.append('PolyBench requires pinned Linux Python 3.10 or later')
    source=lock['trees'].get('polybench',{}).get('files',{})
    for name in ('pyproject.toml','src/poly_bench_evaluation/__init__.py','src/poly_bench_evaluation/run_evaluation.py',
                 'src/poly_bench_evaluation/polybench_data.py','src/poly_bench_evaluation/docker_utils.py',
                 'src/poly_bench_evaluation/scoring.py','src/poly_bench_evaluation/constants.py',
                 'src/poly_bench_evaluation/parsers/__init__.py'):
        if name not in source:missing.append('PolyBench source tree: '+name)
    dependencies=lock['trees'].get('dependencies',{}).get('files',{})
    if not any(re.fullmatch(r'poly_bench_evaluation-[^/]+\.dist-info/METADATA',name) for name in dependencies):
        missing.append('PolyBench dependencies: real poly_bench_evaluation metadata and full transitive dependencies')
    for task in tasks:
        try:prepared_instance(task,lambda **fields:fields)
        except (ValueError,KeyError) as error:missing.append(str(error)+': '+task['id'])
        resource=lock['tasks'][task['id']]
        if set(resource['images']['grading'])!={'verifier'} or resource['images'].get('services'):
            missing.append('PolyBench requires one Agent and grading.verifier image: '+task['id'])
        if resource.get('gpu_device_ids') or resource.get('verifier_gpu_device_ids') or resource.get('verifier_cap_add'):
            missing.append('PolyBench runner uses unprivileged CPU environments: '+task['id'])
        if task['evaluation']['dataset']['revision']!=lock['release']:
            missing.append('PolyBench resource release must match dataset provenance: '+task['id'])
    return missing
