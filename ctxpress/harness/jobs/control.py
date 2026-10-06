"""Stop dispatch before cancelling, recovering or resuming owned evaluation jobs."""
from __future__ import annotations
import contextlib, json, os, time, uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from ctxpress import benchmarks
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.core import processes


def read(connection):
    row = connection.execute("SELECT value FROM metadata WHERE key='control'").fetchone()
    return json.loads(row[0]) if row else {}


def stopped(connection):
    return read(connection).get('dispatch_stopped') is True


def _plan(directory):
    from ctxpress.harness.jobs.queue import database
    plan = eval_plan.load(Path(directory) / 'plan.json', check_inputs=False)
    with database(directory) as connection:
        stored = json.loads(connection.execute("SELECT value FROM metadata WHERE key='plan'").fetchone()[0])
        if stored != plan:
            raise ValueError('evaluation database and plan differ')
        expected = {job['id']: job for job in plan['jobs']}
        actual = {row['id']: json.loads(row['spec']) for row in connection.execute('SELECT id,spec FROM jobs')}
        if actual != expected:
            raise ValueError('evaluation job identities differ from the frozen plan')
    return plan


@contextlib.contextmanager
def _operation(directory, operation):
    from ctxpress.harness.jobs.queue import database
    owner = dict(pid=os.getpid(), identity=processes.identity(os.getpid()), token=uuid.uuid4().hex)
    with database(directory) as connection:
        connection.execute('BEGIN IMMEDIATE')
        previous = read(connection).get('owner')
        if previous and processes.alive(previous['pid'], previous['identity']):
            raise RuntimeError('another evaluation control operation is still running')
        record = dict(dispatch_stopped=True, phase=operation, requested=time.time(), owner=owner)
        connection.execute("INSERT OR REPLACE INTO metadata VALUES ('control',?)", (json.dumps(record),))
        connection.execute("UPDATE jobs SET status='cancelled',finished=?,error='dispatch stopped by user' WHERE status='pending'",
                           (time.time(),))
    try:
        yield record
    except BaseException as error:
        record.update(phase='cleanup_failed', error=type(error).__name__)
        raise
    finally:
        record.pop('owner', None)
        record['updated'] = time.time()
        with database(directory) as connection:
            if read(connection).get('owner') == owner:
                connection.execute("UPDATE metadata SET value=? WHERE key='control'", (json.dumps(record),))


def _scheduler_stopped(directory, timeout):
    from ctxpress.harness.jobs.queue import state
    deadline = time.monotonic() + timeout
    while True:
        scheduler = state(directory)['scheduler']
        if not scheduler or not scheduler['alive']:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('scheduler has not acknowledged stopped dispatch; retain resources')
        time.sleep(.05)


def recover_attempt(adapter, folder, label):
    folder = Path(folder)
    journals = sorted(folder.glob('resources-*.json'))
    if folder.is_symlink() or any(path.is_symlink() for path in journals):
        raise ValueError('evaluation recovery paths must not be symlinks')
    if any(path.name.startswith('resources-milestone-') for path in journals):
        if adapter.describe()['name'] != 'swe-milestone':
            raise ValueError('native resource journal belongs to a different benchmark')
        adapter.recover_native(folder, label)
        return len(journals)
    # All current two-phase adapters use an Agent journal and a verifier journal.
    # Credential-bearing resources must disappear before verifier/network cleanup.
    def order(path):
        record = json.loads(path.read_text(encoding='utf-8'))
        agent = record.get('role') == 'agent' or path.stem.endswith('-agent')
        # Legacy Harbor Agent journals have a private channel but no role field.
        agent = agent or record.get('role') is None and bool(record.get('channel'))
        return (0 if agent else 1, path.name)
    for path in sorted(journals, key=order):
        adapter.recover(path, label)
    return len(journals)


def _recover(directory, plan):
    from ctxpress.harness.jobs.queue import database
    with database(directory) as connection:
        rows = [dict(row) for row in connection.execute('SELECT id,pid,identity FROM jobs')]
    if any(row['pid'] and processes.alive(row['pid'], row['identity']) for row in rows):
        raise RuntimeError('evaluation workers are still alive; cancel them before recovery')
    adapter = benchmarks.get(plan['config'].get('benchmark', benchmarks.DEFAULT))
    count = 0
    if (Path(directory) / 'jobs').is_symlink():
        raise ValueError('evaluation jobs directory must not be a symlink')
    for job in plan['jobs']:
        root = Path(directory) / 'jobs' / job['id']
        if root.is_symlink():
            raise ValueError('evaluation job directory must not be a symlink')
        for folder in sorted(root.glob('attempt-*')):
            if not folder.is_dir() or not folder.name.removeprefix('attempt-').isdigit():
                raise ValueError('invalid evaluation attempt directory')
            count += recover_attempt(adapter, folder, plan['sha256'][:12] + '-' + job['id'])
    with database(directory) as connection:
        connection.execute("UPDATE jobs SET status='interrupted',finished=?,error='worker exited without a terminal result' WHERE status='running'",
                           (time.time(),))
    return count


def recover(directory, timeout=60):
    from ctxpress.harness.jobs.queue import state
    eval_plan._positive(timeout, 'control timeout', integer=False)
    directory = Path(directory).resolve(); plan = _plan(directory)
    with _operation(directory, 'recovering') as control:
        _scheduler_stopped(directory, timeout)
        count = _recover(directory, plan)
        control.update(phase='recovered', recovered_journals=count)
    return dict(state(directory), recovered_journals=count)


def cancel(directory, timeout=60):
    from ctxpress.harness.jobs.queue import database, state
    eval_plan._positive(timeout, 'control timeout', integer=False)
    directory = Path(directory).resolve(); plan = _plan(directory)
    with _operation(directory, 'cancelling') as control:
        _scheduler_stopped(directory, timeout)
        with database(directory) as connection:
            rows = [dict(row) for row in connection.execute('SELECT id,pid,identity FROM jobs')]
        live = [row for row in rows if row['pid'] and processes.alive(row['pid'], row['identity'])]
        with ThreadPoolExecutor(max_workers=max(1, min(16, len(live)))) as pool:
            futures = [pool.submit(processes.stop_owned, row['pid'], row['identity'], timeout) for row in live]
            failures = []
            for future in futures:
                try:
                    future.result()
                except Exception as error:
                    failures.append(error)
            if failures:
                raise RuntimeError('worker cancellation incomplete; retain resource journals') from failures[0]
        with database(directory) as connection:
            connection.execute("UPDATE jobs SET status='cancelled',finished=?,error='cancelled by user' WHERE status='running'",
                               (time.time(),))
        count = _recover(directory, plan)
        control.update(phase='cancelled', recovered_journals=count)
    from ctxpress.harness.results.report import write_report
    write_report(directory)
    return dict(state(directory), recovered_journals=count)


def resume(directory, background=False):
    from ctxpress.harness.jobs.queue import _verified_plan, start
    directory = Path(directory).resolve(); plan = _plan(directory)
    with _operation(directory, 'resuming') as control:
        _scheduler_stopped(directory, 60)
        _recover(directory, plan)
        _verified_plan(directory)
        control.update(phase='resumed', dispatch_stopped=False)
    return start(directory / 'plan.json', directory, background=background, retry=True)
