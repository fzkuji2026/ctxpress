"""Local image identities and complete auxiliary workspace declarations."""
from __future__ import annotations
import hashlib, json, re, subprocess
from pathlib import Path
from ctxpress.harness.jobs import plan as eval_plan

SCHEMA = 'ctxpress.eval.environment'


def _seal(content):
    return dict(content, sha256=hashlib.sha256(eval_plan.canonical(content).encode()).hexdigest())


def image(reference):
    """Inspect an existing image only; never pull or start a container."""
    process = subprocess.run(['docker','image','inspect',reference], check=True, capture_output=True, text=True)
    rows = json.loads(process.stdout)
    if len(rows) != 1 or not re.fullmatch(r'sha256:[0-9a-f]{64}', rows[0].get('Id','')):
        raise ValueError('Docker did not establish a unique image content ID')
    row = rows[0]
    return dict(reference=reference, id=row['Id'], os=row.get('Os'), architecture=row.get('Architecture'),
                repo_digests=sorted(row.get('RepoDigests') or []))


def workspace(root):
    """Declare every directory and regular file; reject symlinks and credentials."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError('auxiliary workspace must be an existing directory')
    files, directories, executables = {}, [], []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('workspace snapshots do not support symbolic links')
        if path.name.lower() == 'auth.json':
            raise ValueError('credential files cannot be workspace snapshots')
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            directories.append(relative)
        elif path.is_file():
            files[relative] = eval_plan.file_sha256(path)
            if path.stat().st_mode & 0o111:
                executables.append(relative)
        else:
            raise ValueError('workspace snapshots require regular files and directories')
    return dict(root=str(root), files=files, directories=directories, executables=executables)


def capture(root, base_image, coordinates):
    points = {}
    for n, j in coordinates:
        if type(n) is not int or type(j) is not int or n < 0 or j < 1 or f'{n}:{j}' in points:
            raise ValueError('provide unique nonnegative n and positive j coordinates')
        points[f'{n}:{j}'] = image(f'hsnap-{n}:{j}')
    if not points:
        raise ValueError('provide at least one boundary image')
    return _seal(dict(schema=SCHEMA, version=1, workspace=workspace(root),
                      images=dict(base=image(base_image), boundaries=points)))


def _relative(value):
    if not isinstance(value,str):
        raise ValueError('invalid workspace relative path')
    path = Path(value)
    if not value or path.is_absolute() or path.as_posix() != value or '..' in path.parts or value == '.':
        raise ValueError('invalid workspace relative path')
    if path.name.lower() == 'auth.json':
        raise ValueError('credential files cannot be workspace snapshots')
    return path


def verify(lock, coordinates=()):
    content = {key:value for key,value in lock.items() if key != 'sha256'}
    if lock.get('schema') != SCHEMA or type(lock.get('version')) is not int or lock.get('version') != 1 or _seal(content)['sha256'] != lock.get('sha256'):
        raise ValueError('evaluation environment manifest changed or is unsupported')
    images = lock['images']
    for record in [images['base'], *images['boundaries'].values()]:
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',record['id']):
            raise ValueError('environment images require full content IDs')
    for n,j in coordinates:
        if f'{n}:{j}' not in images['boundaries']:
            raise ValueError('environment manifest is missing a selected boundary image')
    verify_tree(lock['workspace'])
    return lock


def verify_tree(tree):
    """Validate a complete file/directory declaration without reading its source."""
    if not Path(tree['root']).is_absolute():
        raise ValueError('workspace manifest requires an absolute source root')
    directories = tree['directories']
    if len(directories) != len(set(directories)):
        raise ValueError('duplicate workspace directory')
    for relative in [*directories, *tree['files']]:
        path = _relative(relative)
        if relative in directories and relative in tree['files']:
            raise ValueError('workspace path is both a file and directory')
        if path.parent != Path('.') and path.parent.as_posix() not in directories:
            raise ValueError('workspace manifest is missing a parent directory')
    for digest in tree['files'].values():
        if not re.fullmatch(r'[0-9a-f]{64}',digest):
            raise ValueError('workspace files require full hashes')
    executables = tree['executables']
    if len(executables) != len(set(executables)) or set(executables) - set(tree['files']):
        raise ValueError('workspace executable declarations must refer to unique files')
    return tree


def read(path, coordinates=()):
    path = Path(path).expanduser().resolve()
    if path.name.lower() == 'auth.json':
        raise ValueError('credential files cannot be environment manifests')
    raw = path.read_bytes()
    return verify(json.loads(raw), coordinates), hashlib.sha256(raw).hexdigest()


def load(path, coordinates=()):
    return read(path, coordinates)[0]


def verify_workspace(lock, root):
    verify(lock)
    actual = workspace(root)
    if not same_tree(actual, lock['workspace']):
        raise ValueError('auxiliary workspace differs from the frozen environment manifest')


def same_tree(actual, expected):
    """Compare contents and permissions independently of path enumeration order."""
    # Captures sort POSIX strings; workspace traversal sorts Path components.
    # Sorted lists preserve multiplicity while accepting either recorded order.
    return (actual['files'] == expected['files'] and
            all(sorted(actual[key]) == sorted(expected[key]) for key in ('directories', 'executables')))


def runtime(lock, root, n, j):
    """Use immutable IDs even if original tags have since moved."""
    verify(lock, [(n,j)])
    verify_workspace(lock, root)
    base = lock['images']['base']['id']
    boundary = lock['images']['boundaries'][f'{n}:{j}']['id']
    if image(base)['id'] != base or image(boundary)['id'] != boundary:
        raise ValueError('declared image content is unavailable locally')
    return base, boundary
