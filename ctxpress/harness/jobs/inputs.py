"""Verified per-run copies of declared evaluation inputs; credentials stay outside."""
from __future__ import annotations
import copy, hashlib, shutil, tempfile
from pathlib import Path
from ctxpress.harness.jobs import plan as eval_plan

SCHEMA = 'ctxpress.eval.inputs'


def _sealed(content):
    return dict(content, sha256=hashlib.sha256(eval_plan.canonical(content).encode()).hexdigest())


def _relative(plan, source, digest):
    path = Path(source)
    env = plan['config']['environment']
    if plan.get('input_trees'):
        from ctxpress.harness.jobs import trees as eval_trees
        relative = eval_trees.destination(plan, source)
        if relative:
            return relative
    if plan.get('grading_snapshot'):
        from ctxpress.benchmarks.milestone import checkpoint_grading as eval_grading
        relative = eval_grading.relative(plan['grading_snapshot'],source)
        if relative:
            return relative
    if plan.get('environment_snapshot'):
        try:
            return (Path('workspace') / path.relative_to(plan['environment_snapshot']['workspace']['root'])).as_posix()
        except ValueError:
            pass
    for key, folder in (('scripts','scripts'),('bindir','bin')):
        if env.get(key):
            try:
                return (Path(folder) / path.relative_to(env[key])).as_posix()
            except ValueError:
                pass
    return 'artifacts/' + digest + path.suffix


def prepare(plan, directory):
    """Publish an all-or-nothing snapshot; reattachment never re-reads sources."""
    directory = Path(directory).resolve()
    if directory.exists():
        return verify(plan, directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix='.inputs-', dir=directory.parent))
    files = {}
    try:
        if plan.get('input_trees'):
            from ctxpress.harness.jobs import trees as eval_trees
            eval_trees.prepare_directories(plan, staged)
        if plan.get('grading_snapshot'):
            from ctxpress.benchmarks.milestone import checkpoint_grading as eval_grading
            for key,descriptor in plan['grading_snapshot']['trees'].items():
                root = staged/'grading'/eval_grading.folder(plan['grading_snapshot'],key)
                root.mkdir(parents=True,exist_ok=True)
                for relative in descriptor['directories']:
                    (root/relative).mkdir(parents=True,exist_ok=True)
        if plan.get('environment_snapshot'):
            workspace = staged / 'workspace'
            workspace.mkdir()
            for relative in plan['environment_snapshot']['workspace']['directories']:
                (workspace / relative).mkdir(parents=True, exist_ok=True)
        for source, digest in plan['artifacts'].items():
            if Path(source).name.lower() == 'auth.json' or Path(source).resolve().name.lower() == 'auth.json':
                raise ValueError('credential files cannot be evaluation input snapshots')
            relative = _relative(plan, source, digest)
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if eval_plan.file_sha256(target) != digest:
                raise ValueError('evaluation input changed during snapshot creation')
            files[source] = dict(path=relative, sha256=digest)
        manifest = _sealed(dict(schema=SCHEMA, version=1, plan_sha256=plan['sha256'], files=files))
        eval_plan.atomic_json(staged / 'manifest.json', manifest)
        verify(plan, staged)
        try:
            staged.rename(directory)
        except OSError:
            if not directory.exists():
                raise
        return verify(plan, directory)
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def verify(plan, directory):
    """Validate bindings and file content; never fall back to a live source."""
    import json
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    content = {key:value for key,value in manifest.items() if key != 'sha256'}
    if (manifest.get('schema') != SCHEMA or manifest.get('version') != 1 or
        _sealed(content)['sha256'] != manifest.get('sha256') or manifest.get('plan_sha256') != plan['sha256']):
        raise ValueError('evaluation input snapshot manifest changed or belongs to another plan')
    files = manifest.get('files', {})
    if set(files) != set(plan['artifacts']):
        raise ValueError('evaluation input snapshot has different source bindings')
    resolved = {}
    for source, digest in plan['artifacts'].items():
        row = files[source]
        relative = Path(row['path'])
        target = (directory / relative).resolve()
        if (row['path'] != _relative(plan, source, digest) or relative.is_absolute() or
            target == directory or directory not in target.parents or
            row['sha256'] != digest or eval_plan.file_sha256(target) != digest):
            raise ValueError('evaluation input snapshot changed')
        resolved[source] = str(target)
    if plan.get('input_trees'):
        from ctxpress.harness.jobs import trees as eval_trees
        eval_trees.verify_copies(plan, directory)
    if plan.get('grading_snapshot'):
        from ctxpress.benchmarks.milestone import checkpoint_grading as eval_grading
        source = plan['config']['environment']['grading']
        if eval_grading.load(resolved[source]) != plan['grading_snapshot']:
            raise ValueError('grading manifest differs from the reviewed plan')
        eval_grading.verify_copies(plan['grading_snapshot'],directory/'grading')
    if plan.get('environment_snapshot'):
        from ctxpress.harness.jobs import environment as eval_environment
        source = plan['config']['environment']['snapshot']
        if eval_environment.load(resolved[source]) != plan['environment_snapshot']:
            raise ValueError('environment manifest differs from the reviewed plan')
        eval_environment.verify_workspace(plan['environment_snapshot'], directory / 'workspace')
    return resolved


def execution(plan, directory):
    """Return effective paths separately from the unchanged approved plan."""
    paths = verify(plan, directory)
    config = copy.deepcopy(plan['config'])
    root = Path(directory).resolve()
    for key, folder in (('scripts','scripts'),('bindir','bin')):
        if config['environment'].get(key):
            config['environment'][key] = str(root / folder)
    if plan.get('environment_snapshot'):
        config['environment']['snapshot'] = paths[config['environment']['snapshot']]
        config['environment']['workspace'] = str(root / 'workspace')
    if plan.get('grading_snapshot'):
        config['environment']['grading'] = paths[config['environment']['grading']]
        config['environment']['grading_root'] = str(root/'grading')
    if plan.get('input_trees'):
        from ctxpress.harness.jobs import trees as eval_trees
        eval_trees.execution(plan, root, config)
    if plan.get('task_resources'):
        config['environment']['resources'] = paths[config['environment']['resources']]
        config['environment']['official_root'] = str(root / 'official-inputs')
    if config['environment'].get('model_catalog'):
        from ctxpress.harness.runtime import codex_catalog
        original = config['environment']['model_catalog']
        catalog = paths[original]
        identity = codex_catalog.inspect(catalog, config['model'], config['reasoning'])
        if dict(identity, path=original) != plan.get('codex_model_catalog'):
            raise ValueError('model catalog identity differs from the frozen plan')
        config['environment']['model_catalog'] = catalog
        codex_catalog.preflight(config['environment']['bindir'], catalog, config['model'], config['reasoning'])
    if config['environment'].get('bindir'):
        from ctxpress.harness.runtime import codex_binary
        # Verify hashes first, then probe only the reviewed copies. Never import
        # a missing companion from the user's original installation.
        codex_binary.preflight(config['environment']['bindir'], bindings=set(paths.values()))
    return config, paths


def method(entry, paths):
    result = copy.deepcopy(entry)
    def visit(value):
        if not isinstance(value, dict):
            return
        args = value.get('args') or {}
        key = {'CostModel':'profile','AutoCostModel':'policy'}.get(value.get('class'))
        if key and args.get(key) and not isinstance(args[key], dict):
            args[key] = paths[args[key]]
        visit(args.get('inner'))
        for child in args.get('methods', []):
            visit(child)
    visit(result)
    return result
