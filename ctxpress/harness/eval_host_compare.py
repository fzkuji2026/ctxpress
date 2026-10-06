"""Observe retained Codex host identity for comparisons; never execute the CLI."""
from __future__ import annotations
import json
from pathlib import Path


def binding(plan, folder, directory, paths, evidence):
    """Bind an explicitly frozen catalog to the actual worker request, read-only."""
    from ctxpress.harness import codex_catalog, eval_task_compare as common
    source = plan['config']['environment'].get('model_catalog')
    if source is None:
        return None
    catalog = common._path(paths[source], directory)
    identity = codex_catalog.inspect(catalog, plan['config']['model'], plan['config']['reasoning'])
    if dict(identity, path=source) != plan.get('codex_model_catalog'):
        raise ValueError('model catalog identity differs from frozen plan')
    common._read(catalog, directory, evidence, expected=identity['sha256'], document=False)
    benchmark = plan['config']['benchmark']
    native = benchmark == 'swe-milestone'
    filename = ('milestone-execute-request.json' if native else
                'harbor-request.json' if benchmark in common.eval_harbor_compare.BENCHMARKS else 'swe-request.json')
    request = common._read(Path(folder) / filename, folder, evidence)
    settings = request['execution'] if native else request
    if (settings.get('model_catalog') != str(catalog) or
            settings.get('model_catalog_sha256') != identity['sha256']):
        raise ValueError('actual Codex model catalog differs from frozen selection')
    return identity


def observe(result, folder, evidence):
    from ctxpress.harness import eval_task_compare as common
    folder = Path(folder).resolve()
    parent = common._path(result['proxy_log'], folder).parent
    roots = (parent, parent / 'agent-logs')
    fallbacks, instructions, cli_logs, sessions = [], set(), [], []
    expected = 'Model metadata for `' + result['model'] + ('` not found. Defaulting to fallback metadata; '
                'this can degrade performance and cause issues.')
    for root in roots:
        if not root.exists():
            continue
        if root != folder:
            common._path(root, folder)
        cli = root / 'codex.txt'
        if cli.exists() or cli.is_symlink():
            cli_logs.append(cli)
        session_root = root / 'sessions'
        if not session_root.exists():
            continue
        common._path(session_root, folder)
        for path in sorted(session_root.rglob('rollout-*.jsonl')):
            raw = common._read(path, folder, evidence, document=False)
            sessions.append(str(path))
            for text in raw.splitlines():
                try:
                    row = json.loads(text)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(row, dict) or row.get('type') != 'session_meta':
                    continue
                payload = row.get('payload')
                if isinstance(payload, dict) and payload.get('base_instructions') is not None:
                    instructions.add(common._digest(payload['base_instructions']))
    native = folder / 'native-execution.json'
    if (result.get('benchmark') or {}).get('name') == 'swe-milestone' and native.exists():
        record = common._read(native, folder, evidence)
        trial = common._path(record['trial'], folder)
        cli = trial / 'log/agent_stdout.txt'
        if cli.exists() or cli.is_symlink():
            cli_logs.append(cli)
    for cli in cli_logs:
        raw = common._read(cli, folder, evidence, document=False)
        for line, text in enumerate(raw.splitlines(), 1):
            try:
                row = json.loads(text)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict):
                continue
            item = row.get('item')
            if (row.get('type') == 'item.completed' and isinstance(item, dict) and
                    item.get('type') == 'error' and item.get('message') == expected):
                fallbacks.append(dict(kind='model_metadata_fallback', model=result['model'],
                                      source=str(cli), line=line))
    return dict(model_metadata_fallbacks=fallbacks, base_instruction_sha256=sorted(instructions),
                cli_logs=[str(p) for p in cli_logs], sessions=sessions,
                scope='Observed client warnings and base instruction identity; missing logs do not prove host equivalence')
