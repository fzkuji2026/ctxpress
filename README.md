# ctxpress

**Context management for LLM agents.** ctxpress puts a context-management method between an agent and its
model: before every request, the method rewrites the history the agent sends — replacing old tool outputs with
placeholders, truncating, keeping only structure, deleting, moving originals to a retrievable store, or summarizing.
The same method runs in your own Codex or Claude Code session, through a Python API, in an offline replay
simulator, and in container-based evaluation on official benchmarks.

[English](README.md) · [简体中文](README.zh-CN.md)

[![tests](https://github.com/fzkuji2026/ctxpress/actions/workflows/tests.yml/badge.svg)](https://github.com/fzkuji2026/ctxpress/actions/workflows/tests.yml) ![version](https://img.shields.io/badge/version-1.0.1-blue) ![python](https://img.shields.io/badge/python-3.10%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

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
- **Reproducible evaluation.** Nine benchmark families run from the original tasks and are scored by the official
  graders, with frozen plans, recovery, paired comparison and a unified process analysis.
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

**Any other host** speaking the Responses, Anthropic Messages or Chat Completions API (vLLM, LiteLLM agents):
`ctxpress serve` starts the proxy; point the host's API base URL at it.

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
| TokenPilot: idle-age approximation / model-driven lifecycle adaptation | `TokenPilot`, `TokenPilotLifecycle` | [arXiv:2606.17016](https://arxiv.org/abs/2606.17016) |
| SWE-Pruner | `SWEPruner` | [arXiv:2601.16746](https://arxiv.org/abs/2601.16746) |
| ARC | `ARC` | [arXiv:2607.25066](https://arxiv.org/abs/2607.25066) |
| ACM | `ACM` | [arXiv:2607.23809](https://arxiv.org/abs/2607.23809) |
| AgentFold: segment approximation / agent-selected folding tool | `AgentFold`, `AgentFoldTools` | [arXiv:2510.24699](https://arxiv.org/abs/2510.24699) |
| ACON: generic guidelines / public AppWorld templates | `ACON`, `ACONSource` | [arXiv:2510.00615](https://arxiv.org/abs/2510.00615) |
| ReSum | `ReSum` | [arXiv:2509.13313](https://arxiv.org/abs/2509.13313) |
| Cost model (fixed or offline-tuned policy) | `CostModel`, `AutoCostModel` | this project |
| Scored method base class | `ScoredMethod` | this project |

Wrappers compose methods: `Composed`, `EntryTruncation`, `PinRequirements`, `WithMemory`, `Trigger`. Details,
budgets and the differences from each original are in [docs/methods.md](docs/methods.md); adding a method usually
takes one scoring function ([docs/new_method.md](docs/new_method.md)).

All variants use the same `ctxpress codex`, `ctxpress claude`, Python API and `ctxpress eval` interfaces;
author-source comparisons in `repro/` are test oracles, not standalone baseline runners.
The three new adaptations have offline integration tests, not real-model benchmark results. Their precise
reproduction scope and pinned sources are documented in [the method guide](docs/methods.md#新增源码适配与统一入口).

## Results

Preliminary results with Codex as the host (`gpt-6.1-sol`, medium reasoning). Every job runs the original task from
scratch in a container and is scored by the benchmark's official grader. Jobs are capped at 100 tool calls and 30
minutes; the host's native compaction threshold is 230k tokens. Costs use the declared rates checked on 2026-10-04
($2.00 input, $0.10 cached input, $10.00 output per million tokens). *Cost ratio* is paired against no compaction
over tasks where both sides report complete usage. Samples are small (1–3 repeats), so the numbers are descriptive.

**SWE-Milestone, Navidrome milestone chain** — official `score_1000`, mean of 3 runs per method

| Method | Mean score | Cost / run | Cost ratio | Max input | Cache-read share | History reduction |
|---|---:|---:|---:|---:|---:|---:|
| No compaction | 42.0 | $1.78 | 1.00 | 199k | 0.98 | 0% |
| Codex auto-compact (230k) | 42.0 | $2.08 | 1.17 | 226k | 0.98 | 0% |
| DTOC | 42.0 | $1.65 | **0.93** | 117k | 0.94 | 37% |
| Complexity Trap (summary) † | 42.0 | $2.04 | 1.15 | 86k | 0.92 | 54% |
| AutoCostModel † | 42.0 | $2.50 ‡ | 1.37 | 219k | 0.97 | 12% |
| KeepLastTokens | 42.0 | $4.30 | 2.42 | 99k | 0.73 | 44% |
| Complexity Trap (hybrid) † | 41.9 | $6.92 | 3.88 | 62k | 0.30 | 62% |
| Complexity Trap (masking) | 38.3 | $7.35 | 4.12 | 73k | 0.27 | 52% |
| CWL | 34.6 | $2.15 ‡ | 1.21 | 104k | 0.92 | 20% |
| ARC | 34.6 | $6.51 ‡ | 3.70 | 76k | 0.28 | 54% |
| Pichay | 30.3 | $4.95 ‡ | 2.74 | 69k | 0.42 | 59% |
| AgentDiet § | 27.1 | $2.10 ‡ | 1.15 | 52k | 0.73 | 75% |
| ClawVM | 21.4 | $2.12 | 1.19 | 66k | 0.79 | 56% |

**SWE-bench Verified, 10 instances** — 1 run per method

| Method | Resolved | Cost (10 tasks) | Cost ratio | Max input (mean) | Cache-read share |
|---|---:|---:|---:|---:|---:|
| No compaction | 8/10 | $1.17 | 1.00 | 28.4k | 0.83 |
| Codex auto-compact (230k) | 8/10 | $1.31 | 1.12 | 28.4k | 0.79 |
| AutoCostModel † | 8/10 | $1.07 | **0.92** | 26.9k | 0.85 |
| Complexity Trap (summary) | 8/10 | $1.15 | 0.99 | 26.6k | 0.81 |
| CWL | 8/10 | $1.17 (9 tasks) ‡ | 1.08 | 27.3k | 0.87 |
| KeepLastTokens | 8/10 | $1.27 | 1.09 | 29.0k | 0.84 |
| DTOC | 8/10 | $1.42 | 1.22 | 28.0k | 0.81 |
| AgentDiet | 8/10 | $1.63 | 1.40 | 20.3k | 0.76 |
| Complexity Trap (masking) | 8/10 | $1.58 (9 tasks) ‡ | 1.46 | 25.5k | 0.76 |
| Complexity Trap (hybrid) | 8/10 | $1.92 | 1.65 | 25.4k | 0.75 |
| ARC | 8/10 | $2.15 | 1.85 | 23.2k | 0.63 |
| Pichay | 8/10 | $2.30 | 1.98 | 22.7k | 0.68 |
| ClawVM | 8/10 | $2.79 | 2.39 | 22.2k | 0.76 |

† Rerun on ctxpress 1.0.1 after fixing an AutoCostModel launch defect and a summary defect that dropped the tool
catalog; the other rows come from the previous round with otherwise identical settings.
‡ Some runs lack complete usage; the cost averages only runs with complete usage, and unknown cost is never counted
as zero; process columns use the same runs. § Mean of 2 runs: the third was excluded because its request log has a numbering gap.

- **Short tasks do not separate methods.** On Verified every method resolves the same 8/10 with about 28k tokens of
  context; methods that act there only add cost.
- **Rewriting early history breaks prompt caching.** Complexity Trap masking shortens the history by 52%, but its
  cache-read share drops from 0.98 to 0.27 and it costs 4.1× the baseline. Methods that keep the prefix stable
  (DTOC, Complexity Trap summary) stay near the baseline cost; DTOC is the only one cheaper than no compaction while
  keeping all three runs at the baseline score.
- **The budget bounds this round.** At 100 tool calls the agent completes 3 of the 9 milestones and no compaction
  peaks at 199k tokens, below the 230k native threshold. A longer-budget round that fills the window is planned
  ([ROADMAP.md](ROADMAP.md)).

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
SWE-bench Pro, SWE-PolyBench and BigCodeBench. BrowseComp-Plus runs the ACM authors' own research agent behind
the proxy, so their trained checkpoint can be evaluated in the same pipeline ([docs/acm.md](docs/acm.md)).

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
