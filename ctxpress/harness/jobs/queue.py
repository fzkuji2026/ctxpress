"""Finite background evaluation jobs, with durable state and independent processes."""
from __future__ import annotations
import contextlib, json, os, shutil, signal, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path
from ctxpress.harness.jobs import plan as eval_plan, inputs as eval_inputs
from ctxpress.core import artifacts as artifact_io
from ctxpress.core import processes
from ctxpress import benchmarks
from ctxpress.harness.jobs import task as tasks


class ModelRequestFailure(RuntimeError):
    """The continuation produced no successful observed model request."""


class EvaluationCancelled(KeyboardInterrupt):
    """A user cancelled this worker; unwind the benchmark's resource guards."""


def execution_observation(result):
    from ctxpress.harness.runtime import execution_health
    health = execution_health.observe(result)
    successful = sum("request" in row and type(row.get("status")) is int and 200 <= row["status"] < 300
                     and not row.get("stream_error") for row in result.get("rewrites", []))
    requests = result.get("requests")
    observation = dict(observed_requests=requests, successful_requests=successful,
        valid=type(requests) is int and requests > 0 and successful > 0 and not health['execution_invalid'])
    if health['tool_calls'] or health['execution_invalid'] or result.get('execution_health') is not None:
        observation.update(execution_invalid=health['execution_invalid'], tool_runtime_failures=health['tool_runtime_failures'],
            execution_health_unknown=health['health_unknown'], execution_evidence_errors=health['evidence_errors'])
    return observation


def validate_execution(result):
    observation = execution_observation(result)
    if observation.get('execution_invalid'):
        from ctxpress.harness.runtime.execution_health import ExecutionInfrastructureFailure
        raise ExecutionInfrastructureFailure('execution invalid: tool runtime failed or its evidence is unsafe; see execution observation')
    if not observation["observed_requests"]:
        raise ModelRequestFailure("continuation exited without an observed model request")
    if not observation["valid"]:
        raise ModelRequestFailure("continuation produced no successful observed model request")


@contextlib.contextmanager
def database(directory):
    connection = sqlite3.connect(Path(directory) / "jobs.sqlite", timeout=20)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def prepare(plan, directory):
    eval_plan.verify(plan, check_inputs=False)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    runtime = directory / "runtime"
    if not runtime.exists():
        # Frozen inputs are copied and hashed below. Re-reading all source
        # artifacts twice while copying only the Python runtime adds minutes
        # for large official dependency trees on a mounted filesystem.
        frozen_inputs = plan.get('input_snapshot_version') == 1
        if frozen_inputs:
            if plan['code_sha256'] != eval_plan.fingerprint():
                raise ValueError('framework code changed; compile a new plan')
        else:
            eval_plan.verify(plan)
        staged = Path(tempfile.mkdtemp(prefix=".runtime-", dir=directory))
        try:
            shutil.copytree(Path(eval_plan.__file__).resolve().parents[2], staged / "ctxpress",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            if frozen_inputs:
                if plan['code_sha256'] != eval_plan.fingerprint(staged / 'ctxpress'):
                    raise ValueError('framework code changed; compile a new plan')
            else:
                eval_plan.verify(plan, code_root=staged / "ctxpress")
            try:
                staged.rename(runtime)
            except OSError:
                if not runtime.exists():
                    raise
        finally:
            if staged.exists():
                shutil.rmtree(staged)
    input_root = None
    if plan.get('input_snapshot_version') == 1:
        input_root = directory / 'inputs'
        eval_inputs.prepare(plan, input_root)
    eval_plan.verify(plan, code_root=runtime / "ctxpress", input_root=input_root)
    with database(directory) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL, "
            "attempt INTEGER NOT NULL DEFAULT 0, pid INTEGER, identity TEXT, started REAL, finished REAL, result TEXT, error TEXT)")
        row = connection.execute("SELECT value FROM metadata WHERE key='plan'").fetchone()
        if row and json.loads(row[0])["sha256"] != plan["sha256"]:
            raise ValueError("output directory belongs to a different plan")
        connection.execute("INSERT OR IGNORE INTO metadata VALUES ('plan', ?)", (eval_plan.canonical(plan),))
        for job in plan["jobs"]:
            connection.execute("INSERT OR IGNORE INTO jobs (id,spec,status) VALUES (?,?,'pending')", (job["id"], eval_plan.canonical(job)))
    artifact_io.atomic_json(directory / "plan.json", plan)
    return directory


def _verified_plan(directory):
    directory = Path(directory).resolve()
    plan = eval_plan.load(directory / 'plan.json', check_inputs=False)
    input_root = directory / 'inputs' if plan.get('input_snapshot_version') == 1 else None
    return eval_plan.verify(plan, code_root=directory / 'runtime/ctxpress', input_root=input_root)


def state(directory):
    from ctxpress.harness.jobs import control as eval_control
    with database(directory) as connection:
        jobs = [dict(row) for row in connection.execute("SELECT id,status,attempt,pid,identity,started,finished,error FROM jobs ORDER BY id")]
        row = connection.execute("SELECT value FROM metadata WHERE key='scheduler'").fetchone()
        phase = "running"
        if row is None:
            row = connection.execute("SELECT value FROM metadata WHERE key='background'").fetchone()
            phase = "starting"
        control = eval_control.read(connection)
    scheduler = json.loads(row[0]) if row else None
    if scheduler:
        scheduler["alive"] = processes.alive(scheduler["pid"], scheduler["identity"])
        scheduler["phase"] = phase
    counts = {}
    for job in jobs:
        if job['pid']:
            job["alive"] = processes.alive(job["pid"], job["identity"])
        counts[job["status"]] = counts.get(job["status"], 0) + 1
    return dict(directory=str(Path(directory).resolve()), scheduler=scheduler, control=control, counts=counts, jobs=jobs)


def set_result(directory, job_id, attempt, result=None, error=None):
    with database(directory) as connection:
        connection.execute("UPDATE jobs SET status=?,finished=?,result=?,error=? WHERE id=? AND attempt=? AND status='running'",
            ("cancelled" if error == 'EvaluationCancelled' else "failed" if error else "completed", time.time(), json.dumps(result, ensure_ascii=False) if result is not None else None,
             error, job_id, attempt))


def _launch_job(directory, job_id, attempt):
    folder = Path(directory) / "jobs" / job_id / f"attempt-{attempt}"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "worker.log").open("ab") as output:
        return subprocess.Popen([sys.executable, "-m", "ctxpress.harness.cli", "job", "--directory", str(directory),
            "--job", job_id, "--attempt", str(attempt)], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            cwd=Path(directory) / "runtime", env=_runtime_env(directory), **processes.detach_options())


def _runtime_env(directory):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(directory) / "runtime") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def _runnable(pending, live, slots):
    """Keep overlapping GPU jobs pending so they cannot consume all CPU slots."""
    def devices(row):
        spec = json.loads(row['spec'])
        from ctxpress.harness.runtime.gpu import claimed_gpus
        return {item.casefold() for item in claimed_gpus(spec.get('resources'))}
    occupied = set().union(*(devices(row) for row in live)) if live else set()
    selected = []
    for row in pending:
        if len(selected) >= slots:
            break
        requested = devices(row)
        if requested & occupied:
            continue
        selected.append(row)
        occupied.update(requested)
    return selected


def schedule(directory, retry=False, launch=None, interval=1.0):
    from ctxpress.harness.jobs import control as eval_control
    directory = Path(directory).resolve()
    plan = _verified_plan(directory)
    launch = launch or _launch_job
    mine = dict(pid=os.getpid(), identity=processes.identity(os.getpid()))
    with database(directory) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if eval_control.stopped(connection):
            raise RuntimeError('evaluation dispatch is stopped; use eval resume')
        row = connection.execute("SELECT value FROM metadata WHERE key='scheduler'").fetchone()
        previous = json.loads(row[0]) if row else None
        if previous and processes.alive(previous["pid"], previous["identity"]):
            raise RuntimeError("a scheduler is already running for this plan")
        connection.execute("INSERT OR REPLACE INTO metadata VALUES ('scheduler',?)", (json.dumps(mine),))
        if retry:
            for job in connection.execute("SELECT id,pid,identity FROM jobs WHERE status='running'").fetchall():
                if not processes.alive(job["pid"], job["identity"]):
                    connection.execute("UPDATE jobs SET status='interrupted' WHERE id=?", (job["id"],))
            connection.execute("UPDATE jobs SET status='pending',error=NULL WHERE status IN ('failed','interrupted','cancelled')")
    children = {}
    try:
        while True:
            with database(directory) as connection:
                if eval_control.stopped(connection):
                    break
            for job_id, process in list(children.items()):
                if process.poll() is not None:
                    del children[job_id]
            with database(directory) as connection:
                rows = [dict(row) for row in connection.execute("SELECT * FROM jobs ORDER BY id")]
                live = []
                for row in rows:
                    if row["status"] != "running":
                        continue
                    if processes.alive(row["pid"], row["identity"]):
                        live.append(row)
                    else:
                        connection.execute("UPDATE jobs SET status='interrupted',finished=?,error=? WHERE id=? AND status='running'",
                            (time.time(), "worker exited without a terminal result", row["id"]))
                pending = [row for row in rows if row["status"] == "pending"]
            slots = max(0, plan["max_parallel"] - len(live))
            for row in _runnable(pending, live, slots):
                _verified_plan(directory)
                attempt = row["attempt"] + 1
                with database(directory) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if eval_control.stopped(connection):
                        break
                    updated = connection.execute("UPDATE jobs SET status='running',attempt=?,pid=NULL,identity=NULL,started=?,finished=NULL,result=NULL,error=NULL WHERE id=? AND status='pending'",
                        (attempt, time.time(), row["id"]))
                    if not updated.rowcount:
                        continue
                    try:
                        process = launch(directory, row["id"], attempt)
                        children[row["id"]] = process
                        connection.execute("UPDATE jobs SET pid=?,identity=? WHERE id=? AND attempt=?", (process.pid, processes.identity(process.pid), row["id"], attempt))
                    except Exception as error:
                        connection.execute("UPDATE jobs SET status='failed',finished=?,error=? WHERE id=? AND attempt=?",
                            (time.time(), type(error).__name__, row["id"], attempt))
            snapshot = state(directory)
            if not snapshot["counts"].get("pending") and not snapshot["counts"].get("running") and not children:
                break
            time.sleep(interval)
    finally:
        # Observation ending is not permission to kill or repeat still-live jobs.
        with database(directory) as connection:
            connection.execute("DELETE FROM metadata WHERE key='scheduler' AND value=?", (json.dumps(mine),))
            connection.execute("DELETE FROM metadata WHERE key='background' AND value=?", (json.dumps(mine),))
    from ctxpress.harness.results.report import write_report
    return write_report(directory)


def start(plan_path, directory, background=False, retry=False):
    from ctxpress.harness.jobs import control as eval_control
    plan = eval_plan.load(plan_path, check_inputs=False)
    if plan["missing_environment_files"]:
        raise ValueError("evaluation environment is incomplete; prepare it and compile a new plan")
    prepare(plan, directory)
    directory = Path(directory).resolve()
    with database(directory) as connection:
        if eval_control.stopped(connection):
            raise RuntimeError('evaluation dispatch is stopped; use eval resume')
    snapshot = state(directory)
    if snapshot["scheduler"] and snapshot["scheduler"]["alive"]:
        return snapshot
    with (directory / "scheduler.log").open("ab") as output:
        command = [sys.executable, "-m", "ctxpress.harness.cli", "worker", "--directory", str(directory)]
        if retry:
            command += ["--retry"]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            cwd=directory / "runtime", env=_runtime_env(directory), **processes.detach_options())
    with database(directory) as connection:
        connection.execute("INSERT OR REPLACE INTO metadata VALUES ('background',?)",
            (json.dumps(dict(pid=process.pid, identity=processes.identity(process.pid))),))
    if not background:
        if process.wait() != 0:
            raise RuntimeError("evaluation worker failed; inspect scheduler.log")
        snapshot = state(directory)
        return dict(directory=str(directory), report=str(directory / "report.json"), html=str(directory / "report.html"),
                    completed=snapshot["counts"].get("completed", 0))
    return dict(directory=str(directory), pid=process.pid, identity=processes.identity(process.pid), background=True)


def execute_job(directory, job_id, attempt):
    directory = Path(directory).resolve()
    with database(directory) as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["attempt"] != attempt or row["status"] != "running":
            raise ValueError("job is not claimed by this attempt")
        job = json.loads(row["spec"])
    folder = directory / "jobs" / job_id / f"attempt-{attempt}"
    result = None
    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    def cancelled(number, frame):
        raise EvaluationCancelled('evaluation cancelled by user')
    for number in previous:
        signal.signal(number, cancelled)
    try:
        if Path(__file__).resolve().parents[2] != directory / "runtime" / "ctxpress":
            raise ValueError("jobs must run from the frozen runtime")
        plan = _verified_plan(directory)
        if plan["missing_environment_files"]:
            raise ValueError("evaluation environment is incomplete; prepare it and compile a new plan")
        label = plan["sha256"][:12] + "-" + job_id
        benchmark = benchmarks.get(plan['config'].get('benchmark', benchmarks.DEFAULT))
        if attempt > 1:
            from ctxpress.harness.jobs.control import recover_attempt
            for prior in sorted(folder.parent.glob('attempt-*')):
                if prior != folder:
                    recover_attempt(benchmark, prior, label)
        config = plan['config']
        task = tasks.from_job(job, config.get('benchmark', benchmarks.DEFAULT))
        paths = None
        entry = job['method']
        if plan.get('input_snapshot_version') == 1:
            config, paths = eval_inputs.execution(plan, directory / 'inputs')
            entry = eval_inputs.method(entry, paths)
        if paths is not None:
            task = tasks.remap(task, paths)
        result = benchmark.execute(task, entry, config, job, paths=paths, folder=folder, label=label)
        artifact_io.atomic_json(folder / "result.json", result)
        validate_execution(result)
        set_result(directory, job_id, attempt, result=result)
    except BaseException as error:
        set_result(directory, job_id, attempt, result=result, error=type(error).__name__)
        raise
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
