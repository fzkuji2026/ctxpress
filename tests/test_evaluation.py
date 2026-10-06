import copy, json, os, subprocess, sys, time
from pathlib import Path
import pytest
from ctxpress.harness.jobs import plan as eval_plan, queue as evaluation
from ctxpress.core import artifacts as artifact_io
from ctxpress.core import processes
from ctxpress.harness.results.report import report


def config():
    return dict(schema="ctxpress.eval", version=1, name="offline-fixture", scope="mechanism", backend="codex_docker", model="fixture-model",
        environment=dict(bindir="./fixture-bin"), boundaries=[dict(id="b", n=3, j=14, context_tokens=30000)],
        methods=[dict(**{"class": "ComplexityTrap"}, args=dict(n=5)), dict(**{"class": "CodexAutoCompact"}, args=dict(t=128000))],
        repeats=2, workers=2, run=dict(max_calls=4, timeout=20))


def test_plan_is_reviewable_and_does_not_start_processes(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("plan launched a process"))
    plan = eval_plan.compile_plan(config(), tmp_path)
    assert plan["run_count"] == 4 and plan["max_parallel"] == 2
    assert [job["compact_limit"] for job in plan["jobs"]] == [230000, 230000, 128000, 128000]
    assert plan["config"]["environment"]["bindir"] == str(tmp_path / "fixture-bin")
    changed = copy.deepcopy(plan)
    changed["jobs"][0]["method"]["args"]["n"] = 1
    with pytest.raises(ValueError, match="changed"):
        eval_plan.verify(changed)
    formal = config()
    formal.update(scope="formal")
    formal["run"].update(submit=True)
    with pytest.raises(ValueError, match="128k"):
        eval_plan.compile_plan(formal)
    formal["boundaries"][0]["context_tokens"] = 130000
    assert eval_plan.compile_plan(formal)["run_count"] == 4


def fake_launch(directory, job_id, attempt, delay=0.15, release=None):
    args = [sys.executable, str(Path(__file__).with_name("fake_eval_job.py")), str(directory), job_id, str(attempt), str(delay)]
    if release is not None:
        args.append(str(release))
    return subprocess.Popen(args,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def test_parallel_jobs_are_durable_and_not_repeated_on_resume(tmp_path):
    directory = evaluation.prepare(eval_plan.compile_plan(config()), tmp_path / "run")
    calls, processes_started = [], []
    def launch(directory, job_id, attempt):
        calls.append((job_id, attempt))
        process = fake_launch(directory, job_id, attempt)
        processes_started.append(process)
        return process
    result = evaluation.schedule(directory, launch=launch, interval=0.02)
    assert result["completed"] == 4 and len(calls) == 4
    assert evaluation.state(directory)["counts"] == {"completed": 4}
    evaluation.schedule(directory, launch=lambda *a: pytest.fail("completed job repeated"), interval=0.02)
    evidence = report(directory)
    assert all(method["valid_grades"] == 2 and method["api_input_tokens"] == 240 for method in evidence["methods"])
    for process in processes_started:
        assert process.wait(timeout=5) == 0, process.stderr.read().decode()
        process.stderr.close()


def test_resume_attaches_to_a_still_live_job(tmp_path):
    cfg = config(); cfg["methods"] = cfg["methods"][:1]; cfg["repeats"] = 1
    directory = evaluation.prepare(eval_plan.compile_plan(cfg), tmp_path / "run")
    job_id = eval_plan.load(directory / "plan.json")["jobs"][0]["id"]
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET status='running',attempt=1 WHERE id=?", (job_id,))
    process = fake_launch(directory, job_id, 1, delay=0.5)
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET pid=?,identity=? WHERE id=?", (process.pid, processes.identity(process.pid), job_id))
    try:
        evaluation.schedule(directory, launch=lambda *a: pytest.fail("live job repeated"), interval=0.02)
        assert evaluation.state(directory)["counts"] == {"completed": 1}
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill(); process.wait()
        process.stderr.close()


def test_scheduler_conflict_and_failed_attempt_retry(tmp_path):
    cfg = config(); cfg["methods"] = cfg["methods"][:1]; cfg["repeats"] = 1
    directory = evaluation.prepare(eval_plan.compile_plan(cfg), tmp_path / "run")
    mine = dict(pid=os.getpid(), identity=processes.identity(os.getpid()))
    with evaluation.database(directory) as connection:
        connection.execute("INSERT INTO metadata VALUES ('scheduler',?)", (json.dumps(mine),))
    with pytest.raises(RuntimeError, match="already running"):
        evaluation.schedule(directory)
    with evaluation.database(directory) as connection:
        connection.execute("DELETE FROM metadata WHERE key='scheduler'")
        connection.execute("UPDATE jobs SET status='running',attempt=1,pid=NULL")
    evaluation.schedule(directory, interval=0.01)
    assert evaluation.state(directory)["counts"] == {"interrupted": 1}
    evaluation.schedule(directory, retry=True, launch=fake_launch, interval=0.02)
    assert evaluation.state(directory)["jobs"][0]["attempt"] == 2
    assert evaluation.state(directory)["counts"] == {"completed": 1}


def test_report_preserves_invalid_grades_and_unavailable_usage(tmp_path):
    cfg = config(); cfg["methods"] = cfg["methods"][:1]; cfg["repeats"] = 2
    cfg["prices"] = dict(input=1, cached=0.1, output=5, unit="USD_per_million_tokens", source="fixture only", as_of="2000-01-01")
    directory = evaluation.prepare(eval_plan.compile_plan(cfg), tmp_path / "run")
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET status='running',attempt=1")
    ids = [job["id"] for job in evaluation.state(directory)["jobs"]]
    evaluation.set_result(directory, ids[0], 1, dict(grade=dict(infra_invalid=True, resolved=False), requests=1))
    evaluation.set_result(directory, ids[1], 1, dict(grade=dict(infra_invalid=False, resolved=True), requests=1))
    group = report(directory)["methods"][0]
    assert group["infra_invalid"] == group["valid_grades"] == group["resolved"] == 1
    assert group["missing_api_usage"] == 2 and group["api_cost_at_declared_rates_usd"] is None


def test_background_start_returns_while_jobs_continue(tmp_path, monkeypatch):
    cfg = config(); cfg['methods'] = cfg['methods'][:1]; cfg['repeats'] = 1
    binary = tmp_path / 'bin'
    binary.mkdir()
    (binary / 'codex').write_text('offline fixture', encoding='utf-8')
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    for name in ('valid_points.json', 'miss_probe_docker.py', 'c03_run.py', 'multi_probe.py', 'make_sidecar.py', 'grade.sh'):
        (scripts / name).write_text('offline fixture', encoding='utf-8')
    rollout = scripts / 'recorded.jsonl'
    rollout.write_text('{}\n', encoding='utf-8')
    (scripts / 'valid_points.json').write_text(json.dumps([dict(n=3,j=14,i=0,src=str(rollout))]), encoding='utf-8')
    cfg['environment'] = dict(bindir=str(binary), scripts=str(scripts))
    plan = eval_plan.compile_plan(cfg)
    path = tmp_path / 'plan.json'
    artifact_io.atomic_json(path, plan)
    actual = subprocess.Popen
    started = []
    def launch(command, **kwargs):
        if command[1:4] == ['-m', 'ctxpress.harness.cli', 'worker']:
            directory = command[command.index('--directory') + 1]
            process = actual([sys.executable, str(Path(__file__).with_name('fake_eval_scheduler.py')), directory], **kwargs)
            started.append(process)
            return process
        return actual(command, **kwargs)
    monkeypatch.setattr(evaluation.subprocess, 'Popen', launch)
    directory = tmp_path / 'run'
    result = evaluation.start(path, directory, background=True)
    try:
        assert result['background'] and processes.alive(result['pid'], result['identity'])
        snapshot = evaluation.state(directory)
        assert snapshot['scheduler']['alive']
        started[0].wait(timeout=15)
        assert evaluation.state(directory)['counts'] == {'completed': 1}
        assert (directory / 'report.html').exists()
    finally:
        for process in started:
            if process.poll() is None:
                process.kill(); process.wait()


def test_old_attempt_cannot_overwrite_successful_retry(tmp_path):
    cfg = config(); cfg['methods'] = cfg['methods'][:1]; cfg['repeats'] = 1
    directory = evaluation.prepare(eval_plan.compile_plan(cfg), tmp_path / 'run')
    job = evaluation.state(directory)['jobs'][0]['id']
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET status='running',attempt=2")
    evaluation.set_result(directory, job, 1, error='stale attempt')
    assert evaluation.state(directory)['counts'] == {'running': 1}
    evaluation.set_result(directory, job, 2, result=dict(test_only=True))
    evaluation.set_result(directory, job, 1, error='stale attempt')
    assert evaluation.state(directory)['counts'] == {'completed': 1}


def test_frozen_runtime_allows_continued_workspace_development(tmp_path, monkeypatch):
    cfg = config(); cfg['methods'] = cfg['methods'][:1]; cfg['repeats'] = 1
    plan = eval_plan.compile_plan(cfg)
    directory = evaluation.prepare(plan, tmp_path / 'run')
    original_fingerprint = eval_plan.fingerprint
    monkeypatch.setattr(eval_plan, 'fingerprint', lambda root=None: 'workspace-changed' if root is None else original_fingerprint(root))
    # Reattach using the existing runtime even after workspace code changes.
    evaluation.prepare(plan, directory)
    evaluation.schedule(directory, launch=fake_launch, interval=0.02)
    assert evaluation.state(directory)['counts'] == {'completed': 1}
    assert original_fingerprint(directory / 'runtime/ctxpress') == plan['code_sha256']


def test_report_does_not_price_absent_request_logs_as_free_execution(tmp_path):
    cfg = config(); cfg['methods'] = cfg['methods'][:1]; cfg['repeats'] = 1
    cfg['prices'] = dict(input=1, cached=0.1, output=5, unit='USD_per_million_tokens', source='fixture only', as_of='2000-01-01')
    directory = evaluation.prepare(eval_plan.compile_plan(cfg), tmp_path / 'run')
    job = evaluation.state(directory)['jobs'][0]['id']
    with evaluation.database(directory) as connection:
        connection.execute("UPDATE jobs SET status='running',attempt=1")
    evaluation.set_result(directory, job, 1, result=dict(requests=0, rewrites=[], usage={}))
    group = report(directory)['methods'][0]
    assert group['usage_unobserved_runs'] == 1 and not group['api_usage_complete']
    assert group['api_cost_at_declared_rates_usd'] is None
    assert group['completed'] == 1 and group['observed_continuations'] == 0
    assert group['jobs'][0]['execution_observation'] == dict(observed_requests=0,successful_requests=0,valid=False)


def test_interrupted_success_status_is_not_an_observed_continuation():
    result = dict(requests=1,rewrites=[dict(request=1,status=200,stream_error='IncompleteRead')])
    with pytest.raises(evaluation.ModelRequestFailure,match='no successful'):
        evaluation.validate_execution(result)
    assert not evaluation.execution_observation(result)['valid']
