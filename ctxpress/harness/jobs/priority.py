"""Operational dispatch order, separate from frozen experiment specifications."""
import json
import time


def ordered(rows, record, plan_sha256):
    if not record:
        return rows
    if record['plan_sha256'] != plan_sha256:
        raise ValueError('dispatch priority belongs to another plan')
    labels = record['methods']
    if len(labels) != len(set(labels)):
        raise ValueError('duplicate method labels in dispatch priority')
    ranks = {label: index for index, label in enumerate(labels)}
    return sorted(rows, key=lambda row: (
        ranks[json.loads(row['spec'])['method']['label']], row['id']))


def set_order(directory, methods, reason):
    from .control import _plan
    from .queue import database
    plan = _plan(directory)
    expected = {job['method']['label'] for job in plan['jobs']}
    if len(methods) != len(set(methods)) or set(methods) != expected:
        raise ValueError('priority must name every frozen method label exactly once')
    if not reason.strip():
        raise ValueError('dispatch priority requires a reason')
    record = dict(plan_sha256=plan['sha256'], methods=list(methods),
                  reason=reason, updated=time.time())
    with database(directory) as connection:
        connection.execute('BEGIN IMMEDIATE')
        previous = connection.execute("SELECT value FROM metadata WHERE key='dispatch_priority'").fetchone()
        history = connection.execute("SELECT value FROM metadata WHERE key='dispatch_priority_history'").fetchone()
        entries = json.loads(history[0]) if history else []
        if previous:
            entries.append(json.loads(previous[0]))
        connection.execute("INSERT OR REPLACE INTO metadata VALUES ('dispatch_priority_history',?)", (json.dumps(entries),))
        connection.execute("INSERT OR REPLACE INTO metadata VALUES ('dispatch_priority',?)", (json.dumps(record),))
    return record
