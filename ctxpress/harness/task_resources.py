"""Task-start resource declarations; no image pull, installer or model invocation."""
from __future__ import annotations
import hashlib, json, re
from pathlib import Path
from ctxpress.harness import eval_environment, eval_plan, eval_trees

SCHEMA = 'ctxpress.eval.task_resources'


def task_digest(task):
    return hashlib.sha256(eval_plan.canonical(task).encode()).hexdigest()


def seal(content):
    return dict(content, sha256=hashlib.sha256(eval_plan.canonical(content).encode()).hexdigest())


def verify(lock, benchmark, selected):
    if not isinstance(lock, dict):
        raise ValueError('invalid task resource manifest')
    content = {key:value for key,value in lock.items() if key != 'sha256'}
    if (lock.get('schema') != SCHEMA or type(lock.get('version')) is not int or lock['version'] != 1 or
            seal(content)['sha256'] != lock.get('sha256') or lock.get('benchmark') != benchmark):
        raise ValueError('task resource manifest changed or belongs to another benchmark')
    if not isinstance(lock.get('release'), str) or not lock['release'].strip():
        raise ValueError('task resources require an explicit benchmark release')
    if not isinstance(lock.get('tasks'), dict) or not isinstance(lock.get('trees'), dict):
        raise ValueError('task resources require task and official input tree bindings')
    if 'native_data_version' in lock:
        from ctxpress.harness import milestone_version
        if benchmark!='swe-milestone' or not isinstance(lock['native_data_version'],dict) or lock['native_data_version'].get('release')!=lock['release']:
            raise ValueError('native Git data version belongs to another benchmark/release')
        milestone_version.validate(lock['native_data_version'])
    for task in selected:
        record = lock['tasks'].get(task['id'])
        if not isinstance(record, dict) or record.get('task_sha256') != task_digest(task):
            raise ValueError('task resources do not bind the selected task data: ' + task['id'])
        if 'gpu_device_ids' in record:
            from ctxpress.benchmarks import harbor_gpu
            count = harbor_gpu.requirements(task['initial_state'].get('environment', {}))['count']
            harbor_gpu.device_ids(record['gpu_device_ids'], count)
        from ctxpress.benchmarks.harbor_protocol import validate_verifier_gpus
        validate_verifier_gpus(task, record)
        validate_verifier_caps(benchmark, record)
        images = record.get('images')
        if not isinstance(images, dict) or 'agent' not in images or 'grading' not in images:
            raise ValueError('declare agent and official grading image identities')
        if not images['grading'] or not isinstance(images['grading'], dict):
            raise ValueError('official grading images must be declared')
        services = images.get('services', {})
        if (not isinstance(services, dict) or any(not isinstance(key, str) or key == 'main' or
                not re.fullmatch(r'[a-zA-Z0-9_-]+', key) for key in services)):
            raise ValueError('declare valid auxiliary service image identities')
        for image in [images['agent'], *images['grading'].values(), *services.values()]:
            if (not isinstance(image, dict) or not isinstance(image.get('id'), str) or
                    not re.fullmatch(r'sha256:[0-9a-f]{64}', image['id'])):
                raise ValueError('task images require full immutable content IDs')
    if lock.get('runtime') is not None:
        runtime = lock['runtime']
        if (not isinstance(runtime, dict) or not isinstance(runtime.get('python'), str) or
                not Path(runtime['python']).is_absolute() or not isinstance(runtime.get('sha256'), str) or
                not re.fullmatch(r'[0-9a-f]{64}', runtime['sha256'])):
            raise ValueError('official runtime requires an absolute Python path and file identity')
    for key, descriptor in lock['trees'].items():
        eval_trees.relative(key)
        eval_environment.verify_tree(descriptor)
    return lock


def read(path, benchmark, selected):
    path = Path(path).resolve()
    digest = eval_plan.file_sha256(path)  # Refuse auth.json before opening it.
    lock = json.loads(path.read_text(encoding='utf-8'))
    return verify(lock, benchmark, selected), digest


def verify_plan(plan):
    lock = plan.get('task_resources')
    if lock is None:
        return
    selected = {job['task']['id']:job['task'] for job in plan['jobs']}
    verify(lock, plan['config']['benchmark'], list(selected.values()))
    for key, tree in lock['trees'].items():
        expected = dict(tree=tree, folder='official-inputs/' + key, environment_key=None)
        if plan.get('input_trees', {}).get('official:' + key) != expected:
            raise ValueError('official resource tree differs from the reviewed input binding')
    for job in plan['jobs']:
        if job.get('resources') != lock['tasks'][job['task']['id']]:
            raise ValueError('job resources differ from the reviewed task binding')


def capture(spec, selected):
    """Inspect declared local images and files only; never create a trial or install tools."""
    import sys
    required = {'schema', 'version', 'benchmark', 'release', 'tasks', 'trees'}
    if (not isinstance(spec, dict) or required-set(spec) or set(spec)-required-{'native_data_version'} or spec.get('schema') != 'ctxpress.eval.task_resource_spec' or
            type(spec.get('version')) is not int or spec['version'] != 1 or
            not isinstance(spec.get('release'), str) or not spec['release'].strip()):
        raise ValueError('invalid task resource capture specification')
    if 'native_data_version' in spec and (spec['benchmark']!='swe-milestone' or spec['native_data_version'] is not True):
        raise ValueError('native_data_version=true applies to SWE-Milestone capture only')
    ids = {task['id'] for task in selected}
    if not ids or len(ids) != len(selected) or not isinstance(spec['tasks'], dict) or set(spec['tasks']) != ids:
        raise ValueError('resource capture must declare exactly the selected task IDs')
    if any(task['benchmark'] != spec['benchmark'] for task in selected):
        raise ValueError('resource capture benchmark differs from the selected tasks')
    if not isinstance(spec['trees'], dict) or not spec['trees']:
        raise ValueError('declare official source and dependency input trees')
    trees = {}
    for key, definition in spec['trees'].items():
        eval_trees.relative(key)
        if (not isinstance(definition, dict) or set(definition) - {'root', 'files', 'directories'} or not {'root','files'} <= set(definition) or
                not isinstance(definition['root'], str) or not isinstance(definition['files'], list) or not definition['files'] or
                not isinstance(definition.get('directories', []), list)):
            raise ValueError('resource trees require an explicit root and file selection')
        trees[key] = eval_trees.capture(definition['root'], definition['files'], folder='official-inputs/' + key, directories=definition.get('directories', []))['tree']
    for task in selected:
        definition = spec['tasks'][task['id']]
        if (not isinstance(definition, dict) or set(definition) - {'agent_image', 'grading_images', 'service_images', 'gpu_device_ids', 'verifier_gpu_device_ids', 'verifier_cap_add'} or
                not {'agent_image','grading_images'} <= set(definition) or
                not isinstance(definition['agent_image'], str) or not definition['agent_image'] or
                not isinstance(definition['grading_images'], dict) or not definition['grading_images'] or
                any(not isinstance(key, str) or not key or not isinstance(reference, str) or not reference
                    for key,reference in definition['grading_images'].items())):
            raise ValueError('declare explicit local agent and grading image references')
        services = definition.get('service_images', {})
        if (not isinstance(services, dict) or any(not isinstance(key, str) or key == 'main' or
                not re.fullmatch(r'[a-zA-Z0-9_-]+', key) or not isinstance(reference, str) or not reference
                for key, reference in services.items())):
            raise ValueError('declare explicit auxiliary service names and image references')
        from ctxpress.benchmarks import harbor_gpu
        count = harbor_gpu.requirements(task['initial_state'].get('environment', {}))['count']
        if count or 'gpu_device_ids' in definition:
            harbor_gpu.device_ids(definition.get('gpu_device_ids'), count)
        from ctxpress.benchmarks.harbor_protocol import validate_verifier_gpus
        validate_verifier_gpus(task, definition, require=True)
        validate_verifier_caps(spec['benchmark'], definition)
    provenance={}
    if spec.get('native_data_version'):
        from ctxpress.harness import milestone_version
        provenance['native_data_version']=milestone_version.capture(selected,spec['release'])
    # All file selections and task declarations are validated before contacting Docker.
    cache = {}
    def image(reference):
        if reference not in cache:
            cache[reference] = eval_environment.image(reference)
        return copy_image(cache[reference])
    def copy_image(record):
        return json.loads(json.dumps(record))
    tasks = {}
    for task in selected:
        definition = spec['tasks'][task['id']]
        tasks[task['id']] = dict(task_sha256=task_digest(task), images=dict(agent=image(definition['agent_image']),
            grading={key:image(reference) for key,reference in definition['grading_images'].items()}))
        if definition.get('service_images'):
            tasks[task['id']]['images']['services'] = {key:image(reference) for key,reference in definition['service_images'].items()}
        if definition.get('gpu_device_ids'):
            tasks[task['id']]['gpu_device_ids'] = list(definition['gpu_device_ids'])
        if definition.get('verifier_gpu_device_ids'):
            tasks[task['id']]['verifier_gpu_device_ids'] = list(definition['verifier_gpu_device_ids'])
        if 'verifier_cap_add' in definition:
            tasks[task['id']]['verifier_cap_add'] = list(definition['verifier_cap_add'])
    runtime = dict(python=str(Path(sys.executable).resolve()),sha256=eval_plan.file_sha256(sys.executable),
                   version=sys.version,platform=sys.platform)
    result = seal(dict(schema=SCHEMA,version=1,benchmark=spec['benchmark'],release=spec['release'],
                       tasks=tasks,trees=trees,runtime=runtime,**provenance,
                       scope='declared task data, selected official files/dependencies, local image IDs and explicit GPU UUIDs when provided; host Python, system libraries and GPU driver remain live'))
    return verify(result,spec['benchmark'],selected)


def validate_verifier_caps(benchmark,record):
    if 'verifier_cap_add' in record and (benchmark not in ('swe-bench','swe-bench-lite','swe-bench-verified') or
            record['verifier_cap_add'] not in ([],['SYS_ADMIN'])):
        raise ValueError('declare supported SWE-bench verifier capabilities explicitly')
