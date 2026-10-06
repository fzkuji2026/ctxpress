# ctxpress

**Context management for LLM coding agents.** ctxpress puts a context-management method between an agent and its
model: before every request, the method rewrites the history the agent sends — replacing old tool outputs with
placeholders, truncating, keeping only structure, deleting, moving originals to a retrievable store, or summarizing.
The same method runs in your own Codex or Claude Code session, through a Python API, in an offline replay
simulator, and in container-based evaluation on official benchmarks.

[English](README.md) · [简体中文](README.zh-CN.md)

![version](https://img.shields.io/badge/version-1.0.0-blue) ![python](https://img.shields.io/badge/python-3.10%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

## Features

- **Plug-and-play in real agents.** `ctxpress install codex` wraps your existing `codex` command; `ctxpress claude`
  runs Claude Code through the same proxy. No changes to the agent, its configuration or its login.
- **Published methods, one interface.** Twenty-plus methods from recent papers and agent CLIs, each a small class on
  a shared engine, switched with one argument. Where authors released code, `repro/` compares ctxpress with it
  request by request.
- **Agent tools when a method needs them.** Methods that let the agent manage its own context (CWL, DTOC, ACM) get
  their tools through a built-in MCP server.
- **Honest accounting.** Every request logs what the method changed and the usage the API reported; summaries,
  reflections, native compaction and side calls are billed separately, and unknown usage stays unknown.
- **Reproducible evaluation.** Eight benchmark families run from the original tasks in containers and are scored
  by the official graders, with frozen plans, recovery, paired comparison and a unified process analysis.
- **Standard library only** at runtime (Python 3.10+), on Linux, macOS and Windows.

## Installation

```bash
git clone https://github.com/fzkuji2026/ctxpress.git
pip install ./ctxpress
ctxpress --version
```

Optional extras: `.[sim]` for the replay simulator, `.[itemmodel]` for the learned reuse model, `.[test]` for the
test suite.

## Quick start

**Codex**

```bash
ctxpress install codex --method ComplexityTrap --budget 10   # then open a new terminal
codex                                                         # runs as usual, through the method
ctxpress use DTOC                                             # switch method for the next session
ctxpress status                                               # active method and what recent runs changed
ctxpress uninstall codex
```

Run once without installing: `ctxpress codex --method ARC --budget 10 -- exec "fix the failing test"`.

**Claude Code**

```bash
ctxpress claude --method ComplexityTrap --budget 10
```

**Python**

```python
from ctxpress import ContextManager

manager = ContextManager({"class": "ComplexityTrap", "args": {"budget": 10}})
request = manager.apply({"model": model, "input": history}, session="task-1")
response = client.responses.create(**request)
```

**Any Responses-compatible host:** `ctxpress serve` starts the proxy; point the host's API base URL at it.

`ctxpress list` shows every method with its source, whether it needs a model of its own, and how it differs from
the original implementation.

## Methods

| Method | Class | Source |
|---|---|---|
| No compaction (baseline) | `NoCompaction` | — |
| Codex auto-compact | `CodexAutoCompact` | Codex CLI |
| Claude Code compaction | `ClaudeCode` | Claude Code |
| Sliding window | `SlidingWindow` | — |
| Keep the last N tokens of tool output | `KeepLastTokens` | — |
| Complexity Trap: observation masking, LLM summary, hybrid | `ComplexityTrap`, `ComplexityTrapSummary`, `ComplexityTrapHybrid` | [arXiv:2508.21433](https://arxiv.org/abs/2508.21433) |
| CliffCompaction | `CliffCompaction` | [arXiv:2609.26779](https://arxiv.org/abs/2609.26779) |
| AgentDiet | `AgentDiet` | [arXiv:2509.23586](https://arxiv.org/abs/2509.23586) |
| CWL | `CWL` | [arXiv:2606.11213](https://arxiv.org/abs/2606.11213) |
| DTOC | `DTOC` | [arXiv:2609.26121](https://arxiv.org/abs/2609.26121) |
| Pichay | `Pichay` | [arXiv:2603.09023](https://arxiv.org/abs/2603.09023) |
| ClawVM | `ClawVM` | [arXiv:2604.10352](https://arxiv.org/abs/2604.10352) |
| TokenPilot | `TokenPilot` | [arXiv:2606.17016](https://arxiv.org/abs/2606.17016) |
| SWE-Pruner | `SWEPruner` | [arXiv:2601.16746](https://arxiv.org/abs/2601.16746) |
| ARC | `ARC` | [arXiv:2607.25066](https://arxiv.org/abs/2607.25066) |
| ACM | `ACM` | [arXiv:2607.23809](https://arxiv.org/abs/2607.23809) |
| AgentFold | `AgentFold` | [arXiv:2510.24699](https://arxiv.org/abs/2510.24699) |
| ACON | `ACON` | [arXiv:2510.00615](https://arxiv.org/abs/2510.00615) |
| ReSum | `ReSum` | [arXiv:2509.13313](https://arxiv.org/abs/2509.13313) |
| Cost model (fixed or offline-tuned policy) | `CostModel`, `AutoCostModel` | this project |
| Scored method base class | `ScoredMethod` | this project |

Wrappers compose methods: `Composed`, `EntryTruncation`, `PinRequirements`, `WithMemory`, `Trigger`. Details,
budgets and the differences from each original are in [docs/methods.md](docs/methods.md); adding a method usually
takes one scoring function ([docs/new_method.md](docs/new_method.md)).

## How it works

```
agent (Codex / Claude Code / your code)
        │  full history on every request
        ▼
ctxpress proxy ── method rewrites the history (core operations, dedicated store, summaries via the same model)
        │  ── MCP server: retrieval and the method's own agent tools
        ▼
model API  ──►  streamed back unchanged; usage and changes logged per request
```

The package is layered so that each layer imports only the ones above it:

| Package | Role |
|---|---|
| `ctxpress/core` | items, accounting and the shared operations (placeholder, truncate, structure, delete, summaries) |
| `ctxpress/methods` | one file per method, written only with core operations |
| `ctxpress/live` | real execution: request rewriting, the proxy, the MCP tool server, usage accounting |
| `ctxpress/hosts` | Codex and Claude Code integration |
| `ctxpress/replay` | offline replay pre-screen on recorded sessions (results are simulated) |
| `ctxpress/harness`, `ctxpress/benchmarks` | evaluation: jobs, container runtime, results; one package per benchmark family |

`tests/test_layering.py` enforces these directions. See [docs/architecture.md](docs/architecture.md).

## Evaluation

`ctxpress eval` runs methods on original benchmark tasks inside containers and scores them with the official
graders: SWE-bench (full, Verified, Lite), SWE-Milestone, Terminal-Bench, Terminal-Bench-Science, DeepSWE,
SWE-bench Pro, SWE-PolyBench and BigCodeBench.

```bash
ctxpress eval configure configs/experiment.protocol.json --family swe-bench --phase pilot ... --output plan-config.json
ctxpress eval plan plan-config.json --output plan.json
ctxpress eval run plan.json --directory runs/my-run --background
ctxpress eval status --directory runs/my-run
ctxpress eval review --directory runs/my-run --reference no-compaction --output runs/my-run-review
ctxpress analyze runs/my-run --output runs/my-run-analysis
```

Plans freeze code, inputs, models and prices before anything starts; runs can be cancelled, recovered and resumed;
reports keep failures, missing usage and exclusions visible. Evaluation needs Linux and Docker (use WSL on
Windows). See [docs/evaluation.md](docs/evaluation.md) and [docs/benchmarks.md](docs/benchmarks.md).

## Testing

```bash
pip install -e ".[test]"
python -m pytest tests -q
ctxpress smoke --output runs/smoke     # every method through the real proxy against a local fake model
```

The smoke run checks that each method starts, rewrites real requests, calls its own summary, reflection or
pruning model when it has one, and yields the unified statistics; its usage is synthetic. See
[docs/testing.md](docs/testing.md).

## Documentation

| Topic | Document |
|---|---|
| Using ctxpress with Codex and Claude Code | [docs/hosts.md](docs/hosts.md) |
| Python API | [docs/python-api.md](docs/python-api.md) |
| Methods | [docs/methods.md](docs/methods.md) · [adding a method](docs/new_method.md) |
| Real execution: proxy and rewriting | [docs/live.md](docs/live.md) |
| Evaluation and benchmark adapters | [docs/evaluation.md](docs/evaluation.md) · [docs/benchmarks.md](docs/benchmarks.md) |
| Replay simulator and the cost model | [docs/replay.md](docs/replay.md) · [docs/cost_policy.md](docs/cost_policy.md) |
| ACM integration | [docs/acm.md](docs/acm.md) |
| Code structure | [docs/architecture.md](docs/architecture.md) |
| Tests and reproduction scripts | [docs/testing.md](docs/testing.md) · [repro/README.md](repro/README.md) |
| Roadmap | [ROADMAP.md](ROADMAP.md) |

The detailed documents are written in Chinese.

## License

[MIT](LICENSE). The vendored TOML parser in `ctxpress/_vendor/tomllib` keeps its original license.
