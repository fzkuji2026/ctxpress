"""Explicit Harbor phase and frozen runtime API selection, without imports."""
from __future__ import annotations
import copy
from ctxpress.harness.runtime import gpu as harbor_gpu
from ctxpress.harness.runtime.gpu import claimed_gpus, validate_verifier_gpus


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


