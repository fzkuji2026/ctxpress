"""Portable task identity and start-state contract; benchmark data stays in adapters."""
from __future__ import annotations
import copy

MODES = ('checkpoint', 'task_start')


def validate(task):
    if not isinstance(task, dict) or set(task) - {'id', 'benchmark', 'start_mode', 'initial_state', 'inputs', 'evaluation'}:
        raise ValueError('invalid task instance')
    for key in ('id', 'benchmark'):
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise ValueError(f'task {key} must be explicit')
    if task.get('start_mode') not in MODES:
        raise ValueError('task start_mode must be checkpoint or task_start')
    if not isinstance(task.get('initial_state'), dict) or not task['initial_state']:
        raise ValueError('task initial_state must be declared')
    if (not isinstance(task.get('evaluation'), dict) or
            not isinstance(task['evaluation'].get('kind'), str) or not task['evaluation']['kind']):
        raise ValueError('task evaluation kind must be declared')
    state = task['initial_state']
    if task['start_mode'] == 'checkpoint':
        point = state.get('recorded_boundary')
        if (set(state) != {'recorded_boundary'} or not isinstance(point, dict) or
                set(point) != {'id', 'n', 'j', 'context_tokens'} or point['id'] != task['id'] or
                type(point['n']) is not int or point['n'] < 0 or type(point['j']) is not int or point['j'] <= 0 or
                type(point['context_tokens']) is not int or point['context_tokens'] <= 0):
            raise ValueError('checkpoint task requires an explicit recorded boundary')
    elif 'recorded_boundary' in state:
        raise ValueError('task-start state cannot contain a recorded boundary')
    if not isinstance(task.get('inputs'), list):
        raise ValueError('task inputs must be a list')
    for item in task['inputs']:
        if (not isinstance(item, dict) or set(item) != {'role', 'path', 'sha256'} or
                item['role'] not in ('task', 'runtime', 'grading') or
                not isinstance(item['path'], str) or not item['path'] or
                not isinstance(item['sha256'], str) or len(item['sha256']) != 64 or
                any(char not in '0123456789abcdef' for char in item['sha256'])):
            raise ValueError('task inputs require a role, file path and SHA-256')
    return task


def checkpoint(point, benchmark, inputs=()):
    return validate(dict(id=point['id'], benchmark=benchmark, start_mode='checkpoint',
                         initial_state=dict(recorded_boundary=copy.deepcopy(point)), inputs=list(inputs),
                         evaluation=dict(kind='official-milestone')))


def from_job(job, benchmark):
    """Old frozen jobs retain their original declaration; no new evidence is inferred."""
    if job.get('task') is not None:
        task = validate(job['task'])
        if task['benchmark'] != benchmark:
            raise ValueError('task benchmark differs from plan')
        if 'boundary' in job and (task['start_mode'] != 'checkpoint' or task['initial_state'].get('recorded_boundary') != job['boundary']):
            raise ValueError('task and legacy boundary declarations disagree')
        return task
    if 'boundary' in job:
        return checkpoint(job['boundary'], benchmark)
    raise ValueError('job has no task instance')


def remap(task, paths):
    """Only declared file inputs change paths; task IDs and opaque state stay stable."""
    result = copy.deepcopy(validate(task))
    for item in result['inputs']:
        item['path'] = paths[item['path']]
    return result


def verify_remap(original, moved):
    """Only input paths may change when an approved task enters its snapshot."""
    validate(original);validate(moved)
    normalized=copy.deepcopy(moved)
    if len(normalized['inputs'])!=len(original['inputs']):
        raise ValueError('remapped task input selection changed')
    for actual,expected in zip(normalized['inputs'],original['inputs']):
        actual['path']=expected['path']
    if normalized!=original:
        raise ValueError('remapped task identity, start state or grading protocol changed')
    return moved


def identity(job):
    return job['task']['id'] if job.get('task') else job['boundary']['id']
