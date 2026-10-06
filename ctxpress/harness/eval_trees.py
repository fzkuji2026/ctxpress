"""Declared input trees for task-start benchmarks; original layouts survive freezing."""
from __future__ import annotations
from pathlib import Path, PurePosixPath
from ctxpress.harness import eval_environment, eval_plan


def relative(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value or
            PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts or
            PurePosixPath(value).as_posix() != value or value == '.'):
        raise ValueError('invalid input tree relative path')
    return Path(value)


def capture(root, files, *, folder, environment_key=None, directories=()):
    """Select regular files without following any intermediate symbolic links."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError('input tree requires an existing directory')
    relative(folder)
    declared_directories = list(directories)
    selected, directories, executables = {}, set(), []
    for name in files:
        name = relative(name).as_posix()
        path = root / name
        if any(part.is_symlink() for part in [path, *path.parents] if part != root and root in part.parents):
            raise ValueError('input trees cannot contain symbolic links')
        if not path.is_file():
            raise FileNotFoundError(path)
        selected[name] = eval_plan.file_sha256(path)
        directories.update(parent.as_posix() for parent in Path(name).parents if parent != Path('.'))
        if path.stat().st_mode & 0o111:
            executables.append(name)
    for name in declared_directories:
        name = relative(name).as_posix()
        path = root / name
        if any(part.is_symlink() for part in [path, *path.parents] if part != root and root in part.parents):
            raise ValueError('input trees cannot contain symbolic links')
        if not path.is_dir():
            raise ValueError('declared input directory is unavailable')
        directories.add(name)
        directories.update(parent.as_posix() for parent in Path(name).parents if parent != Path('.'))
    tree = dict(root=str(root), files=selected, directories=sorted(directories), executables=sorted(set(executables)))
    return dict(tree=eval_environment.verify_tree(tree), folder=folder, environment_key=environment_key)


def verify(plan):
    """Check tree bindings without reading original sources, including on resume."""
    descriptors = plan.get('input_trees', {})
    if not isinstance(descriptors, dict):
        raise ValueError('invalid input tree declarations')
    roots, folders, keys, sources = [], [], set(), set()
    for descriptor in descriptors.values():
        if not isinstance(descriptor, dict) or set(descriptor) != {'tree', 'folder', 'environment_key'}:
            raise ValueError('invalid input tree descriptor')
        tree = eval_environment.verify_tree(descriptor['tree'])
        root, folder = Path(tree['root']), relative(descriptor['folder'])
        for prior in roots:
            if root == prior or root in prior.parents or prior in root.parents:
                raise ValueError('input tree source roots overlap')
        for prior in folders:
            if folder == prior or folder in prior.parents or prior in folder.parents:
                raise ValueError('input tree snapshot folders overlap')
        # These names are owned by the legacy snapshot implementation.
        if folder.parts[0] in ('bin', 'scripts', 'grading', 'workspace', 'artifacts', 'manifest.json'):
            raise ValueError('input tree snapshot folder is reserved')
        roots.append(root); folders.append(folder)
        key = descriptor['environment_key']
        if key is not None:
            if (not isinstance(key, str) or key in keys or plan['config']['environment'].get(key) != str(root)):
                raise ValueError('input tree environment root is not bound to the plan')
            keys.add(key)
        for name, digest in tree['files'].items():
            relative(name)
            source = str(root / name)
            if source in sources or plan['artifacts'].get(source) != digest:
                raise ValueError('input tree file is not bound to plan artifacts')
            sources.add(source)
    return descriptors


def destination(plan, source):
    for descriptor in plan.get('input_trees', {}).values():
        tree = descriptor['tree']
        try:
            name = Path(source).relative_to(tree['root']).as_posix()
        except ValueError:
            continue
        if name in tree['files']:
            return (Path(descriptor['folder']) / name).as_posix()
    return None


def prepare_directories(plan, directory):
    for descriptor in verify(plan).values():
        root = Path(directory) / descriptor['folder']
        root.mkdir(parents=True, exist_ok=True)
        for name in descriptor['tree']['directories']:
            (root / relative(name)).mkdir(parents=True, exist_ok=True)


def verify_copies(plan, directory):
    for descriptor in verify(plan).values():
        actual = eval_environment.workspace(Path(directory) / descriptor['folder'])
        if not eval_environment.same_tree(actual, descriptor['tree']):
            raise ValueError('frozen benchmark input tree changed: ' + descriptor['folder'])


def execution(plan, directory, config):
    for descriptor in verify(plan).values():
        if descriptor['environment_key'] is not None:
            config['environment'][descriptor['environment_key']] = str(Path(directory).resolve() / descriptor['folder'])
    return config
