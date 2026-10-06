"""Freeze authentic data HEAD/tag objects for the author's read-only version gate.

Dataset files are bound separately by task hashes. This records Git identity and
dirty-worktree status; it does not claim that selected files equal a clean tree.
No other history, blobs, remote configuration or Git hooks are copied.
"""
from __future__ import annotations
import base64, contextlib, hashlib, os, re, subprocess, zlib
from pathlib import Path
from ctxpress.harness.jobs import trees as eval_trees

SCHEMA = 'ctxpress.eval.milestone_data_version'
OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')


def release_ref(release):
    if not isinstance(release, str) or not re.fullmatch(r'v\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9._-]+)?', release):
        raise ValueError('native data version requires an explicit release tag')
    return 'refs/tags/' + release


def git(path, *args):
    result = subprocess.run(['git', '-C', str(path), *args], capture_output=True, check=True, timeout=30)
    return result.stdout


def capture(tasks, release):
    """Read the actual source checkout before freezing inputs or inspecting images."""
    ref = release_ref(release)
    if not tasks:
        raise ValueError('native Git version capture requires selected tasks')
    first = Path(tasks[0]['initial_state']['workspace']).resolve()
    root = Path(git(first, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
    head = git(first, 'rev-parse', 'HEAD^{commit}').decode().strip()
    tag = git(first, 'rev-parse', ref).decode().strip()
    if git(first, 'rev-parse', ref + '^{commit}').decode().strip() != head:
        raise ValueError('native dataset HEAD differs from its release tag')
    for task in tasks:
        workspace = Path(task['initial_state']['workspace']).resolve()
        if (task['benchmark'] != 'swe-milestone' or root not in (workspace, *workspace.parents) or
                git(workspace, 'rev-parse', '--show-toplevel').decode().strip() != str(root) or
                git(workspace, 'rev-parse', 'HEAD^{commit}').decode().strip() != head):
            raise ValueError('selected native itineraries must share their declared Git checkout')
    objects = {}
    pending = [head, tag]
    while pending:
        oid = pending.pop()
        if oid in objects:
            continue
        if len(objects) >= 33 or not OID.fullmatch(oid):
            raise ValueError('native Git tag chain is invalid or too deep')
        kind = git(root, 'cat-file', '-t', oid).decode().strip()
        if kind not in ('commit', 'tag'):
            raise ValueError('native version objects must resolve to a commit')
        raw = git(root, 'cat-file', kind, oid)
        if len(raw) > 1024 * 1024:
            raise ValueError('native version object is oversized')
        objects[oid] = dict(type=kind, data=base64.b64encode(raw).decode('ascii'))
        if kind == 'tag':
            pending.append(raw.splitlines()[0].removeprefix(b'object ').decode('ascii'))
    status = git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=normal')
    # Recheck after the object/status reads; a moving checkout is not a snapshot.
    if (git(root, 'rev-parse', 'HEAD^{commit}').decode().strip() != head or
            git(root, 'rev-parse', ref).decode().strip() != tag):
        raise ValueError('native Git version changed during capture')
    result = dict(schema=SCHEMA, version=1, release=release, head=head, tag=tag, objects=objects,
                  source_dirty=bool(status), source_status_sha256=hashlib.sha256(status).hexdigest())
    validate(result)
    return result


def validate(record):
    keys = {'schema', 'version', 'release', 'head', 'tag', 'objects', 'source_dirty', 'source_status_sha256'}
    if (not isinstance(record, dict) or set(record) != keys or record.get('schema') != SCHEMA or
            type(record.get('version')) is not int or record['version'] != 1):
        raise ValueError('invalid native Git version evidence')
    release_ref(record['release'])
    head, tag, objects = record['head'], record['tag'], record['objects']
    if (not isinstance(head, str) or not OID.fullmatch(head) or not isinstance(tag, str) or
            not OID.fullmatch(tag) or len(head) != len(tag) or type(record['source_dirty']) is not bool or
            not isinstance(record['source_status_sha256'], str) or
            not re.fullmatch('[0-9a-f]{64}', record['source_status_sha256']) or
            not isinstance(objects, dict) or not 1 <= len(objects) <= 33):
        raise ValueError('invalid native Git version object bindings')
    decoded = {}
    for oid, item in objects.items():
        if (not isinstance(oid, str) or not OID.fullmatch(oid) or len(oid) != len(head) or
                not isinstance(item, dict) or set(item) != {'type', 'data'} or item['type'] not in ('tag', 'commit') or
                not isinstance(item['data'], str) or len(item['data']) > 1400000):
            raise ValueError('invalid native Git version object')
        try:
            raw = base64.b64decode(item['data'], validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError('invalid native Git object encoding') from error
        wire = item['type'].encode() + b' ' + str(len(raw)).encode() + b'\0' + raw
        algorithm = 'sha1' if len(oid) == 40 else 'sha256'
        if len(raw) > 1024 * 1024 or hashlib.new(algorithm, wire).hexdigest() != oid:
            raise ValueError('native Git object differs from its object ID')
        decoded[oid] = (item['type'], raw, wire)
    if head not in decoded or decoded[head][0] != 'commit':
        raise ValueError('native data HEAD requires its authentic commit object')
    seen = set()
    current = tag
    while current != head:
        if current in seen or current not in decoded or decoded[current][0] != 'tag':
            raise ValueError('native release tag does not resolve to its data HEAD')
        seen.add(current)
        lines = decoded[current][1].splitlines()
        try:
            if not lines[0].startswith(b'object ') or lines[1] not in (b'type tag', b'type commit'):
                raise ValueError('native tag object header is invalid')
            target = lines[0][7:].decode('ascii')
            if target not in decoded or lines[1][5:].decode('ascii') != decoded[target][0]:
                raise ValueError('native tag target type is invalid')
            current = target
        except (IndexError, UnicodeError) as error:
            raise ValueError('native tag object header is invalid') from error
    if set(decoded) != seen | {head}:
        raise ValueError('native version evidence contains unrelated Git objects')
    return decoded


def materialize(record, directory):
    """Create a minimal, detached Git database; no hooks/config from the source."""
    decoded = validate(record)
    directory = Path(directory).resolve()
    target = directory / '.git'
    if not directory.is_dir() or target.exists() or target.is_symlink():
        raise ValueError('native data version staging requires a fresh Git directory')
    target.mkdir()
    (target / 'objects').mkdir(); (target / 'refs').mkdir()
    (target / 'HEAD').write_text(record['head'] + '\n', encoding='ascii')
    sha256 = len(record['head']) == 64
    config = '[core]\n\trepositoryformatversion = ' + ('1' if sha256 else '0') + '\n\tbare = false\n'
    if sha256:
        config += '[extensions]\n\tobjectFormat = sha256\n'
    (target / 'config').write_text(config, encoding='ascii')
    reference = target / eval_trees.relative(release_ref(record['release']))
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_text(record['tag'] + '\n', encoding='ascii')
    for oid, (_, _, wire) in decoded.items():
        path = target / 'objects' / oid[:2] / oid[2:]
        path.parent.mkdir(exist_ok=True); path.write_bytes(zlib.compress(wire))
    return target


@contextlib.contextmanager
def pinned_environment(release):
    release_ref(release)
    keys = ('SWE_MILESTONE_IMAGE_TAG', 'SWE_MILESTONE_DATA_VERSION_CHECK')
    previous = {key: os.environ.get(key) for key in keys}
    os.environ[keys[0]] = release; os.environ.pop(keys[1], None)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
