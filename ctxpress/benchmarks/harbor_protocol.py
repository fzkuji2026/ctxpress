"""Explicit Harbor phase and frozen runtime API selection, without imports."""
from __future__ import annotations
import copy
from . import harbor_gpu


def verifier_environment(config):
    verifier = config.get('verifier', {})
    if not isinstance(verifier, dict):
        raise ValueError('Harbor verifier must be a configuration table')
    mode = verifier.get('environment_mode')
    environment = verifier.get('environment')
    if mode not in (None, 'shared', 'separate') or environment is not None and not isinstance(environment, dict):
        raise ValueError('invalid Harbor verifier environment declaration')
    if mode == 'shared' and environment is not None:
        raise ValueError('shared Harbor verifier cannot declare a separate environment')
    separate = mode == 'separate' or environment is not None
    return copy.deepcopy(environment if environment is not None else config.get('environment', {})) if separate else None


def api(lock):
    files = (lock or {}).get('trees', {}).get('harbor', {}).get('files', {})
    if 'src/harbor/trial/single_step.py' in files:
        return 'modern'
    if 'src/harbor/models/task/verifier_mode.py' in files:
        return 'unsupported'
    return 'legacy'


def claimed_gpus(record):
    """Both phases reserve their resources before a job consumes a worker slot."""
    ids = [*((record or {}).get('gpu_device_ids') or []),
           *((record or {}).get('verifier_gpu_device_ids') or [])]
    unique = {}
    for value in ids:
        unique.setdefault(value.casefold(), value)
    return list(unique.values())


def validate_verifier_gpus(task, record, *, require=False):
    environment = task['initial_state'].get('verifier_environment')
    if 'verifier_gpu_device_ids' in record and environment is None:
        raise ValueError('verifier GPU binding requires a separate verifier environment')
    count = harbor_gpu.requirements(environment or {})['count']
    if 'verifier_gpu_device_ids' in record or require and count:
        harbor_gpu.device_ids(record.get('verifier_gpu_device_ids'), count)
    return count
