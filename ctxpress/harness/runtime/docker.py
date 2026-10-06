"""Docker for owned resources: one checked CLI call, run identities, immutable image IDs and ownership
labels."""
from __future__ import annotations
import re, subprocess


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=60,
                            stdin=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError('managed Harbor Docker operation failed')
    return result.stdout


def identity(project,label):
    if not re.fullmatch(r'ctxp-ms-[0-9a-f]{24}',project) or not isinstance(label,str) or not label:
        raise ValueError('native resources require an explicit run identity')


def image_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',value):
        raise ValueError('native resources require immutable image IDs')
    return value


def labels(project,label,role):
    return {'ctxpress.managed':'true','ctxpress.run':label,'ctxpress.milestone.project':project,'ctxpress.milestone.role':role}
