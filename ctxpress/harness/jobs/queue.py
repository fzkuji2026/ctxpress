"""Finite background evaluation jobs, with durable state and independent processes."""
from __future__ import annotations
import argparse, contextlib, json, os, shutil, signal, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path
from ctxpress.harness.jobs import plan as eval_plan, inputs as eval_inputs
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
    eval_plan.atomic_json(directory / "plan.json", plan)
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


def compare_results(directory, reference, output=None):
    """Analyze a consistent read-only job snapshot without changing its evidence."""
    from ctxpress.harness.results import task_compare as eval_task_compare
    directory = Path(directory).resolve()
    db = directory / 'jobs.sqlite'
    if db.is_symlink() or not db.is_file():
        raise ValueError('comparison requires an existing regular evaluation database')
    with contextlib.closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN')
        record = connection.execute("SELECT value FROM metadata WHERE key='plan'").fetchone()
        if record is None:
            raise ValueError('comparison database has no frozen plan')
        plan = eval_plan.verify(json.loads(record['value']), check_inputs=False)
        jobs = [dict(row) for row in connection.execute('SELECT * FROM jobs ORDER BY id')]
    destination = Path(output).expanduser().resolve() if output is not None else None
    if destination is not None:
        protected = [directory / name for name in ('inputs', 'runtime', 'jobs')]
        protected += [Path(item['tree']['root']).resolve() for item in plan.get('input_trees', {}).values()]
        if (destination.exists() or Path(output).expanduser().is_symlink() or
                destination.name.lower() == 'auth.json' or
                str(destination) in plan.get('artifacts', {}) or
                any(destination == root or root in destination.parents for root in protected)):
            raise ValueError('comparison output must be a new file outside frozen evidence and input trees')
    result = eval_task_compare.compare(plan, jobs, reference=reference, directory=directory)
    if destination is not None:
        serialized = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        # Exclusive creation also refuses an output that appeared during analysis.
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('x', encoding='utf-8') as stream:
            stream.write(serialized)
    return result


def _launch_job(directory, job_id, attempt):
    folder = Path(directory) / "jobs" / job_id / f"attempt-{attempt}"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "worker.log").open("ab") as output:
        return subprocess.Popen([sys.executable, "-m", "ctxpress.harness.jobs.queue", "job", "--directory", str(directory),
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
        from ctxpress.benchmarks.harbor.protocol import claimed_gpus
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
        command = [sys.executable, "-m", "ctxpress.harness.jobs.queue", "worker", "--directory", str(directory)]
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
        eval_plan.atomic_json(folder / "result.json", result)
        validate_execution(result)
        set_result(directory, job_id, attempt, result=result)
    except BaseException as error:
        set_result(directory, job_id, attempt, result=result, error=type(error).__name__)
        raise
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser('benchmarks', help='list built-in benchmark adapters and their supported scope')
    p.add_argument('--start-mode', choices=tasks.MODES)
    p.add_argument('--evidence', action='append', default=[], help='review.json bundle; repeat to show bounded real-run evidence')
    p = sub.add_parser('tasks', help='discover local benchmark task IDs; no execution or downloads')
    p.add_argument('--benchmark', default=benchmarks.DEFAULT)
    p.add_argument('--scripts', help='local benchmark helper/catalog directory')
    p.add_argument('--start-mode', choices=tasks.MODES, default='checkpoint')
    p.add_argument('--data', help='official local dataset directory, required for task_start discovery')
    p.add_argument('--min-context', type=int, default=0)
    p.add_argument('--gradable', action='store_true', help='only tasks with an official grading mapping')
    p = sub.add_parser('capture-task-resources', help='freeze task-start resource identities; inspect local files/images only')
    p.add_argument('--benchmark', required=True); p.add_argument('--data', required=True)
    p.add_argument('--spec', required=True, help='local task resource capture specification JSON')
    p.add_argument('--task', action='append', required=True, help='explicit task ID; repeat for each task')
    p.add_argument('--output', required=True)
    p = sub.add_parser('configure', help='generate a config from a fixed local protocol; no downloads or execution')
    p.add_argument('protocol'); p.add_argument('--family', choices=benchmarks.FAMILIES, required=True)
    p.add_argument('--phase', choices=('pilot', 'method-pilot', 'comparison'), required=True)
    p.add_argument('--data', required=True); p.add_argument('--bindir', required=True)
    p.add_argument('--resources', help='optional captured local task resource manifest')
    p.add_argument('--prices', help='optional local per-model rate declarations JSON')
    p.add_argument('--model-catalog', help='explicit local Codex model catalog to freeze across methods')
    p.add_argument('--output', required=True, help='new configuration path; provenance is written alongside it')
    p = sub.add_parser("plan"); p.add_argument("config"); p.add_argument("--output", required=True)
    p = sub.add_parser('capture-environment', help='inspect local images and declare the auxiliary workspace; no experiment')
    p.add_argument('--workspace', required=True); p.add_argument('--base-image', required=True)
    p.add_argument('--boundary', action='append', required=True, help='n:j; repeat for each selected boundary')
    p.add_argument('--output', required=True)
    p = sub.add_parser('capture-grader', help='declare official grading code, inputs, dependencies and local images')
    p.add_argument('--code', required=True); p.add_argument('--data', required=True); p.add_argument('--trials', required=True)
    p.add_argument('--boundary',action='append',required=True,help='n:j; repeat for each selected boundary')
    p.add_argument('--output',required=True)
    p = sub.add_parser("run"); p.add_argument("plan"); p.add_argument("--directory", required=True)
    p.add_argument("--background", action="store_true"); p.add_argument("--retry", action="store_true")
    p = sub.add_parser("status"); p.add_argument("--directory", required=True)
    p = sub.add_parser("report"); p.add_argument("--directory", required=True)
    p = sub.add_parser('review', help='read-only quality, cost, exclusions, cleanup and process analysis bundle')
    p.add_argument('--directory', required=True); p.add_argument('--reference', required=True)
    p.add_argument('--output', required=True, help='new directory outside frozen evidence')
    p.add_argument('--exclusions', help='plan/source-bound defect review JSON; omission means unreviewed')
    p.add_argument('--inspect-resources', action='store_true', help='read-only Docker and worker observations')
    p.add_argument('--no-analysis', action='store_true')
    p = sub.add_parser('compare', help='read-only task-start comparison by task and repeat')
    p.add_argument('--directory', required=True)
    p.add_argument('--reference', required=True, help='exact method label in the frozen plan')
    p.add_argument('--output', help='optional new analysis JSON; original results are preserved')
    for command in ('cancel', 'recover'):
        p = sub.add_parser(command); p.add_argument('--directory', required=True)
        p.add_argument('--timeout', type=float, default=60, help='seconds for owned processes to exit')
    p = sub.add_parser('resume'); p.add_argument('--directory', required=True)
    p.add_argument('--background', action='store_true')
    p = sub.add_parser("worker"); p.add_argument("--directory", required=True); p.add_argument("--retry", action="store_true")
    p = sub.add_parser("job"); p.add_argument("--directory", required=True); p.add_argument("--job", required=True); p.add_argument("--attempt", type=int, required=True)
    args = ap.parse_args(argv)
    if args.command == 'benchmarks':
        from ctxpress.harness.jobs.evidence import describe
        result = dict(benchmarks=describe(args.start_mode, args.evidence), family_count=len(benchmarks.FAMILIES),
                      families=benchmarks.FAMILIES)
    elif args.command == 'review':
        from ctxpress.harness.results.review import review
        reviewed = review(args.directory, args.reference, args.output, args.exclusions,
                          args.inspect_resources, not args.no_analysis)
        result = dict(artifact=str(Path(args.output) / 'review.json'), counts=reviewed['counts'])
    elif args.command == 'tasks':
        if args.min_context < 0:
            ap.error('--min-context must be nonnegative')
        benchmark = benchmarks.get(args.benchmark)
        if args.start_mode == 'task_start':
            if not args.data or args.scripts or args.min_context or args.gradable:
                ap.error('task_start discovery requires --data; checkpoint filters and --scripts do not apply')
            selected = benchmark.task_instances(args.data)
            description = benchmark.task_start_description()
        else:
            if args.data:
                ap.error('--data requires --start-mode task_start')
            selected = benchmark.tasks(args.scripts or benchmark.default_scripts())
            selected = [task for task in selected if (not args.gradable or task['official_grading_mapped']) and
                 (not args.min_context or type(task['context_tokens']) is int and task['context_tokens'] >= args.min_context)]
            description = benchmark.describe()
        result = dict(benchmark=description, tasks=selected, count=len(selected),
                      resource_scope='catalog discovery only; execution resources and grading inputs not checked', experiments_started=0)
    elif args.command == 'capture-task-resources':
        from ctxpress.harness.jobs import resources as task_resources
        adapter = benchmarks.get(args.benchmark)
        found = adapter.task_instances(args.data)
        catalog = {task['id']:task for task in found}
        if len(catalog) != len(found) or len(args.task) != len(set(args.task)) or set(args.task) - set(catalog):
            ap.error('provide unique task IDs from the selected local benchmark dataset')
        spec_path = Path(args.spec).expanduser().resolve()
        eval_plan.file_sha256(spec_path)
        spec = json.loads(spec_path.read_text(encoding='utf-8'))
        destination = Path(args.output).expanduser().resolve()
        roots = [Path(args.data).expanduser().resolve()]
        if isinstance(spec, dict) and isinstance(spec.get('trees'), dict):
            roots += [Path(item['root']).expanduser().resolve() for item in spec['trees'].values() if isinstance(item,dict) and isinstance(item.get('root'),str)]
        if destination == spec_path or any(destination == root or root in destination.parents for root in roots):
            ap.error('resource manifest output must be outside its dataset, official input trees and capture specification')
        lock = task_resources.capture(spec, [catalog[key] for key in args.task])
        eval_plan.atomic_json(destination, lock)
        result = dict(manifest=str(destination), sha256=lock['sha256'], tasks=list(args.task),
                      experiments_started=0, downloads_started=0, scope=lock['scope'])
    elif args.command == 'capture-environment':
        from ctxpress.harness.jobs import environment as eval_environment
        source, destination = Path(args.workspace).expanduser().resolve(), Path(args.output).expanduser().resolve()
        if destination == source or source in destination.parents:
            ap.error('environment manifest output must be outside the auxiliary workspace')
        try:
            coordinates = [tuple(map(int, value.split(':'))) for value in args.boundary]
            if any(len(point) != 2 for point in coordinates):
                raise ValueError()
        except ValueError:
            ap.error('boundaries must use n:j coordinates')
        lock = eval_environment.capture(source, args.base_image, coordinates)
        destination.parent.mkdir(parents=True, exist_ok=True)
        eval_plan.atomic_json(destination, lock)
        result = dict(manifest=str(destination), sha256=lock['sha256'], images=lock['images'],
                      workspace_files=len(lock['workspace']['files']), experiments_started=0)
    elif args.command == 'capture-grader':
        from ctxpress.benchmarks.milestone import checkpoint_grading as eval_grading
        destination = Path(args.output).expanduser().resolve()
        sources = [Path(path).expanduser().resolve() for path in (args.code,args.data,args.trials)]
        if any(destination == source or source in destination.parents for source in sources):
            ap.error('grading manifest output must be outside the source directories')
        try:
            coordinates = [tuple(map(int,value.split(':'))) for value in args.boundary]
            if any(len(point)!=2 for point in coordinates):
                raise ValueError()
        except ValueError:
            ap.error('boundaries must use n:j coordinates')
        lock = eval_grading.capture(args.code,args.data,args.trials,coordinates)
        destination.parent.mkdir(parents=True,exist_ok=True)
        eval_plan.atomic_json(destination,lock)
        result = dict(manifest=str(destination),sha256=lock['sha256'],images=lock['images'],
                      input_files=sum(len(tree['files']) for tree in lock['trees'].values()),scope=lock['scope'],experiments_started=0)
    elif args.command == 'configure':
        from ctxpress.harness.jobs import protocol as eval_protocol
        try:
            result = eval_protocol.write(args.protocol, args.output, family=args.family, phase=args.phase,
                                         data=args.data, bindir=args.bindir, resources=args.resources, prices=args.prices,
                                         model_catalog=args.model_catalog)
        except (ValueError, OSError) as error:
            ap.error(str(error))
    elif args.command == "plan":
        source = Path(args.config)
        plan = eval_plan.compile_plan(json.loads(source.read_text(encoding="utf-8")), source.resolve().parent)
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        eval_plan.atomic_json(args.output, plan)
        result = {key: plan[key] for key in ("sha256", "run_count", "max_parallel", "agent_timeout_seconds_upper_bound", "context_evidence", "cost_evidence", "missing_environment_files")}
    elif args.command == "run":
        result = start(args.plan, args.directory, args.background, args.retry)
    elif args.command == "status":
        result = state(args.directory)
    elif args.command in ('cancel', 'recover', 'resume'):
        from ctxpress.harness.jobs import control as eval_control
        if args.command == 'resume':
            result = eval_control.resume(args.directory, background=args.background)
        else:
            result = getattr(eval_control, args.command)(args.directory, timeout=args.timeout)
    elif args.command == "report":
        from ctxpress.harness.results.report import write_report
        result = write_report(args.directory)
    elif args.command == 'compare':
        try:
            result = compare_results(args.directory, args.reference, args.output)
        except (ValueError, OSError, sqlite3.Error) as error:
            ap.error(str(error))
    elif args.command == "worker":
        result = schedule(args.directory, args.retry)
    else:
        execute_job(args.directory, args.job, args.attempt)
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
