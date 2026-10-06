"""Bind each MCP control invocation to an observed direct call/output pair.

Native Code Mode inventories lose completeness when arguments exceed 8 KiB.
Receipts record control execution independently of that best-effort inventory.
Execution receipts persist digests and random identities. Verified control history
contains method arguments so its state can be replayed after an MCP restart.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

MARKER = re.compile(r'<ctxpress-control-receipt:([0-9a-f]{32}):([0-9a-f]{32})>')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                   separators=(',', ':')).encode()).hexdigest()


class ControlReceipts:
    def __init__(self, store, log=None):
        self.store = Path(store).absolute()
        self.log = log
        self.observed = set()
        self.run = self.store.name
        if not re.fullmatch('[0-9a-f]{32}', self.run):
            raise ValueError('control receipts require a launch-specific store')

    def path(self, run, event, kind):
        if not all(re.fullmatch('[0-9a-f]{32}', x) for x in (run, event)):
            raise ValueError('invalid control receipt identity')
        path = self.store.parent/run/'control-receipts'/kind/(event+'.json')
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('control receipt path is a symlink')
        return path

    def record(self, kind, **fields):
        from ctxpress.live.logging import append_record
        append_record(self.log, dict(type=kind, t=time.time(), **fields))

    def issue(self, name, arguments, text, failed):
        event = uuid.uuid4().hex
        row = dict(name=name, arguments=digest(arguments), output=digest(text), failed=bool(failed))
        path = self.path(self.run, event, 'events')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as out:
            json.dump(row, out)
        receipt = self.run+':'+event
        # Written before returning the MCP result, including results whose text
        # a nested JS caller discards. A killed launcher still retains issuance.
        self.record('method_tool_receipt_issued', receipt=receipt)
        return '<ctxpress-control-receipt:'+receipt+'>'

    def consume(self, run, event, call, output):
        path = self.path(run, event, 'events')
        row = json.loads(path.read_text(encoding='utf-8'))
        args = call.get('arguments')
        args = json.loads(args) if isinstance(args, str) else args
        if row['name'] != call['name'] or row['arguments'] != digest(args) or row['output'] != digest(output):
            raise ValueError('control receipt does not match call/output')
        binding = dict(call_id=call['call_id'], name=call['name'], arguments=row['arguments'])
        claim = self.path(run, event, 'claims')
        claim.parent.mkdir(parents=True, exist_ok=True)
        # Proxy request threads serialize consume under MethodTools.lock.
        # Claims survive a CLI restart; a replay must retain its original call id.
        if claim.exists():
            if json.loads(claim.read_text(encoding='utf-8')) != binding:
                raise ValueError('control receipt reused for a different call')
        else:
            with claim.open('x', encoding='utf-8') as out:
                json.dump(binding, out)
        receipt = run+':'+event
        if receipt not in self.observed:
            self.record('method_tool_receipt_observed', receipt=receipt)
            self.observed.add(receipt)
        return receipt

    def audit(self):
        folder = self.store/'control-receipts'/'events'
        issued = [self.run+':'+p.stem for p in folder.glob('*.json')]
        self.record('method_tool_receipt_audit', run=self.run, issued=issued)

    def inventory(self, call, output):
        """Restore only native observations bound to exactly the same call/result.

        Codex rollouts omit nested inventories. Store them before forwarding so a
        new proxy can recover categories on resume without parsing JavaScript.
        """
        fields = ('cell_id', 'executed_tool_calls', 'tool_calls_complete')
        binding = dict(call={k: call[k] for k in ('type', 'call_id', 'name', 'namespace', 'input', 'arguments') if k in call},
                       output={k: output[k] for k in ('type', 'call_id', 'output') if k in output})
        path = self.store.parent/'control-inventories'/(digest(binding)+'.json')
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('control inventory path is a symlink')
        metadata = output.get('internal_chat_message_metadata_passthrough') or {}
        if not isinstance(metadata, dict):
            return False
        current = {k: metadata[k] for k in fields if k in metadata}
        if ('executed_tool_calls' in current or current.get('tool_calls_complete') is True):
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                temporary = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
                try:
                    temporary.write_text(json.dumps(current), encoding='utf-8')
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            return False
        if current or not path.exists():
            return False
        saved = json.loads(path.read_text(encoding='utf-8'))
        output['internal_chat_message_metadata_passthrough'] = dict(metadata, **copy.deepcopy(saved))
        return True

    def publish_history(self, history):
        from ctxpress.core.artifacts import atomic_json
        path = self.store/'control-receipts'/'history.json'
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('control history path is a symlink')
        atomic_json(path, history)

    def history(self):
        path = self.store/'control-receipts'/'history.json'
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('control history path is a symlink')
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def route_failures(result):
    rows = [row for row in result.get('rewrites', []) if isinstance(row, dict)]
    failures = [dict(kind='method_tool_route_failure', call_id='',
                     error=row.get('reason', 'invalid method tool route'))
                for row in rows if row.get('type') == 'method_tool_route_failed']
    failures.extend(dict(kind='host_contract_failure', call_id='', error=row.get('reason', 'host contract failed'))
                    for row in rows if row.get('type') == 'host_contract_failed')
    issued, observed = set(), set()
    for row in rows:
        if row.get('type') == 'method_tool_receipt_issued':
            issued.add(row['receipt'])
        elif row.get('type') == 'method_tool_receipt_observed':
            observed.add(row['receipt'])
        elif row.get('type') == 'method_tool_receipt_audit':
            issued.update(row.get('issued', []))
    for receipt in sorted(issued - observed):
        failures.append(dict(kind='method_tool_route_failure', call_id=receipt,
            error='MCP control invocation has no verified direct call/output boundary'))
    latest = {}
    for row in rows:
        if row.get('method_tool_route'):
            latest[row.get('session', 'default')] = row
    for row in latest.values():
        for cell in row.get('method_tool_pending_cells', []):
            failures.append(dict(kind='method_tool_route_failure', call_id=cell,
                error='Code Mode cell ended without a complete method tool inventory'))
    return failures
