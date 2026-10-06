"""`ctxpress eval`: plan, run, inspect, cancel, recover and compare evaluation jobs. It sits above the job queue,
results and benchmark adapters, and the queue launches its scheduler and jobs through it."""
from __future__ import annotations
import argparse, json, sqlite3
from pathlib import Path
from ctxpress import benchmarks
from ctxpress.harness.jobs import plan as eval_plan, queue as evaluation, task as tasks
from ctxpress.harness.results import task_compare


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
        from ctxpress.harness.results.evidence import describe
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
        result = evaluation.start(args.plan, args.directory, args.background, args.retry)
    elif args.command == "status":
        result = evaluation.state(args.directory)
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
            result = task_compare.compare_results(args.directory, args.reference, args.output)
        except (ValueError, OSError, sqlite3.Error) as error:
            ap.error(str(error))
    elif args.command == "worker":
        result = evaluation.schedule(args.directory, args.retry)
    else:
        evaluation.execute_job(args.directory, args.job, args.attempt)
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
