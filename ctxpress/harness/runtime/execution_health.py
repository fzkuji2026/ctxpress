"""Infrastructure evidence from matched runtime tool outputs, never agent prose."""
from __future__ import annotations
import hashlib, json, re
from pathlib import Path
from ctxpress.live.control_receipts import route_failures as _route_failures

_SPAWN_FAILURE = re.compile(r'failed to spawn code-mode host ([^\r\n]*codex-code-mode-host): ([^\r\n]+)')


class ExecutionInfrastructureFailure(RuntimeError):
    """Agent tool infrastructure failed despite a successful model response."""


def observe_rows(rows, *, source=None, sha256=None):
    calls = {}; outputs = set(); failures = []
    for line, row in enumerate(rows, 1):
        if not isinstance(row, dict) or row.get('type') != 'response_item':
            continue
        payload = row.get('payload')
        if not isinstance(payload, dict) or not isinstance(payload.get('call_id'), str):
            continue
        identity = payload['call_id']; kind = payload.get('type')
        if kind in ('custom_tool_call', 'function_call'):
            calls[identity] = payload.get('name')
            continue
        if kind not in ('custom_tool_call_output', 'function_call_output') or identity not in calls:
            continue
        outputs.add(identity)
        if calls[identity] not in ('exec', 'functions.exec', 'wait', 'functions.wait'):
            continue
        output = payload.get('output')
        texts = [output] if isinstance(output, str) else [item.get('text') for item in output
            if isinstance(item, dict) and item.get('type') in ('input_text', 'text')] if isinstance(output, list) else []
        for text in texts:
            matched = _SPAWN_FAILURE.fullmatch(text.strip()) if isinstance(text, str) else None
            if matched:
                failures.append(dict(kind='code_mode_host_spawn_failure', source=source, source_sha256=sha256,
                    line=line, call_id=identity, tool=calls[identity], event_type=kind,
                    companion=matched.group(1), error=matched.group(2)))
                break
    return dict(execution_invalid=bool(failures), tool_calls=len(calls), tool_outputs=len(outputs),
                tool_runtime_failures=failures)


def observe(result):
    """Read only session artifacts adjacent to this attempt's recorded proxy log.

    Missing session logs do not establish either tool health or infrastructure
    failure. Retained evidence also survives removal of ephemeral session files.
    """
    roots = set()
    proxy = result.get('proxy_log')
    if isinstance(proxy, str) and proxy:
        parent = Path(proxy).parent
        roots.update((parent/'sessions', parent/'agent-logs'/'sessions'))
    stored = result.get('execution_health')
    errors = []; files = set()
    for root in sorted(roots):
        if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
            errors.append('session root is a symlink: ' + str(root)); continue
        if not root.is_dir():
            continue
        resolved_root = root.resolve()
        for path in root.rglob('rollout-*.jsonl'):
            resolved = path.resolve()
            if (path.is_symlink() or resolved.name.lower() == 'auth.json' or
                    resolved_root not in resolved.parents or
                    any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents)):
                errors.append('session artifact is an alias or escapes its root: ' + str(path)); continue
            files.add(path)
    # Rescan retained logs when available; don't count stored evidence twice.
    if not files:
        valid = (isinstance(stored, dict) and type(stored.get('execution_invalid')) is bool and
            all(type(stored.get(key)) is int and stored[key] >= 0 for key in ('tool_calls', 'tool_outputs')) and
            isinstance(stored.get('tool_runtime_failures'), list) and
            all(isinstance(row, dict) and row.get('kind') in ('code_mode_host_spawn_failure', 'method_tool_route_failure') and
                isinstance(row.get('call_id'), str) and isinstance(row.get('error'), str)
                for row in stored['tool_runtime_failures']) and
            stored['execution_invalid'] == bool(stored['tool_runtime_failures']))
        if stored is not None and not valid:
            errors.append('stored execution_health is malformed; tool runtime health is unknown')
        failures = list(stored['tool_runtime_failures']) if valid else []
        for failure in _route_failures(result):
            if failure not in failures:
                failures.append(failure)
        return dict(execution_invalid=bool(failures or errors), health_unknown=bool(errors), evidence_errors=errors,
            tool_calls=stored['tool_calls'] if valid else 0, tool_outputs=stored['tool_outputs'] if valid else 0,
            tool_runtime_failures=failures)
    observed = dict(execution_invalid=False, tool_calls=0, tool_outputs=0, tool_runtime_failures=_route_failures(result),
                    health_unknown=False, evidence_errors=errors)
    for path in sorted(files):
        try:
            raw = path.read_bytes()
        except OSError:
            errors.append('session artifact could not be read: ' + str(path)); continue
        rows = []
        for line in raw.splitlines():
            try:
                rows.append(json.loads(line))
            except (ValueError, UnicodeError):
                rows.append(None)  # Keep actual file line numbers, including partial records.
        health = observe_rows(rows, source=str(path.resolve()), sha256=hashlib.sha256(raw).hexdigest())
        observed['tool_calls'] += health['tool_calls']; observed['tool_outputs'] += health['tool_outputs']
        observed['tool_runtime_failures'].extend(health['tool_runtime_failures'])
    observed['health_unknown'] = bool(errors)
    observed['execution_invalid'] = bool(observed['tool_runtime_failures'] or errors)
    return observed


def retain(result):
    """Attach diagnostic evidence to a new result, retaining the official grade."""
    result['execution_health'] = observe(result)
    return result
