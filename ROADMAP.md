# ctxpress 路线图

## 目标与范围

ctxpress 是 Agent 上下文管理工具箱：所有方法复用同一个改写、存档、模型调用和记账框架，真实任务由 benchmark 官方运行器执行、官方评分器评分。本轮固定 8 个 benchmark 家族，不继续扩充。

## 已完成

| 项目 | 状态 | 边界 |
|---|---|---|
| 方法库 | 已发表方法、本文方法及组合包装器，见 [方法文档](docs/methods.md) | 与原实现的差别由 `ctxpress list` 逐项列出 |
| 宿主接入 | Codex（`install` / `use` / `doctor`）、Claude Code（`ctxpress claude`）、其他 Responses 宿主（`ctxpress serve`） | 真实 TUI 交互检查覆盖 Codex 0.159.0-alpha.12.1 与 Claude Code 2.1.59 |
| 8 类 benchmark 接入 | 各有一个原始任务的真实执行和官方评分证据 | 不代表完整 benchmark 已跑完 |
| 统一验收 | `ctxpress eval review` | 绑定冻结计划，评分、费用、排除、清理和分析分别给证据 |
| 过程评测 | `ctxpress analyze` | 效率、token、费用、上下文长度和缓存中断按原因统计，并自带一致性检查 |
| 费用记账 | 主请求、摘要、宿主原生压缩和旁路调用分别计费 | 缺用量或费率时费用显示未知，不按零计 |
| 宿主契约检查 | 每次渲染和溢出重试都检查 | 工具声明、宿主压缩状态、固定指令、媒体及调用配对；违规阻止转发 |
| 方法机制预检查 | `ctxpress check-method` | 合成历史和假摘要；不代表真实 CLI 执行或方法质量 |
| 全方法冒烟测试 | `ctxpress smoke`：每个方法经真实代理、假模型和真实 `ctxpress mcp` 跑通并输出统一统计 | 合成用量；不代表质量或费用 |
| 代码结构（v1.0.0） | 框架分层 core → methods → live → hosts；评测按职责（jobs / runtime / results / checks）和 benchmark 家族分组 | `tests/test_layering.py` 检查依赖方向 |
| ACM | 工具机制适配及作者运行桥接 | 作者 9B 权重未部署，未开始真实评测 |
| 正式比较 v7 | SWE-bench Verified 130 个、SWE-Milestone 39 个作业跑完，经统一验收：169 个作业的资源清理全部核验，缺陷排除与缺用量分别记录 | 每家族 1–3 次重复，结果是描述性的；长任务受 100 次调用预算截断 |
| 持续集成 | GitHub Actions：Linux / macOS / Windows × Python 3.10 / 3.12，跑全部测试和全方法冒烟测试 | 评测用例只在 Linux 上运行 |

## 进行中

1. 正式比较 v8：在 v1.0.0 上补跑 v7 缺失的 AutoCostModel 和受缺陷影响的两个摘要方法，并把 SWE-Milestone 的预算提高到上下文能超过宿主压缩阈值的程度，再比较方法。方案确认后才启动。

## 之后

- 其余六类家族的正式比较（需先下载各自的任务镜像和数据）。
- ACM 作者模型的实际运行：需要作者依赖、检索索引和已服务的模型权重。
- Chat Completions 宿主。
- Terminal-Bench / Science 的真实 GPU 任务、MIG，以及评分侧多服务 Compose。

## 使用入口

- 运行方法：`ctxpress codex --method <Method>`、`ctxpress claude --method <Method>`；Python 使用 `ContextManager` / `apply()`。
- 合成机制检查：`ctxpress check-method <Method> --output <new.json>`。
- 全方法冒烟测试：`ctxpress smoke --output <new-dir>`。
- 官方评测：`ctxpress eval configure / plan / run / status / cancel / recover / resume`。
- 只读验收：`ctxpress eval review --directory <run> --reference <label> --output <new-dir>`。
- 过程分析：`ctxpress analyze <log-or-report> --output <new-dir>`。
- ACM 作者运行：`ctxpress acm-author --help`；默认只准备，显式 `--execute` 才启动。

接口说明见 [评测文档](docs/evaluation.md)、[Benchmark 适配器](docs/benchmarks.md)、[ACM 接入](docs/acm.md) 和 [添加方法](docs/new_method.md)。
