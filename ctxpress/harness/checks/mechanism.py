"""Prepare M1 checks through the shared evaluation queue.

By default this only writes a plan. After settings approval, --run starts the
finite queue in the background; results do not establish task quality.
"""
import argparse, copy, json
from pathlib import Path
from ctxpress.harness.jobs import plan as eval_plan, queue as evaluation
from ctxpress.core import artifacts as artifact_io

ENTRIES = [
    ("CodexAutoCompact", {"t": 230000}),
    ("NoCompaction", {}),
    ("ComplexityTrap", {"budget": 10}),
    ("KeepLastTokens", {"budget": 16000}),
    ("SlidingWindow", {"t": 230000}),
    ("Pichay", {"age": 4, "min_size": 500}),
    ("ClawVM", {"budget": 32000}),
    ("ARC", {"budget": 5}),
    ("TokenPilot", {"budget": 2000, "a": 8, "score": False}),
    ("SWEPruner", {}),
    ("ClaudeCode", {"t": 230000}),
    ("CliffCompaction", {"t": 200000}),
    ("ScoredMethod", {"budget": 16000, "protect": 1}),
    ("AgentFold", {"budget": 2}),
    ("ACON", {"t_hist": 230000, "t_obs": 2000}),
    ("ReSum", {"k": 40}),
    ("ComplexityTrapSummary", {"n": 21, "m": 10}),
    ("ComplexityTrapHybrid", {"n": 43, "m": 10, "w": 10}),
    ("AgentDiet", {"a": 2, "b": 1, "threshold": 500}),
    ("CWL", {"budget": 80000}),
    ("DTOC", {}),
    ("ACM", {}),
    ("ClearThenSummarize", {"t": 230000}),
    ("PichayApprox", {}),
    ("ClawVMApprox", {"budget": 5}),
    ("CostModel", {}),
    ("AutoCostModel", {}),
    ("EntryTruncation", {"inner": {"class": "ScoredMethod", "args": {"budget": 16000}}, "budget": 2000}),
    ("PinRequirements", {"inner": {"class": "ComplexityTrap", "args": {"budget": 10}}}),
    ("WithMemory", {"inner": {"class": "ComplexityTrap", "args": {"budget": 10}}, "label": True}),
    ("Trigger", {"inner": {"class": "ComplexityTrap", "args": {"budget": 10}}, "threshold": 128000}),
    ("Composed", {"methods": [
        {"class": "PinRequirements", "args": {"inner": {"class": "NoCompaction"}}},
        {"class": "WithMemory", "args": {"inner": {"class": "EntryTruncation", "args": {
            "budget": 2000, "inner": {"class": "ScoredMethod", "args": {"budget": 16000, "protect": 1}}}}}}]}),
]


def boundary(value):
    """The caller selects recorded coordinates and declares their context size."""
    try:
        n, j, tokens = map(int, value.split(":"))
        if n < 0 or j <= 0 or tokens <= 0:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError("boundary must be N:J:CONTEXT_TOKENS (N >= 0, J and tokens > 0)") from None
    return dict(id=f"n{n}-j{j}", n=n, j=j, context_tokens=tokens)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--calls", type=int, default=6)
    ap.add_argument("--out", default="runs/mechanism-plan.json", help="compiled plan JSON")
    ap.add_argument("--directory", default="runs/mechanism")
    ap.add_argument("--only", nargs="+", choices=[name for name, _ in ENTRIES])
    ap.add_argument("--model", required=True)
    ap.add_argument("--bindir", required=True)
    ap.add_argument("--boundary", type=boundary, action="append", required=True,
                    help="recorded N:J:CONTEXT_TOKENS; repeat to select multiple boundaries")
    ap.add_argument("--scripts")
    ap.add_argument('--environment-snapshot', help='reviewed local image/workspace manifest')
    ap.add_argument('--grading-snapshot', help='reviewed official grading input manifest')
    ap.add_argument("--reasoning", default="low")
    ap.add_argument("--pruner-url")
    ap.add_argument("--cost-profile", help="fitted CostModel statistics from independent training histories")
    ap.add_argument("--cost-policy", help="frozen AutoCostModel policy from independent training histories")
    ap.add_argument("--via", help="HTTP proxy reachable from the experiment host")
    ap.add_argument("--upstream", help="Responses upstream URL")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--compact-limit", type=int, default=230000)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--run", action="store_true", help="start the approved plan in the background")
    a = ap.parse_args(argv)
    methods, omitted = [], []
    for cls, original in ENTRIES:
        if a.only and cls not in a.only:
            continue
        args = copy.deepcopy(original)
        resource = {"SWEPruner": (a.pruner_url, "url", "--pruner-url"),
                    "CostModel": (a.cost_profile, "profile", "--cost-profile"),
                    "AutoCostModel": (a.cost_policy, "policy", "--cost-policy")}.get(cls)
        if resource:
            value, key, option = resource
            if not value:
                if a.only:
                    ap.error(f"{cls} needs {option}")
                omitted.append(f"{cls}: {option} not supplied")
                continue
            args[key] = value
        if cls == "CodexAutoCompact":
            args["t"] = a.compact_limit
        methods.append({"class": cls, "args": args})
    env = dict(bindir=a.bindir)
    if a.scripts:
        env["scripts"] = a.scripts
    if a.environment_snapshot:
        env['snapshot'] = a.environment_snapshot
    if a.grading_snapshot:
        env['grading'] = a.grading_snapshot
    if a.via:
        env['via'] = a.via
    if a.upstream:
        env['upstream'] = a.upstream
    config = dict(schema="ctxpress.eval", version=1, name="codex-m1-mechanism", scope="mechanism", backend="codex_docker", benchmark="swe-milestone",
        model=a.model, reasoning=a.reasoning, environment=env,
        boundaries=a.boundary, methods=methods, repeats=1, workers=a.workers,
        run=dict(max_calls=a.calls, timeout=a.timeout, compact_limit=a.compact_limit, installed=True, submit=False))
    plan = eval_plan.compile_plan(config)
    artifact_io.atomic_json(a.out, plan)
    result = dict(plan=str(Path(a.out).resolve()), sha256=plan["sha256"], run_count=plan["run_count"],
        max_parallel=plan["max_parallel"], agent_timeout_seconds_upper_bound=plan["agent_timeout_seconds_upper_bound"],
        boundaries=a.boundary, omitted=omitted, missing_environment_files=plan["missing_environment_files"], cost_evidence=plan["cost_evidence"],
        evidence_scope="Each selected method is scheduled once per boundary; completing a run does not prove every operation triggered or task quality.")
    if a.run:
        result["started"] = evaluation.start(a.out, a.directory, background=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
