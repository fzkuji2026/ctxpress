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
    harness/, benchmarks/
               evaluation: real runs in containers, benchmark adapters and official grading, comparison and
               process evaluation. The framework above never imports them.
"""
from ctxpress.methods import REGISTRY, METHODS, build, method_table, BudgetSpec  # noqa: F401
from ctxpress.live.context import LiveContext                        # noqa: F401
from ctxpress.live.rewrite import Rewriter                           # noqa: F401
from ctxpress.live.api import ContextManager, apply                  # noqa: F401
