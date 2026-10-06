"""Command line.

  python -m ctxpress install codex [--method M --args '{...}']   plain `codex` now runs through ctxpress
  python -m ctxpress use M [--args '{...}']                     switch the method (new sessions)
  python -m ctxpress uninstall codex                             plain `codex` again
  python -m ctxpress status                                      active method and what it did in recent sessions
  python -m ctxpress mcp                                         MCP server with ctxpress_retrieve / ctxpress_status tools
  python -m ctxpress codex --method M [--args '{...}'] -- <codex args>   run your own Codex with method M (plug-and-play)
  python -m ctxpress serve --method M [--args ...] --port 8899   the proxy alone, for any harness that can change its API base URL
  python -m ctxpress list                                        every method: paper, needs a model?, runs for real?, differences
  python -m ctxpress run configs/constraint.yaml [--workers 8]   (replay pre-screen, results are simulated) run a config
  python -m ctxpress methods [--html]                           framework table: every method x every layer
  python -m ctxpress params                                      every parameter with its source
  python -m ctxpress curves [--manifest ...]                     reuse curves p_k(a), m_k(a) per type
  python -m ctxpress fit --manifest ... --output profile.json    freeze historical cost-model statistics
  python -m ctxpress tune --manifest ... --lambdas ... --output policy.json  offline-screen a deployable cost policy
  python -m ctxpress coverage [--manifest ...]                   how often truncation / structure still covers an edit
  python -m ctxpress session NAME METHOD [--args '{...}']        run one method on one session and print the metrics
"""
from __future__ import annotations
import argparse, json, sys


def session_summary(s):
    print(f"ctxpress: {s['requests']} requests; history {s['history_tokens_before']} -> {s['history_tokens_after']} tokens; "
          f"API input {s['api_input_tokens']} ({s['api_cached_tokens']} cached)", file=sys.stderr)
    print(f"ctxpress: process evaluation: ctxpress analyze \"{s['log']}\" --prices <protocol or plan JSON> --output <new dir>",
          file=sys.stderr)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == 'eval':
        from ctxpress.harness.jobs.queue import main as eval_main
        return eval_main(arguments[1:])
    if arguments and arguments[0] == 'acm-author':
        from ctxpress.harness.author_acm import main as acm_main
        return acm_main(arguments[1:])
    if arguments and arguments[0] == 'analyze':
        from ctxpress.harness.results.analysis import main as analyze_main
        return analyze_main(arguments[1:])
    ap = argparse.ArgumentParser(prog="ctxpress")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("codex"); a.add_argument("--method"); a.add_argument("--args", default="{}")
    a.add_argument("--codex-bin"); a.add_argument("--port", type=int, default=0); a.add_argument("--upstream")
    a.add_argument("--quiet", action="store_true")
    a.add_argument("--via"); a.add_argument("--log"); a.add_argument("--store-dir"); a.add_argument("--dry-run", action="store_true")
    a.add_argument("codex_args", nargs=argparse.REMAINDER)
    a = sub.add_parser("claude", help="run Claude Code through the proxy (Anthropic Messages)")
    a.add_argument("--method"); a.add_argument("--args", default="{}"); a.add_argument("--claude-bin")
    a.add_argument("--port", type=int, default=0); a.add_argument("--upstream"); a.add_argument("--quiet", action="store_true")
    a.add_argument("--via"); a.add_argument("--log"); a.add_argument("--store-dir"); a.add_argument("--dry-run", action="store_true")
    a.add_argument("--no-tools", action="store_true", help="do not give the agent ctxpress's MCP tools")
    a.add_argument("claude_args", nargs=argparse.REMAINDER)
    a = sub.add_parser("serve"); a.add_argument("--method", default="NoCompaction"); a.add_argument("--args", default="{}")
    a.add_argument("--port", type=int, default=8899); a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--upstream", default="https://chatgpt.com/backend-api/codex"); a.add_argument("--via"); a.add_argument("--log")
    a = sub.add_parser("check-method", help="network-free synthetic trigger and host-contract checks")
    a.add_argument("method"); a.add_argument("--args", default="{}")
    a.add_argument("--turns", type=int, default=60); a.add_argument("--output-chars", type=int, default=4096)
    a.add_argument("--require-trigger", action="store_true"); a.add_argument("--output", required=True)
    sub.add_parser("list")
    a = sub.add_parser("install"); a.add_argument("target", choices=["codex"]); a.add_argument("--method", default="NoCompaction")
    a.add_argument("--args", default="{}"); a.add_argument("--upstream")
    a = sub.add_parser("uninstall"); a.add_argument("target", choices=["codex"])
    a = sub.add_parser("use"); a.add_argument("method"); a.add_argument("--args", default="{}")
    sub.add_parser("mcp")
    sub.add_parser("status")
    a = sub.add_parser("run"); a.add_argument("config"); a.add_argument("--workers", type=int)
    a = sub.add_parser("eval", help="plan, run, inspect or report real execution jobs")
    sub.add_parser("acm-author", help="prepare or explicitly run the pinned ACM author agent")
    sub.add_parser("analyze", help="process evaluation of a request log, evaluation directory or strict report")
    a.add_argument("eval_args", nargs=argparse.REMAINDER)
    a = sub.add_parser("doctor", help="inspect Codex; optional offline loopback mechanism check")
    a.add_argument("doctor_args", nargs=argparse.REMAINDER)
    a = sub.add_parser("methods"); a.add_argument("--html", action="store_true")
    sub.add_parser("params")
    a = sub.add_parser("curves"); a.add_argument("--manifest")
    a = sub.add_parser("fit"); a.add_argument("--manifest", required=True); a.add_argument("--output", required=True)
    a.add_argument("--sessions", nargs="+"); a.add_argument("--exclude", nargs="+", default=[])
    a.add_argument("--truncate-budget", type=int, default=2000); a.add_argument("--no-smooth", action="store_true")
    a = sub.add_parser("tune", help="offline-screen lambda and freeze a deployable policy (simulated evidence)")
    a.add_argument("--manifest", required=True); a.add_argument("--output", required=True)
    a.add_argument("--sessions", nargs="+"); a.add_argument("--exclude", nargs="+", default=[])
    a.add_argument("--lambdas", nargs="+", type=float, required=True)
    a.add_argument("--args", default="{}"); a.add_argument("--params", default="{}")
    a.add_argument("--kind", choices=["strict", "harm", "measured"], default="strict")
    a.add_argument("--reference-limit", type=int, default=230000); a.add_argument("--workers", type=int, default=1)
    a = sub.add_parser("coverage"); a.add_argument("--manifest")
    a = sub.add_parser("session"); a.add_argument("name"); a.add_argument("method"); a.add_argument("--args", default="{}")
    a.add_argument("--manifest"); a.add_argument("--preset")
    for name in ("codex", "claude", "serve", "install", "use", "session"):
        sub.choices[name].add_argument("--budget", type=int,
            help="retention budget; unit and scope depend on the selected method (ctxpress list)")
    x = ap.parse_args(argv)
    if x.cmd in ("serve", "install", "use", "session") and x.budget is not None:
        from ctxpress.methods import with_budget
        x.args = json.dumps(with_budget({"args": json.loads(x.args)}, x.budget)["args"])

    if x.cmd == "codex":
        from ctxpress.hosts.codex.launch import run as launch
        args = x.codex_args[1:] if x.codex_args[:1] == ["--"] else x.codex_args
        entry = {"class": x.method, "args": json.loads(x.args)} if x.method else None
        cmd, res = launch(entry, args, x.codex_bin, x.port, x.upstream, x.via, x.log, x.dry_run, x.store_dir,
                          **({"budget": x.budget} if x.budget is not None else {}))
        if x.dry_run:
            print(" ".join(cmd), file=sys.stderr)
        elif not x.quiet and res[1].get("requests"):
            session_summary(res[1])
        sys.exit(0 if x.dry_run else res[0])
    elif x.cmd == "claude":
        from ctxpress.hosts.claude.launch import run as launch_claude
        args = x.claude_args[1:] if x.claude_args[:1] == ["--"] else x.claude_args
        entry = {"class": x.method, "args": json.loads(x.args)} if x.method else None
        cmd, res = launch_claude(entry, args, x.claude_bin, x.port, x.upstream, x.via, x.log, x.dry_run, x.store_dir,
                                 tools=not x.no_tools, **({"budget": x.budget} if x.budget is not None else {}))
        if x.dry_run:
            print(" ".join(cmd), file=sys.stderr)
        elif not x.quiet and res[1].get("requests"):
            session_summary(res[1])
        sys.exit(0 if x.dry_run else res[0])
    elif x.cmd == "serve":
        from ctxpress.live.proxy import main as proxy_main
        proxy_main(["--method", x.method, "--args", x.args, "--port", str(x.port), "--host", x.host, "--upstream", x.upstream]
                   + (["--via", x.via] if x.via else []) + (["--log", x.log] if x.log else []))
    elif x.cmd == "install":
        from ctxpress.hosts.codex.install import install
        files = install(x.method, json.loads(x.args), x.upstream)
        print(f"ctxpress: `codex` now runs through ctxpress (method {x.method}). Added to: " + ", ".join(files))
        print("Open a new terminal (or reload your shell), then use codex as usual. Switch: ctxpress use <Method>; remove: ctxpress uninstall codex")
    elif x.cmd == "uninstall":
        from ctxpress.hosts.codex.install import uninstall
        files = uninstall()
        print("ctxpress removed from: " + (", ".join(files) or "(nothing installed)") + ". Open a new terminal.")
    elif x.cmd == "use":
        from ctxpress.hosts.codex.install import use
        c = use(x.method, json.loads(x.args))
        print(f"active method: {c['method']} {json.dumps(c['args'])} (applies to new Codex sessions)")
    elif x.cmd == "status":
        from ctxpress.live.mcp import status
        print(status())
    elif x.cmd == "mcp":
        from ctxpress.live.mcp import main as mcp_main
        mcp_main()
    elif x.cmd == "check-method":
        from ctxpress.harness.checks.method import check
        from ctxpress.harness.results.review import write
        result = check(x.method, json.loads(x.args), x.turns, x.output_chars)
        write(x.output, result)
        print(json.dumps({k: result[k] for k in ('method', 'contract_valid', 'triggered', 'operations')}))
        if x.require_trigger and not result['triggered']:
            raise SystemExit(2)
    elif x.cmd == "list":
        from ctxpress.methods import method_table
        print(method_table())
    elif x.cmd == "run":
        from ctxpress.replay.runner import run_config
        summary, md, out = run_config(x.config, workers=x.workers)
        print(md); print(f"\nwritten to {out}")
    elif x.cmd == "eval":
        from ctxpress.harness.jobs.queue import main as eval_main
        eval_main(x.eval_args)
    elif x.cmd == "doctor":
        from ctxpress.hosts.codex.doctor import main as doctor_main
        doctor_main(x.doctor_args)
    elif x.cmd == "methods":
        from ctxpress.replay.tables import framework_table
        print(framework_table(html=x.html))
    elif x.cmd == "params":
        from ctxpress.core.params import DEFAULT
        for name, v, src, note in DEFAULT.table():
            print(f"{name:<28} {str(v):<10} {src:<11} {note}")
    elif x.cmd == "curves":
        from ctxpress.replay import corpus
        from ctxpress.core import reuse
        c = reuse.Curves(corpus.load_manifest(x.manifest), smooth=False)
        for k, t in sorted(c.tab.items()):
            print(k, " ".join(f"a≤{b}: p={p:.2f} m={m:.0f}" for b, (p, m) in sorted(t.items(), key=lambda z: int(z[0]))))
    elif x.cmd in ("fit", "tune"):
        from ctxpress.replay import corpus
        from ctxpress.replay.calibrate import fit
        traces = corpus.load_manifest(x.manifest, names=x.sessions)
        known = {trace["name"] for trace in corpus.load_manifest(x.manifest, keep_text=False)}
        unknown = (set(x.sessions or []) | set(x.exclude)) - known
        if unknown:
            ap.error("unknown historical sessions: " + ", ".join(sorted(unknown)))
        traces = [trace for trace in traces if trace["name"] not in x.exclude]
        if x.cmd == "fit":
            result = fit(traces, x.output, x.truncate_budget, not x.no_smooth)
        else:
            from ctxpress.core.params import DEFAULT
            from ctxpress.replay.tune import tune
            result = tune(traces, x.output, x.lambdas, params=DEFAULT.override(json.loads(x.params)),
                method_args=json.loads(x.args), kind=x.kind, workers=x.workers,
                reference={"class":"CodexAutoCompact", "args":{"t":x.reference_limit}},
                on_fold=lambda done,total: print(f"ctxpress tune: {done}/{total} folds (simulated)", file=sys.stderr, flush=True))
        print(json.dumps(result, ensure_ascii=False))
    elif x.cmd == "coverage":
        from ctxpress.replay import corpus
        from ctxpress.core import reuse
        for (form, ct), (rate, n) in sorted(reuse.coverage_rates(corpus.load_manifest(x.manifest)).items()):
            print(f"{form:<11} {ct:<11} {rate:.2f}  (n = {n})")
    elif x.cmd == "session":
        from ctxpress.replay import corpus
        from ctxpress.core import engine
        from ctxpress.methods import build
        from ctxpress.replay.runner import preset
        traces = corpus.load_manifest(x.manifest)
        tr = next(t for t in traces if t["name"] == x.name)
        m = build({"class": x.method, "args": json.loads(x.args)}, train=[t for t in traces if t is not tr])
        r = engine.run(tr, m, preset(x.preset))
        base = engine.run(tr, build({"class": "NoCompaction"}), preset(x.preset))
        r["cost_rel"] = r["cost"] / base["cost"]
        print(m.name); print(json.dumps({k: round(v, 4) if isinstance(v, float) else v for k, v in sorted(r.items())}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    raise SystemExit(main())
