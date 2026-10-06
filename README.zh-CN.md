# ctxpress

**面向 LLM Agent 的上下文管理工具箱。** ctxpress 在 Agent 和模型之间插入一个上下文管理方法：每次请求前，由方法改写
Agent 发送的历史——把旧的工具输出换成占位符、截断、只留结构、删除、移入可取回的存档，或写成摘要。同一个方法可以在你自己的
Codex / Claude Code 会话里运行，也可以通过 Python 接口调用、在离线重放模拟器中筛选，或在容器里用官方 benchmark 评测。

[English](README.md) · [简体中文](README.zh-CN.md)

[![tests](https://github.com/fzkuji2026/ctxpress/actions/workflows/tests.yml/badge.svg)](https://github.com/fzkuji2026/ctxpress/actions/workflows/tests.yml) ![version](https://img.shields.io/badge/version-1.0.1-blue) ![python](https://img.shields.io/badge/python-3.10%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

## 特性

- **即插即用。** `ctxpress install codex` 包装你现有的 `codex` 命令；`ctxpress claude` 让 Claude Code 经过同一个代理。
  不修改 Agent 本身、它的配置和登录。
- **统一接口下的已发表方法。** 二十多个来自近期论文和 Agent CLI 的方法，各自是共用引擎上的一个小类，改一个参数即可切换。
  作者公开了代码的方法，`repro/` 下的脚本逐请求与原实现对照。
- **方法需要时提供 Agent 工具。** 让 Agent 自己管理上下文的方法（CWL、DTOC、ACM）通过内置 MCP 服务提供工具。
- **如实记账。** 每次请求记录方法改了什么、API 报告的用量；摘要、反思、宿主原生压缩和旁路调用分别计费，缺失的用量保持未知。
- **可复现的评测。** 8 个 benchmark 家族从原始任务在容器中执行、由官方评分器打分；计划冻结、可恢复，提供配对比较和统一的过程评测。
- **运行时只依赖 Python 标准库**（3.10+），支持 Linux、macOS 和 Windows。

## 安装

```bash
git clone https://github.com/fzkuji2026/ctxpress.git
pip install ./ctxpress
ctxpress --version
```

可选依赖：`.[sim]` 用于重放模拟器，`.[itemmodel]` 用于学习的再用模型，`.[test]` 用于测试。

## 快速上手

**Codex**

```bash
ctxpress install codex --method ComplexityTrap --budget 10   # 之后打开新终端
codex                                                         # 和平时一样使用，请求经过所选方法
ctxpress use DTOC                                             # 下次启动换用另一个方法
ctxpress status                                               # 当前方法，以及最近几次运行改了什么
ctxpress uninstall codex
```

不安装、只运行一次：`ctxpress codex --method ARC --budget 10 -- exec "fix the failing test"`。

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

**其他兼容 Responses API 的宿主：**用 `ctxpress serve` 启动代理，把宿主的 API 地址指向它。

`ctxpress list` 列出每个方法的来源、是否需要额外模型，以及与原实现的差别。

## 方法

| 方法 | 类 | 来源 |
|---|---|---|
| 不压缩（对照） | `NoCompaction` | — |
| Codex 自动压缩 | `CodexAutoCompact` | Codex CLI |
| Claude Code 压缩 | `ClaudeCode` | Claude Code |
| 滑动窗口 | `SlidingWindow` | — |
| 按 token 保留最近的工具输出 | `KeepLastTokens` | — |
| Complexity Trap：观察遮蔽、LLM 摘要、混合 | `ComplexityTrap`、`ComplexityTrapSummary`、`ComplexityTrapHybrid` | [arXiv:2508.21433](https://arxiv.org/abs/2508.21433) |
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
| 成本模型（固定参数或离线筛选的策略） | `CostModel`、`AutoCostModel` | 本项目 |
| 打分方法基类 | `ScoredMethod` | 本项目 |

包装器用于组合方法：`Composed`、`EntryTruncation`、`PinRequirements`、`WithMemory`、`Trigger`。各方法的参数、预算单位和与原实现的
差别见 [docs/methods.md](docs/methods.md)；添加新方法通常只需一个打分函数，见 [docs/new_method.md](docs/new_method.md)。

## 工作方式

```
Agent（Codex / Claude Code / 你的程序）
        │  每次请求带着完整历史
        ▼
ctxpress 代理 ── 方法改写历史（共用操作、专用存档、经同一模型写摘要）
        │  ── MCP 服务：原文取回和方法自带的 Agent 工具
        ▼
模型 API  ──►  响应流式原样返回；逐请求记录改动和用量
```

包按层组织，每层只引用它之上的层：

| 包 | 作用 |
|---|---|
| `ctxpress/core` | 条目、记账和共用操作（占位符、截断、结构化、删除、摘要） |
| `ctxpress/methods` | 每个方法一个文件，只用 core 的操作写成 |
| `ctxpress/live` | 真实运行：请求改写、代理、MCP 工具服务、用量计费 |
| `ctxpress/hosts` | Codex 和 Claude Code 接入 |
| `ctxpress/replay` | 在录制会话上做离线重放预筛（结果为模拟） |
| `ctxpress/harness`、`ctxpress/benchmarks` | 评测：作业、容器运行时、结果；每个 benchmark 家族一个包 |

`tests/test_layering.py` 检查这些依赖方向，详见 [docs/architecture.md](docs/architecture.md)。

## 评测

`ctxpress eval` 在容器里用原始 benchmark 任务运行方法，并用官方评分器打分：SWE-bench（完整集、Verified、Lite）、SWE-Milestone、
Terminal-Bench、Terminal-Bench-Science、DeepSWE、SWE-bench Pro、SWE-PolyBench 和 BigCodeBench。

```bash
ctxpress eval configure configs/experiment.protocol.json --family swe-bench --phase pilot ... --output plan-config.json
ctxpress eval plan plan-config.json --output plan.json
ctxpress eval run plan.json --directory runs/my-run --background
ctxpress eval status --directory runs/my-run
ctxpress eval review --directory runs/my-run --reference no-compaction --output runs/my-run-review
ctxpress analyze runs/my-run --output runs/my-run-analysis
```

计划在启动前冻结代码、输入、模型和价格；运行可以取消、恢复和继续；报告保留失败、缺失用量和排除记录。评测需要 Linux 和 Docker
（Windows 上用 WSL）。详见 [docs/evaluation.md](docs/evaluation.md) 和 [docs/benchmarks.md](docs/benchmarks.md)。

## 测试

```bash
pip install -e ".[test]"
python -m pytest tests -q
ctxpress smoke --output runs/smoke     # 每个方法经过真实代理，对接本地假模型各跑一遍
```

冒烟测试检查每个方法能启动、改写真实请求、在需要时调用自己的摘要 / 反思 / 剪枝模型，并输出统一统计；其中的用量是合成数据。
详见 [docs/testing.md](docs/testing.md)。

## 文档

| 主题 | 文档 |
|---|---|
| 在 Codex 和 Claude Code 里使用 | [docs/hosts.md](docs/hosts.md) |
| Python 接口 | [docs/python-api.md](docs/python-api.md) |
| 方法 | [docs/methods.md](docs/methods.md) · [添加新方法](docs/new_method.md) |
| 真实执行：代理与改写 | [docs/live.md](docs/live.md) |
| 评测与 benchmark 适配器 | [docs/evaluation.md](docs/evaluation.md) · [docs/benchmarks.md](docs/benchmarks.md) |
| 重放模拟器与成本模型 | [docs/replay.md](docs/replay.md) · [docs/cost_policy.md](docs/cost_policy.md) |
| ACM 接入 | [docs/acm.md](docs/acm.md) |
| 代码结构 | [docs/architecture.md](docs/architecture.md) |
| 测试与对照脚本 | [docs/testing.md](docs/testing.md) · [repro/README.md](repro/README.md) |
| 路线图 | [ROADMAP.md](ROADMAP.md) |

## 许可证

[MIT](LICENSE)。`ctxpress/_vendor/tomllib` 中随附的 TOML 解析器保留其原有许可证。
