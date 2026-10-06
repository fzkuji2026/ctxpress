"""Read-only inventory for the fixed task-start protocol; no pulls or model calls."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ctxpress import benchmarks
from ctxpress.harness import eval_plan, task_resources

# the SWE-Milestone authors' code checkout
AUTHOR_CODE = Path(os.environ.get('CTXPRESS_MILESTONE_AUTHOR_CODE', '~/swe/SWE-Milestone')).expanduser()


def command(args):
    try:
        process = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return process.stdout.strip() if process.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def inventory(protocol, project):
    data = project.parent/'data'
    binary = Path(shutil.which('codex') or '/absent/codex').resolve()
    locations = {'swe-milestone': data/'swe-milestone', 'swe-bench-verified': data/'swe-verified',
                 'terminal-bench': data/'terminal-bench', 'terminal-bench-science': data/'terminal-bench-science',
                 'deep-swe': data/'deep-swe', 'swe-bench-pro': data/'swe-bench-pro-v2',
                 'swe-polybench': data/'swe-polybench', 'bigcodebench': data/'bigcodebench'}
    result = dict(schema='ctxpress.experiment.inventory', version=1,
                  observed_at=datetime.now(timezone.utc).isoformat(), family_count=len(benchmarks.FAMILIES),
                  code_sha256=eval_plan.fingerprint(), protocol_sha256=hashlib.sha256(eval_plan.canonical(protocol).encode()).hexdigest(),
                  experiments_started=0, downloads_started=0, families=[],
                  docker_version=command(['docker', 'info', '--format', '{{.ServerVersion}}']) if shutil.which('docker') else None,
                  python_dependencies={name: bool(importlib.util.find_spec(name)) for name in
                      ('docker', 'git', 'pydantic', 'harbor', 'pier', 'swebench', 'bigcodebench')},
                  dependency_scope='selected inventory Python only; official runtime and complete dependency capture remain required',
                  codex=dict(binary=str(binary), version=command([str(binary), '--version']) if binary.is_file() else None),
                  milestone_author=dict(path=str(AUTHOR_CODE),
                      commit=command(['git', '-C', str(AUTHOR_CODE), 'rev-parse', 'HEAD'])),
                  milestone_data_commit=command(['git', '-C', str(data/'swe-milestone'), 'rev-parse', 'HEAD']))
    image_rows = command(['docker', 'image', 'ls', '--no-trunc', '--format', '{{json .}}']) if shutil.which('docker') else None
    result['existing_milestone_images'] = [json.loads(row) for row in (image_rows or '').splitlines()
                                           if json.loads(row).get('Repository', '').startswith('swe-milestone/')]
    for family in protocol['families']:
        name = family['benchmark']; root = locations[name]
        row = dict(family=family['family'], benchmark=name, release=family['release'], data=str(root),
                   data_present=root.is_dir(), task_count=None, selected_tasks=family['pilot_tasks'],
                   comparison_tasks=family['comparison_tasks'],
                   prepared_resource_manifest=False, ready_for_real_run=False, missing=[])
        if root.is_dir():
            try:
                tasks = benchmarks.get(name).task_instances(root)
                row['task_count'] = len(tasks)
                selected = {task['id']: task for task in tasks}
                requested = set((family['pilot_tasks'] or []) + (family['comparison_tasks'] or []))
                if requested - set(selected):
                    raise ValueError('declared task IDs are absent from the local dataset: ' + ', '.join(sorted(requested - set(selected))))
                if requested & set(protocol['selection'].get('exclude_task_ids', [])):
                    raise ValueError('evaluation task IDs overlap excluded training tasks')
                row['selected_task_hashes'] = {task_id: task_resources.task_digest(selected[task_id])
                    for task_id in family['pilot_tasks'] or [] if task_id in selected}
                row['comparison_task_hashes'] = {task_id: task_resources.task_digest(selected[task_id])
                    for task_id in family['comparison_tasks'] or []}
                if not family['pilot_tasks']:
                    raise ValueError('freeze exact pilot task IDs before compiling a plan; no implicit all-task selection')
                if name == 'swe-milestone':
                    from ctxpress.benchmarks import milestone_protocol
                    image_refs = {image['Repository'] + ':' + image['Tag'] for image in result['existing_milestone_images']}
                    row['milestone_images'] = {}
                    for task_id in family['pilot_tasks'] or []:
                        task = selected[task_id]
                        active = task['evaluation']['active_milestones']
                        required = {mid: f'swe-milestone/{task_id}__{mid}:{family["release"]}' for mid in active}
                        row['milestone_images'][task_id] = dict(active_milestones=active,
                            existing=[mid for mid, reference in required.items() if reference in image_refs],
                            missing={mid: reference for mid, reference in required.items() if reference not in image_refs},
                            service_milestones=milestone_protocol.service_milestones(task))
                    row['official_source_interface_errors'] = milestone_protocol.contract_errors(str(AUTHOR_CODE))
                cfg = dict(schema='ctxpress.eval', version=1, benchmark=name, start_mode='task_start', scope='benchmark',
                           backend='codex_docker', model=protocol['models']['agent'], reasoning=protocol['models']['agent_reasoning'],
                           tasks=family['pilot_tasks'], methods=protocol['pilot']['methods'], repeats=1, workers=1,
                           run=protocol['run'], environment=dict(data=str(root), bindir=str(binary.parent)))
                resource = project/'runs/prepared/milestone-resources.partial.json'
                if name == 'swe-milestone' and resource.is_file():
                    chosen = [selected[task_id] for task_id in family['pilot_tasks']]
                    lock, _ = task_resources.read(resource, name, chosen)
                    cfg['environment']['resources'] = str(resource)
                    row.update(prepared_resource_manifest=True, resource_manifest=str(resource),
                               resource_sha256=lock['sha256'], official_trees={
                                   key: len(tree['files']) for key, tree in lock['trees'].items()})
                plan = eval_plan.compile_plan(cfg)
                row['missing'] = plan['missing_environment_files']
                row['pilot_plan_sha256'] = plan['sha256']
                row['resource_complete'] = not row['missing']
            except (ValueError, OSError) as error:
                row['missing'].append(str(error))
        else:
            row['missing'] += ['original versioned task dataset', 'freeze exact pilot and comparison task IDs']
        if not row['prepared_resource_manifest']:
            row['missing'] += ['complete official source/dependency snapshot', 'captured immutable task resource manifest']
        row['missing'].append('real task-start execution and official grade are not yet verified')
        result['families'].append(row)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', default='configs/experiment.protocol.json')
    parser.add_argument('--output', default='runs/prepared/inventory.json')
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    source = (project/args.protocol).resolve()
    protocol = json.loads(source.read_text(encoding='utf-8'))
    result = inventory(protocol, project)
    destination = (project/args.output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    eval_plan.atomic_json(destination, result)
    print(json.dumps(dict(output=str(destination), experiments_started=0, downloads_started=0,
                         families=[{key: row[key] for key in ('family', 'task_count', 'ready_for_real_run')} for row in result['families']]),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
