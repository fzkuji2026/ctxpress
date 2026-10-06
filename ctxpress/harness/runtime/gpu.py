"""Explicit NVIDIA device bindings, observed hardware checks and host reservations.

No discovery container, installer or image pull is used. Device UUIDs come from
the reviewed resource manifest and are checked in the actual task container.
"""
from __future__ import annotations
import contextlib, csv, hashlib, io, os, re, sys, tempfile, time
from pathlib import Path

UUID = re.compile(r'GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)


def requirements(environment):
    count = environment.get('gpus', 0)
    types = environment.get('gpu_types')
    if type(count) is not int or count < 0:
        raise ValueError('Harbor GPU count must be a nonnegative integer')
    if types is not None and (not isinstance(types, list) or
            any(not isinstance(name, str) or not name.strip() for name in types)):
        raise ValueError('Harbor gpu_types must be null or a list of nonempty model names')
    # The official Modal backend treats an empty list like unspecified types.
    return dict(count=count, types=[name.strip() for name in types] if types else None)


def device_ids(value, count=None):
    if (not isinstance(value, list) or not value or
            any(not isinstance(item, str) or not UUID.fullmatch(item) for item in value) or
            len({item.casefold() for item in value}) != len(value) or
            count is not None and len(value) != count):
        raise ValueError('bind exactly the declared GPU count to distinct full NVIDIA GPU UUIDs; MIG IDs are not supported')
    return list(value)


def requested_reservation(value, ids):
    """Reject task Compose allocations that conflict with its explicit binding."""
    if not isinstance(value, dict) or set(value) - {'driver','capabilities','count','device_ids'}:
        raise ValueError('Harbor task requests an unsupported GPU allocation')
    if value.get('driver', 'nvidia') != 'nvidia' or value.get('capabilities') != ['gpu']:
        raise ValueError('Harbor GPU allocation requires the NVIDIA gpu capability')
    if 'count' in value and 'device_ids' in value:
        raise ValueError('Harbor GPU allocation cannot specify both count and device_ids')
    if 'count' in value and (type(value['count']) is not int or value['count'] != len(ids)):
        raise ValueError('Harbor Compose GPU count differs from task metadata')
    if 'device_ids' in value and {item.casefold() for item in device_ids(value['device_ids'])} != {item.casefold() for item in ids}:
        raise ValueError('Harbor Compose GPU devices differ from the frozen binding')


def guard(service, ids=None):
    """Only the main task can allocate its pinned devices; other services get none."""
    if service.get('runtime') not in (None, 'runc', 'nvidia'):
        raise ValueError('Harbor task requests an undeclared container runtime')
    deploy = service.get('deploy', {})
    if not isinstance(deploy, dict):
        raise ValueError('Harbor Compose deploy must be a mapping')
    resources = deploy.get('resources', {})
    if not isinstance(resources, dict):
        raise ValueError('Harbor Compose resources must be a mapping')
    reservations = resources.get('reservations', {})
    if not isinstance(reservations, dict):
        raise ValueError('Harbor Compose reservations must be a mapping')
    existing = reservations.get('devices', [])
    if not isinstance(existing, list):
        raise ValueError('Harbor Compose device reservations must be a list')
    short = service.get('gpus')
    if not ids:
        if existing or short is not None or service.get('runtime') == 'nvidia':
            raise ValueError('Harbor service GPU allocation requires an explicit task device binding')
    else:
        ids = device_ids(ids)
        if len(existing) > 1:
            raise ValueError('Harbor task cannot add multiple device allocations')
        for declaration in existing:
            requested_reservation(declaration, ids)
        if short is not None:
            if not isinstance(short, list) or len(short) != 1 or not isinstance(short[0], dict):
                raise ValueError('Harbor task cannot request unbounded GPUs')
            requested_reservation(dict(short[0], capabilities=short[0].get('capabilities', ['gpu'])), ids)
            service.pop('gpus')
        reservations = service.setdefault('deploy', {}).setdefault('resources', {}).setdefault('reservations', {})
        reservations['devices'] = [dict(driver='nvidia', device_ids=ids, capabilities=['gpu'])]
    environment = service.setdefault('environment', {})
    if not isinstance(environment, dict):
        raise ValueError('Harbor resolved service environment must be a mapping')
    # Override image defaults and task env; a daemon with the NVIDIA default
    # runtime must not accidentally grant every GPU to CPU/auxiliary services.
    environment['NVIDIA_VISIBLE_DEVICES'] = ','.join(ids) if ids else 'void'
    service.setdefault('labels', {})['ctxpress.gpu.devices'] = ','.join(ids) if ids else ''


def matches(name, allowed):
    if allowed is None:
        return True
    def token(value):
        return re.search(r'(?<![a-zA-Z0-9])' + re.escape(value) + r'(?![a-zA-Z0-9])', name, re.I)
    for value in allowed:
        if token(value):
            return True
        # Provider capacity aliases such as A100-80GB describe the same device
        # reported by NVML as NVIDIA A100-SXM4-80GB or A100-PCIE-80GB.
        capacity = re.fullmatch(r'([a-zA-Z][a-zA-Z0-9]*)-([0-9]+GB)', value, re.I)
        if capacity and all(token(part) for part in capacity.groups()):
            return True
    return False


def observed(output, ids, types):
    ids = device_ids(ids)
    records = []
    for row in csv.reader(io.StringIO(output)):
        if not row:
            continue
        if len(row) != 4:
            raise ValueError('Harbor GPU hardware query returned invalid columns')
        name, identifier, memory, driver = (value.strip() for value in row)
        if not name or not UUID.fullmatch(identifier) or not memory.isdigit() or int(memory) <= 0 or not driver:
            raise ValueError('Harbor GPU hardware identity is incomplete')
        if not matches(name, types):
            raise ValueError('allocated GPU model does not satisfy the task gpu_types')
        records.append(dict(name=name, uuid=identifier, memory_mb=int(memory), driver_version=driver))
    found = [record['uuid'].casefold() for record in records]
    if len(found) != len(ids) or len(set(found)) != len(found) or set(found) != {item.casefold() for item in ids}:
        raise ValueError('visible GPUs differ from the declared device count/UUID binding')
    return dict(devices=records, count=len(records), required_types=types,
                source='nvidia-smi in the actual task container; CUDA workload performance is not verified by this query')


@contextlib.contextmanager
def reservation(ids, daemon_id, progress=None):
    """Serialize overlapping devices for ctxpress workers on this host/user.

    Each device has a separate advisory lock. Sorted acquisition avoids deadlock
    for multi-GPU tasks and permits jobs on disjoint devices to run in parallel.
    """
    if not ids:
        yield
        return
    device_ids(ids)
    if not sys.platform.startswith('linux') or not isinstance(daemon_id, str) or not daemon_id:
        raise ValueError('Harbor GPU reservations require Linux and a Docker daemon identity')
    import fcntl
    root = Path(tempfile.gettempdir()) / ('ctxpress-gpu-' + str(os.getuid()))
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError('GPU reservation directory is not private to this user')
    identity = hashlib.sha256(daemon_id.encode()).hexdigest()
    descriptors = []
    try:
        if progress:
            progress('waiting')
        for identifier in sorted(item.casefold() for item in ids):
            path = root / (identity + '-' + identifier)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            descriptors.append(descriptor)
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.2)
        if progress:
            progress('acquired')
        yield
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        if progress:
            progress('released')


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
    count = requirements(environment or {})['count']
    if 'verifier_gpu_device_ids' in record or require and count:
        device_ids(record.get('verifier_gpu_device_ids'), count)
    return count
