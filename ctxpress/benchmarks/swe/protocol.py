"""Frozen SWE-bench grading contracts without importing optional dependencies."""
from __future__ import annotations
import json, re
from pathlib import Path
from ctxpress.benchmarks.swe.adapter import rows


def api(lock):
    files = (lock or {}).get('trees', {}).get('swebench', {}).get('files', {})
    if 'swebench/harness/test_spec.py' in files or 'swebench/harness/test_spec/test_spec.py' in files:
        return 'legacy'
    if 'swebench/types.py' in files and 'swebench/image_builder/constants.py' in files:
        return 'prepared'
    return None


def instance(task):
    sources = [item for item in task['inputs'] if item['role']=='grading' and Path(item['path']).suffix in ('.json','.jsonl')]
    if len(sources)!=1:
        raise ValueError('SWE-bench requires one frozen instance dataset')
    source=sources[0]
    found,digest=rows(Path(source['path']))
    if digest!=source['sha256']:
        raise ValueError('frozen SWE-bench dataset changed')
    selected=[row for row in found if row.get('instance_id')==task['id']]
    if len(selected)!=1 or any(selected[0].get(field)!=value for field,value in task['initial_state'].items()):
        raise ValueError('grading instance differs from the Agent task binding')
    return selected[0]


def test_ids(row, field):
    value=row.get(field)
    value=json.loads(value) if isinstance(value,str) else value
    if not isinstance(value,list) or any(not isinstance(item,str) or not item for item in value):
        raise ValueError('SWE-bench requires official '+field+' test IDs')
    return value


def validate_row(row, mode):
    for field in ('version','test_patch'):
        if not isinstance(row.get(field),str) or field=='version' and not row[field]:
            raise ValueError('SWE-bench grading requires '+field)
    test_ids(row,'FAIL_TO_PASS');test_ids(row,'PASS_TO_PASS')
    if not test_ids(row,'FAIL_TO_PASS'):
        raise ValueError('SWE-bench requires nonempty FAIL_TO_PASS')
    assets=row.get('image_assets')
    if assets and (json.loads(assets) if isinstance(assets,str) else assets):
        raise ValueError('multimodal grading needs separately frozen local assets; automatic downloads are disabled')
    if mode=='prepared':
        for field in ('image','eval_script','log_parser','eval_type'):
            if not isinstance(row.get(field),str) or not row[field].strip():
                raise ValueError('prepared SWE-bench dataset requires '+field)


def requirements(config,tasks,lock):
    if not lock:return ['SWE-bench: frozen swebench/dependencies trees, Linux Python and prepared Agent/verifier images']
    missing=[];runtime=lock.get('runtime') or {}
    version=re.match(r'(\d+)\.(\d+)',runtime.get('version',''))
    if runtime.get('platform')!='linux' or not version or tuple(map(int,version.groups()))<(3,10):
        missing.append('SWE-bench requires a pinned Linux Python 3.10 or later')
    sources=lock['trees'].get('swebench',{}).get('files',{})
    for name in ('pyproject.toml','swebench/__init__.py','swebench/harness/run_evaluation.py',
                 'swebench/harness/grading.py','swebench/harness/docker_utils.py'):
        if name not in sources:missing.append('SWE-bench source tree: '+name)
    dependencies=lock['trees'].get('dependencies',{}).get('files',{})
    if not any(re.fullmatch(r'swebench-[^/]+\.dist-info/METADATA',name) for name in dependencies):
        missing.append('SWE-bench dependencies: real swebench metadata and full transitive dependencies')
    mode=api(lock)
    if mode is None:missing.append('SWE-bench requires a supported frozen legacy or prepared TestSpec API')
    for task in tasks:
        try:validate_row(instance(task),mode)
        except (ValueError,KeyError) as error:missing.append(str(error)+': '+task['id'])
        record=lock['tasks'][task['id']]
        if set(record['images']['grading'])!={'verifier'} or record['images'].get('services'):
            missing.append('SWE-bench requires one Agent and grading.verifier image per task: '+task['id'])
        if record.get('gpu_device_ids') or record.get('verifier_gpu_device_ids'):
            missing.append('SWE-bench standard task runner uses CPU environments: '+task['id'])
        revision=task['evaluation']['dataset']['revision']
        if revision!=lock['release']:
            missing.append('SWE-bench resource release must match explicit dataset provenance: '+task['id'])
        if mode=='legacy' and task['id']+'.json' not in lock['trees'].get('test_specs',{}).get('files',{}):
            missing.append('SWE-bench legacy TestSpec requires a frozen locally prepared test_specs/'+task['id']+'.json')
        if mode=='prepared' and record.get('verifier_cap_add')!=['SYS_ADMIN']:
            missing.append('prepared SWE-bench official container requires explicit verifier_cap_add=["SYS_ADMIN"]: '+task['id'])
    return missing


def prepared_spec(task,official,mode,module):
    row=instance(task);validate_row(row,mode)
    if mode=='prepared':return module.make_test_spec(row)
    # Importing the author make_test_spec can fetch requirements even with
    # prepared images. Restore its serialized official TestSpec instead.
    import importlib
    name='swebench.harness.test_spec'
    package=importlib.import_module(name)
    if not hasattr(package,'TestSpec'):
        package=importlib.import_module(name+'.test_spec')
    path=Path(official)/'test_specs'/(task['id']+'.json')
    spec=package.TestSpec(**json.loads(path.read_text(encoding='utf-8')))
    if (spec.instance_id!=task['id'] or spec.repo!=row['repo'] or spec.version!=row['version'] or
            spec.FAIL_TO_PASS!=test_ids(row,'FAIL_TO_PASS') or spec.PASS_TO_PASS!=test_ids(row,'PASS_TO_PASS')):
        raise ValueError('prepared official TestSpec differs from frozen instance')
    # Eval-script construction is local and author-owned, unlike environment
    # setup generation. Confirm the captured grading script without fetching.
    builder=getattr(package,'make_eval_script_list',None)
    mapping=getattr(package,'MAP_REPO_VERSION_TO_SPECS',None)
    if not callable(builder) or not isinstance(mapping,dict):
        raise ValueError('legacy runtime lacks local official eval-script construction')
    expected=builder(row,mapping[row['repo']][row['version']],env_name='testbed',repo_directory='/testbed',
        base_commit=row['base_commit'],test_patch=row['test_patch'])
    if spec.eval_script_list!=expected:
        raise ValueError('prepared TestSpec differs from author eval-script construction')
    return spec
