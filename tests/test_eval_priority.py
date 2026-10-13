import json
import sqlite3
import pytest
from ctxpress.harness.jobs import priority, control, queue


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    specs = [{'id': f'j{i}', 'method': {'label': label}} for i, label in enumerate(['A', 'B', 'C'])]
    plan = {'sha256': 'frozen', 'jobs': specs}
    monkeypatch.setattr(control, '_plan', lambda directory: plan)
    with queue.database(tmp_path) as c:
        c.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
        c.execute('CREATE TABLE jobs (id TEXT, spec TEXT, status TEXT, attempt INTEGER)')
        for i, spec in enumerate(specs):
            c.execute('INSERT INTO jobs VALUES (?,?,?,?)', (spec['id'], json.dumps(spec), 'running' if i == 0 else 'pending', 1 if i == 0 else 0))
    return tmp_path


def test_priority_preserves_running_job_and_specs(prepared):
    with queue.database(prepared) as c:
        before = [tuple(r) for r in c.execute('SELECT * FROM jobs')]
    record = priority.set_order(prepared, ['C', 'B', 'A'], 'publication evidence')
    with queue.database(prepared) as c:
        assert [tuple(r) for r in c.execute('SELECT * FROM jobs')] == before
        pending = list(c.execute("SELECT * FROM jobs WHERE status='pending'"))
    assert [r['id'] for r in priority.ordered(pending, record, 'frozen')] == ['j2', 'j1']
    assert [r['id'] for r in queue._runnable(priority.ordered(pending, record, 'frozen'), [], 1)] == ['j2']
    with pytest.raises(ValueError, match='another plan'):
        priority.ordered(pending, record, 'different')


@pytest.mark.parametrize('labels', [['A', 'B'], ['A', 'B', 'D'], ['A', 'A', 'C']])
def test_reject_incomplete_or_changed_cohort(prepared, labels):
    with pytest.raises(ValueError):
        priority.set_order(prepared, labels, 'evidence')
    with queue.database(prepared) as c:
        assert c.execute('SELECT count(*) FROM metadata').fetchone()[0] == 0


def test_history_keeps_previous_order(prepared):
    first = priority.set_order(prepared, ['B', 'A', 'C'], 'first evidence')
    priority.set_order(prepared, ['C', 'B', 'A'], 'updated evidence')
    with queue.database(prepared) as c:
        assert json.loads(c.execute("SELECT value FROM metadata WHERE key='dispatch_priority_history'").fetchone()[0]) == [first]
