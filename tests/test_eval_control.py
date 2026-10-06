"""Real local process cancellation and durable recovery; no models or Docker."""
import json, os, subprocess, sys, time
from pathlib import Path
import pytest
from ctxpress import benchmarks
from ctxpress.__main__ import main
from ctxpress.harness.jobs import control as eval_control, plan as eval_plan, queue as evaluation
from ctxpress.core import processes
from ctxpress.harness.results.report import report
from test_evaluation import config, fake_launch


def prepared(tmp_path, repeats=2):
    cfg = config(); cfg['methods'] = cfg['methods'][:1]; cfg['repeats'] = repeats
    return evaluation.prepare(eval_plan.compile_plan(cfg), tmp_path / 'run')


def test_cancel_pending_and_resume_preserves_completed_results(tmp_path, monkeypatch, capsys):
    directory = prepared(tmp_path)
    ids = [row['id'] for row in evaluation.state(directory)['jobs']]
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET status='running',attempt=1 WHERE id=?", (ids[0],))
    evaluation.set_result(directory, ids[0], 1, result={'test_only': True, 'grade': {'resolved': True}})
    main(['eval', 'cancel', '--directory', str(directory)])
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot['counts'] == {'completed': 1, 'cancelled': 1}
    assert snapshot['control']['dispatch_stopped']
    assert report(directory)['methods'][0]['cancelled'] == 1
    with pytest.raises(RuntimeError, match='eval resume'):
        evaluation.schedule(directory, retry=True)
    launches = []
    def resumed(path, folder, background=False, retry=False):
        assert retry
        def launch(folder, job_id, attempt):
            launches.append((job_id, attempt))
            return fake_launch(folder, job_id, attempt)
        return evaluation.schedule(folder, retry=retry, launch=launch, interval=.01)
    monkeypatch.setattr(evaluation, 'start', resumed)
    main(['eval', 'resume', '--directory', str(directory), '--background'])
    assert launches == [(ids[1], 1)]
    assert evaluation.state(directory)['counts'] == {'completed': 2}
    assert not evaluation.state(directory)['control']['dispatch_stopped']


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux managed process signals')
def test_cancel_live_worker_waits_for_exit_before_recovery(tmp_path, monkeypatch):
    directory = prepared(tmp_path, repeats=1)
    job = evaluation.state(directory)['jobs'][0]['id']
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    calls = []
    try:
        with evaluation.database(directory) as connection:
            connection.execute("UPDATE jobs SET status='running',attempt=1,pid=?,identity=?", (process.pid, processes.identity(process.pid)))
        folder = directory / 'jobs' / job / 'attempt-1'; folder.mkdir(parents=True)
        (folder / 'resources-agent.json').write_text(json.dumps({'role': 'agent'}), encoding='utf-8')
        (folder / 'resources-verifier.json').write_text(json.dumps({'role': 'verifier'}), encoding='utf-8')
        def recover(adapter, path, label):
            assert not processes.alive(process.pid)
            calls.append((path.name, label))
        monkeypatch.setattr(benchmarks.SWEMilestone, 'recover', recover)
        snapshot = eval_control.cancel(directory, timeout=.1)
        assert process.wait(timeout=5) != 0
        assert snapshot['counts'] == {'cancelled': 1}
        assert snapshot['recovered_journals'] == 2
        assert [row[0] for row in calls] == ['resources-agent.json', 'resources-verifier.json']
        assert all(row[1] == eval_plan.load(directory / 'plan.json', check_inputs=False)['sha256'][:12] + '-' + job for row in calls)
    finally:
        if process.poll() is None:
            process.kill(); process.wait()


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux managed process signals')
def test_cancel_running_queue_unwinds_frozen_worker_and_prevents_more_launches(tmp_path):
    from test_eval_inputs import inputs
    cfg = inputs(tmp_path); cfg.update(repeats=3, workers=1, methods=[{'class': 'NoCompaction'}])
    directory = evaluation.prepare(eval_plan.compile_plan(cfg), tmp_path / 'run')
    worker = tmp_path / 'synthetic-worker.py'
    worker.write_text('''import sys, time
from pathlib import Path
directory, job, attempt = sys.argv[1:]
sys.path.insert(0, str(Path(directory) / 'runtime'))
from ctxpress import benchmarks
from ctxpress.harness.jobs.queue import execute_job
def execute(self, task, entry, config, spec, *, paths, folder, label):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'synthetic-ready').write_text('started')
    try:
        time.sleep(60)
    finally:
        (folder / 'synthetic-cleanup').write_text('guard unwound')
benchmarks.SWEMilestone.execute = execute
execute_job(directory, job, int(attempt))
''', encoding='utf-8')
    scheduler = tmp_path / 'synthetic-scheduler.py'
    scheduler.write_text('''import subprocess, sys
from pathlib import Path
directory, worker = sys.argv[1:]
sys.path.insert(0, str(Path(directory) / 'runtime'))
from ctxpress.harness.jobs.queue import schedule
def launch(directory, job, attempt):
    return subprocess.Popen([sys.executable, worker, str(directory), job, str(attempt)], start_new_session=True)
schedule(directory, launch=launch, interval=.02)
''', encoding='utf-8')
    with (directory / 'synthetic.log').open('wb') as output:
        process = subprocess.Popen([sys.executable, str(scheduler), str(directory), str(worker)],
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            while not list(directory.glob('jobs/*/attempt-*/synthetic-ready')):
                assert process.poll() is None, (directory / 'synthetic.log').read_text(encoding='utf-8')
                assert time.monotonic() < deadline, 'synthetic worker did not start'
                time.sleep(.02)
            snapshot = eval_control.cancel(directory, timeout=5)
            assert process.wait(timeout=5) == 0
            assert snapshot['counts'] == {'cancelled': 3}
            assert len(list(directory.glob('jobs/*/attempt-*/synthetic-ready'))) == 1
            assert len(list(directory.glob('jobs/*/attempt-*/synthetic-cleanup'))) == 1
            assert not any(row.get('alive') for row in snapshot['jobs'])
        finally:
            if process.poll() is None:
                process.kill(); process.wait()


def test_recovery_orders_legacy_harbor_agent_before_verifier(tmp_path):
    agent = tmp_path / 'resources-z.json'; agent.write_text(json.dumps({'channel': '/owned/channel'}), encoding='utf-8')
    verifier = tmp_path / 'resources-a.json'; verifier.write_text(json.dumps({'role': 'verifier'}), encoding='utf-8')
    class Adapter:
        def recover(self, path, label):
            events.append(path.name)
    events = []
    eval_control.recover_attempt(Adapter(), tmp_path, 'owned')
    assert events == [agent.name, verifier.name]


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux managed process signals')
def test_identity_mismatch_and_live_recovery_preserve_resources(tmp_path, monkeypatch):
    directory = prepared(tmp_path, repeats=1)
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    try:
        with pytest.raises(ValueError, match='identity changed'):
            processes.stop_owned(process.pid, 'proc:wrong', timeout=.01)
        assert process.poll() is None
        with evaluation.database(directory) as connection:
            connection.execute("UPDATE jobs SET status='running',attempt=1,pid=?,identity=?", (process.pid, processes.identity(process.pid)))
        monkeypatch.setattr(benchmarks.SWEMilestone, 'recover', lambda *args: pytest.fail('live resource recovery'))
        with pytest.raises(RuntimeError, match='still alive'):
            eval_control.recover(directory, timeout=.1)
        assert process.poll() is None
        assert evaluation.state(directory)['control']['phase'] == 'cleanup_failed'
    finally:
        process.kill(); process.wait()


def test_recovery_failure_keeps_dispatch_stopped_and_retry_evidence(tmp_path, monkeypatch):
    directory = prepared(tmp_path, repeats=1)
    job = evaluation.state(directory)['jobs'][0]['id']
    folder = directory / 'jobs' / job / 'attempt-1'; folder.mkdir(parents=True)
    journal = folder / 'resources-agent.json'; journal.write_text(json.dumps({'role': 'agent'}), encoding='utf-8')
    partial = folder / 'model.patch'; partial.write_text('partial output', encoding='utf-8')
    def fail(*args):
        raise RuntimeError('ownership changed')
    monkeypatch.setattr(benchmarks.SWEMilestone, 'recover', fail)
    with pytest.raises(RuntimeError, match='ownership changed'):
        eval_control.cancel(directory)
    assert evaluation.state(directory)['control']['dispatch_stopped'] and journal.exists()
    assert partial.read_text(encoding='utf-8') == 'partial output'
    monkeypatch.setattr(benchmarks.SWEMilestone, 'recover', lambda *args: None)
    snapshot = eval_control.recover(directory)
    assert snapshot['control']['phase'] == 'recovered'
    assert partial.exists() and snapshot['counts'] == {'cancelled': 1}


def test_control_rejects_changed_job_specs_and_active_controller(tmp_path):
    directory = prepared(tmp_path, repeats=1)
    with evaluation.database(directory) as connection:
        connection.execute("INSERT INTO metadata VALUES ('control',?)", (json.dumps(dict(dispatch_stopped=True,
                           owner=dict(pid=os.getpid(), identity=processes.identity(os.getpid()), token='other'))),))
    with pytest.raises(RuntimeError, match='control operation'):
        eval_control.cancel(directory)
    with evaluation.database(directory) as connection:
        connection.execute("DELETE FROM metadata WHERE key='control'")
        connection.execute("UPDATE jobs SET spec='{}'")
    with pytest.raises(ValueError, match='job identities'):
        eval_control.cancel(directory)


def test_catalog_exposes_all_eight_task_start_families_without_execution(capsys, monkeypatch):
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('catalog started a process'))
    main(['eval', 'benchmarks', '--start-mode', 'task_start'])
    data = json.loads(capsys.readouterr().out)
    assert data['family_count'] == 8 and len(data['benchmarks']) == 10
    assert set(name for names in data['families'].values() for name in names) == set(benchmarks.REGISTRY)
    assert all(row['from_task_start'] and row['execution_supported'] and row['official_grading_supported']
               and not row['real_run_verified'] for row in data['benchmarks'])
    main(['eval', 'benchmarks'])
    data = json.loads(capsys.readouterr().out)
    milestone = next(row for row in data['benchmarks'] if row['name'] == 'swe-milestone')
    assert set(milestone['modes']) == {'checkpoint', 'task_start'}
