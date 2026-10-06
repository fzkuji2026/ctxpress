"""ctxpress: context management methods for coding agents, behind one interface.

    from ctxpress import build, LiveContext, Rewriter
    method = build({"class": "ComplexityTrap", "args": {"n": 10}})

Layout (each layer imports only the ones above it; settings depends on nothing)
    core/      the shared engine (items, accounting, operations), parameters, text operations, host sizes, reuse models
    methods/   one file per method (or family); `REGISTRY` / `METHODS` list them
    live/      real execution: LiveContext, Rewriter (requests in and out), the proxy, the MCP tool server, usage
               accounting and session telemetry
    settings   the installed configuration ($CTXPRESS_HOME)
    hosts/     agent CLIs ctxpress plugs into: codex/ (`ctxpress codex`, `ctxpress install codex`), claude/
    replay/    offline replay pre-screen on recorded sessions (results labelled simulated)
    harness/   evaluation machinery shared by every benchmark: jobs/ (plans, the background queue, control, the fixed
               protocol, frozen inputs), runtime/ (containers, the pinned Codex, model traffic, verifier services),
               results/ (reports, review, comparisons, process analysis), checks/ (interactive, method, mechanism)
    benchmarks/
               one package per benchmark family (milestone, swe, pro, polybench, bigcode, harbor, deepswe) with its
               data, agent sessions and official grading; the registry and shared planners at the top.
               harbor and swe also carry the execution engines the other families plug into.
               The framework above never imports harness or benchmarks.
"""
from ctxpress.methods import REGISTRY, METHODS, build, method_table, BudgetSpec  # noqa: F401
from ctxpress.live.context import LiveContext                        # noqa: F401
from ctxpress.live.rewrite import Rewriter                           # noqa: F401
from ctxpress.live.api import ContextManager, apply                  # noqa: F401
