"""Read repository itineraries from official SWE-Milestone data, without running code."""
from __future__ import annotations
import csv, json
from graphlib import TopologicalSorter
from pathlib import Path
from ctxpress.harness.jobs import plan as eval_plan, trees as eval_trees
from ctxpress.harness.jobs.task import validate
from ctxpress.benchmarks.fresh import dataset_file as _file


def _ids(path):
    return [line.strip() for line in path.read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


def itinerary(workspace):
    root = Path(workspace).expanduser().resolve()
    metadata = json.loads(_file(root, 'metadata.json').read_text(encoding='utf-8'))
    if not isinstance(metadata.get('repo_name'), str) or not metadata['repo_name']:
        raise ValueError('dataset metadata must declare repo_name')
    inputs = []; seen = set(); directories = set()
    def add(relative, role):
        path = _file(root, relative)
        if str(path) not in seen:
            inputs.append(dict(role=role, path=str(path), sha256=eval_plan.file_sha256(path)))
            seen.add(str(path))
        return path
    def grading_tree(relative):
        directory = root / relative
        if not directory.exists():return
        if directory.is_symlink():raise ValueError('grading inputs cannot use symbolic directories')
        directories.add(relative)
        for path in sorted(directory.rglob('*')):
            name = path.relative_to(root).as_posix()
            eval_trees.relative(name)
            if path.is_symlink():raise ValueError('grading inputs cannot use symbolic links')
            if path.is_dir():directories.add(name)
            elif path.is_file():add(name, 'grading')
            else:raise ValueError('grading inputs must be regular files/directories')
    add('metadata.json', 'runtime')
    with add('milestones.csv', 'runtime').open(encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    ids = [row.get('id') for row in rows]
    if not ids or any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise ValueError('milestone catalog requires unique IDs')
    selected = root / 'selected_milestone_ids.txt'
    active = _ids(add(selected.name, 'runtime')) if selected.exists() else ids
    if not active or len(set(active)) != len(active) or not set(active) <= set(ids):
        raise ValueError('invalid active milestone selection')
    ungraded_file = root / 'non-graded_milestone_ids.txt'
    ungraded = set(_ids(add(ungraded_file.name, 'runtime'))) if ungraded_file.exists() else set()
    if not ungraded <= set(ids):
        raise ValueError('unknown non-graded milestone IDs')
    edges = set()
    for name in ('dependencies.csv', 'additional_dependencies.csv'):
        if name == 'additional_dependencies.csv' and not (root / name).exists():
            continue
        with add(name, 'runtime').open(encoding='utf-8', newline='') as stream:
            for row in csv.DictReader(stream):
                source, target = row.get('source_id'), row.get('target_id')
                if isinstance(source, str) and source.startswith('#'):
                    continue  # Official DAG loader treats these rows as comments.
                if source not in ids or target not in ids or source == target:
                    raise ValueError(f'{root.name}/{name}: dependency edge references invalid milestones')
                if source in active and target in active:
                    edges.add((source, target))
    predecessors = {value:set() for value in active}
    for source, target in edges:
        predecessors[target].add(source)
    tuple(TopologicalSorter(predecessors).static_order())  # Validate; official runner chooses its own order.
    graded = [value for value in active if value not in ungraded]
    if (root / 'e2e_config.yaml').exists():add('e2e_config.yaml', 'runtime')
    if (root / 'dataset_manifest.json').exists():add('dataset_manifest.json', 'runtime')
    # Native resolution prefers data-root config over the code checkout. Keep
    # that exact selected sibling input, even when data points at one workspace.
    sibling_config_sha256 = None
    config = root.parent / 'config' / (root.name + '.yaml')
    if config.exists() or config.is_symlink():
        path = _file(root.parent, 'config/' + root.name + '.yaml')
        sibling_config_sha256 = eval_plan.file_sha256(path)
        inputs.append(dict(role='grading', path=str(path), sha256=sibling_config_sha256))
    for value in active:
        if Path(value).name != value or value in ('.', '..') or '\\' in value:
            raise ValueError('milestone ID must be a directory name')
        add(f'srs/{value}/SRS.md', 'task')
        if value in graded:
            add(f'dockerfiles/{value}/test_config.json', 'grading')
            add(f'test_results/{value}/{value}_classification.json', 'grading')
            optional = f'test_results/{value}/{value}_filter_list.json'
            if (root / optional).exists():
                add(optional, 'grading')
        else:
            # The native watcher still processes submissions for ungraded DAG
            # nodes. Keep optional author inputs without turning them into
            # quality denominators in ctxpress.
            for name in (f'test_results/{value}/{value}_classification.json',
                         f'test_results/{value}/{value}_filter_list.json'):
                if (root/name).exists():add(name,'grading')
        grading_tree(f'dockerfiles/{value}')
    # Shared test and post-snapshot scripts are grading-only. Historical trials,
    # cloned repositories, caches and unrelated workspaces are not selected.
    for name in ('scripts', 'tests', 'grading_assets'):
        grading_tree(name)
    state=dict(repo=metadata['repo_name'], base_ref=metadata.get('base_tag'),
                                            workspace=str(root), input_directories=sorted(directories),
                                            evidence='dataset metadata; execution image not yet bound')
    if sibling_config_sha256:state['sibling_repo_config_sha256']=sibling_config_sha256
    task = validate(dict(id=root.name, benchmark='swe-milestone', start_mode='task_start',
                         initial_state=state,
                         inputs=inputs, evaluation=dict(kind='official-milestone-trial',
                         active_milestones=active, graded_milestones=graded, dependencies=[list(edge) for edge in sorted(edges)])))
    return task


def extra_input_trees(selected, root):
    """A single-workspace selection can freeze its sibling config separately."""
    files={}
    for task in selected:
        for item in task['inputs']:
            path=Path(item['path'])
            if root not in path.parents:
                if path!=Path(task['initial_state']['workspace']).parent/'config'/(task['id']+'.yaml'):
                    raise ValueError('itinerary input escapes dataset/config roots')
                files[path.name]=path
    if not files:return {}
    parents={path.parent for path in files.values()}
    if len(parents)!=1:raise ValueError('native sibling configurations require one declared config root')
    return {'dataset-config':eval_trees.capture(parents.pop(),sorted(files),folder='dataset-config')}


def data_directories(selected, root):
    result = set()
    for task in selected:
        workspace = Path(task['initial_state']['workspace'])
        for name in task['initial_state'].get('input_directories', []):
            result.add((workspace / eval_trees.relative(name)).relative_to(root).as_posix())
    return sorted(result)


def tasks(data):
    root = Path(data).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    workspaces = [root] if (root / 'metadata.json').is_file() else [path for path in sorted(root.iterdir()) if path.is_dir() and (path / 'metadata.json').is_file()]
    if not workspaces:
        raise ValueError('no SWE-Milestone repository workspaces found')
    return [itinerary(path) for path in workspaces]
