# ctxpress

**面向 LLM Agent 的上下文管理工具箱。** ctxpress 在 Agent 和模型之间插入一个上下文管理方法：每次请求前，由方法改写
Agent 发送的历史——把旧的工具输出换成占位符、截断、只留结构、删除、移入可取回的存档，或写成摘要。同一个方法可以在你自己的
Codex / Claude Code 会话里运行，也可以通过 Python 接口调用、在离线重放模拟器中筛选，或在容器里用官方 benchmark 评测。

[English](README.md) · [简体中文](README.zh-CN.md)

[![tests](https://github.com/fzkuji2026/ctxpress/actions/workflows/tests.yml/badge.svg)](https://github.com/fzkuji2026/ctxpress/actions/workflows/tests.yml) ![version](https://img.shields.io/badge/version-1.1.0-blue) ![python](https://img.shields.io/badge/python-3.10%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

## 特性

- **即插即用。** `ctxpress install codex` 包装你现有的 `codex` 命令；`ctxpress claude` 让 Claude Code 经过同一个代理。
  不修改 Agent 本身、它的配置和登录。
- **统一接口下的已发表方法。** 二十多个来自近期论文和 Agent CLI 的方法，各自是共用引擎上的一个小类，改一个参数即可切换。
  作者公开了代码的方法，`repro/` 下的脚本逐请求与原实现对照。
- **方法需要时提供 Agent 工具。** 让 Agent 自己管理上下文的方法（CWL、DTOC、ACM）通过内置 MCP 服务提供工具。
- **如实记账。** 每次请求记录方法改了什么、API 报告的用量；摘要、反思、宿主原生压缩和旁路调用分别计费，缺失的用量保持未知。
- **可复现的评测。** 9 个 benchmark 家族从原始任务执行、由官方评分器打分；计划冻结、可恢复，提供配对比较和统一的过程评测。
- **运行时只依赖 Python 标准库**（3.10+），支持 Linux、macOS 和 Windows。

## 安装

```bash
git clone https://github.com/fzkuji2026/ctxpress.git
pip install ./ctxpress
ctxpress --version
```

可选依赖：`.[sim]` 用于重放模拟器，`.[itemmodel]` 用于学习的再用模型，`.[analysis]` 用于 `ctxpress analyze` 的图，`.[test]` 用于测试。评测运行环境见 [docs/evaluation.md](docs/evaluation.md#运行环境)。

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

**其他宿主**（Responses、Anthropic Messages 或 Chat Completions 接口，如 vLLM、LiteLLM 写的 Agent）：用 `ctxpress serve` 启动代理，把宿主的 API 地址指向它。

`ctxpress list` 列出每个方法的来源、是否需要额外模型，以及与原实现的差别。

## 本项目方法：infinite-context

本项目的方法是 **infinite-context**（注册名 `infinite-context`；代码中目前用 `InfiniteContext(version=5, ...)` 选用，`ctxpress/methods/infinite_v5.py`）。
每次请求前执行 C_t = R_t(C_{t-1} + 新内容)，R_t 是一列按顺序执行的算子。每个算子是一组显式开关：**何时**动手
（内容进入时、一段工作结束时、话题切换时、超过阈值时），内容**变成什么**（结构化保留、头尾截断、编号占位符、摘要、交给宿主自己压缩），
**谁**写替换（机械、模型、混合），以及**是否值得做**（总是做，或按成本门槛比较节省的输入与缓存重建、写摘要、预计找回和长上下文的质量代价）。
被移出的原文存档，可用 `infinite_context_search` / `infinite_context_read` 读回。

- 默认开关与 CliffCompaction 逐请求一致（24,995 次记录请求，`repro/infinite_v5_cliff_compare.py`）；Codex 原生压缩也是一个算子。结果差异都能追溯到具体开关。
- 重放中，进入时缩短 + 工作段占位符 + 话题切换摘要一起比 CliffCompaction 少发约 28% 的 token，估算费用低约 21%；重放不能比较任务质量。
- **状态：**尚未在真实 benchmark 上评测，论文基线跑完后按同一设置评测。详见 [docs/infinite-context.md](docs/infinite-context.md#本项目方法)。

此前版本（`WorkingView`、`CostModel` / `AutoCostModel`、历史拟合的 `infinite-context` 旧版 1–4、`infinite-context-rules`）保留用于复现，不是本文方法。

## 方法
后续研究固定使用 **16 篇论文 / 18 个方法配置**，全部走统一接口。见[作者源码清单与适配差异](docs/paper-baselines.md)，或运行 `ctxpress eval paper-methods`。新论文协议拒绝旧近似版与项目自研候选。


| 方法 | 类 | 来源 |
|---|---|---|
| 宿主默认：Agent 出厂设置（对照） | `HostDefault` | Codex / Claude Code |
| 不压缩 | `NoCompaction` | — |
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
| TokenPilot：闲置步数近似 / 模型驱动的生命周期适配 | `TokenPilot`、`TokenPilotLifecycle` | [arXiv:2606.17016](https://arxiv.org/abs/2606.17016) |
| SWE-Pruner | `SWEPruner` | [arXiv:2601.16746](https://arxiv.org/abs/2601.16746) |
| ARC | `ARC` | [arXiv:2607.25066](https://arxiv.org/abs/2607.25066) |
| ACM | `ACM` | [arXiv:2607.23809](https://arxiv.org/abs/2607.23809) |
| AgentFold：片段近似 / Agent 选择折叠范围的工具 | `AgentFold`、`AgentFoldTools`、`AgentFoldSource` | [arXiv:2510.24699](https://arxiv.org/abs/2510.24699) |
| ACON：通用指南 / 作者公开 AppWorld 模板 | `ACON`、`ACONSource` | [arXiv:2510.00615](https://arxiv.org/abs/2510.00615) |
| ReSum：周期近似 / 作者推理流程适配 | `ReSum`, `ReSumSource` | [arXiv:2509.13313](https://arxiv.org/abs/2509.13313) |
| Context-Folding：分支/返回的宿主适配，非 FoldGRPO 训练复现 | `ContextFoldingTools` | [arXiv:2510.11967](https://arxiv.org/abs/2510.11967) |
| FoldAct：作者推理窗口规则，使用宿主策略 | `FoldActSource` | [arXiv:2512.22733](https://arxiv.org/abs/2512.22733) |
| **infinite-context：本项目方法**（算子列表；CliffCompaction 与 Codex 压缩都是其特例） | `InfiniteContext(version=5)` | 本项目 |
| 本项目此前版本：成本模型、Working View、历史拟合策略 | `CostModel`、`AutoCostModel`、`WorkingView`、`InfiniteContext`（旧版 1–4，由拟合策略选用）、`InfiniteContextRules` | 本项目（保留用于复现） |
| 打分方法基类 | `ScoredMethod` | 本项目 |

包装器用于组合方法：`Composed`、`EntryTruncation`、`PinRequirements`、`WithMemory`、`Trigger`。各方法的参数、预算单位和与原实现的
差别见 [docs/methods.md](docs/methods.md)；添加新方法通常只需一个打分函数，见 [docs/new_method.md](docs/new_method.md)。

所有变体共用 `ctxpress codex`、`ctxpress claude`、Python API 和 `ctxpress eval` 接口。`repro/` 中的作者源码对照用于验证实现，不是独立 baseline 运行入口。离线对照通过不等于完整复现论文，也不能替代真实评测成绩；适配范围和未复现部分见 [方法说明](docs/methods.md#新增源码适配与统一入口)。

## 结果：SWE-Milestone

Codex 0.161.0，gpt-6.1-sol（medium，Fast 档），128k 窗口，Codex 原生压缩为唯一兜底；作者 Score-1000（满分 100），每个方法每个场景 1 次。
轻场景（ripgrep、scikit-learn、go-zero）18 个配置全部完成；重场景（dubbo、element-web、nushell）因模型额度用完停止，完成 4 个方法。

| | 轻场景平均分 | 成本相对 Vanilla |
|---|---|---|
| Vanilla Codex | 81.7 | 1.00×（$18.87） |
| 18 个中的 14 个方法 | 78.9–82.7 | 0.94–1.27× |
| CWL / FoldActSource | 82.0 / 81.8 | 2.24× / 4.00× |
| AgentDiet / ComplexityTrap | 58.4 / 52.4 | 1.31× / 3.32× |

多数方法与 Vanilla 相差不到 1 分，单次运行无法区分。成本为按标准价格估算的 API 等价费用。完整表格、缓存命中、原因和有效性规则见
[docs/results-swe-milestone.md](docs/results-swe-milestone.md)。

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
Terminal-Bench、Terminal-Bench-Science、DeepSWE、SWE-bench Pro、SWE-PolyBench 和 BigCodeBench。另有 Multi-SWE-bench、SWE-bench Multilingual、
AppWorld、KernelBench、OfficeBench、Recovery-Bench、SWE-QA 和 LongBench v2 等 Harbor 任务家族，来自官方适配器，或用
`python -m ctxpress.benchmarks.convert` 转换官方发布，设置与上下文管理论文的做法对齐（例如 ACON 的 OfficeBench 划分、CliffCompaction 的
KernelBench 连续优化协议；[docs/benchmarks.md](docs/benchmarks.md)）。BrowseComp-Plus 在代理后面原样运行
ACM 作者的检索 Agent，作者训练的模型因此也走同一套评测流程（[docs/acm.md](docs/acm.md)）。

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
python tests/parallel.py            # 并行默认检查（约 3 分钟）；--all 加上扩展测试
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
| 本项目方法（infinite-context） | [docs/infinite-context.md](docs/infinite-context.md#本项目方法) |
| SWE-Milestone 结果 | [docs/results-swe-milestone.md](docs/results-swe-milestone.md) |
| 重放模拟器与成本模型 | [docs/replay.md](docs/replay.md) · [docs/cost_policy.md](docs/cost_policy.md) |
| ACM 接入 | [docs/acm.md](docs/acm.md) |
| 代码结构 | [docs/architecture.md](docs/architecture.md) |
| 测试与对照脚本 | [docs/testing.md](docs/testing.md) · [repro/README.md](repro/README.md) |
| 路线图 | [ROADMAP.md](ROADMAP.md) |

## 许可证

[MIT](LICENSE)。`ctxpress/_vendor/tomllib` 中随附的 TOML 解析器保留其原有许可证。
