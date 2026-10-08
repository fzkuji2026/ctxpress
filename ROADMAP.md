# ctxpress 路线图

## 目标与范围

ctxpress 是 Agent 上下文管理工具箱：所有方法复用同一个改写、存档、模型调用和记账框架，真实任务由 benchmark 官方运行器执行、官方评分器评分。固定 8 个编码 benchmark 家族，另加 BrowseComp-Plus 用于运行 ACM 作者训练的模型。

## 已完成

| 项目 | 状态 | 边界 |
|---|---|---|
| 方法库 | 已发表方法、本文方法及组合包装器，见 [方法文档](docs/methods.md) | 与原实现的差别由 `ctxpress list` 逐项列出 |
| 宿主接入 | Codex（`install` / `use` / `doctor`）、Claude Code（`ctxpress claude`）、其他 Responses 宿主（`ctxpress serve`） | 真实 TUI 交互检查覆盖 Codex 0.159.0-alpha.12.1 与 Claude Code 2.1.59 |
| 固定宿主版本 | 基线固定在 Codex 0.161.0（稳定版，离线兼容和工具检查通过，默认压缩阈值实测约 244.8k）；Claude Code 登记 2.1.285 stable；`HostDefault` 不覆盖宿主任何压缩设置 | 每个大版本重新适配；Claude Code 暂无额度，未评测 |
| 8 类 benchmark 接入 | 各有一个原始任务的真实执行和官方评分证据 | 不代表完整 benchmark 已跑完 |
| 统一验收 | `ctxpress eval review` | 绑定冻结计划，评分、费用、排除、清理和分析分别给证据 |
| 过程评测 | `ctxpress analyze` | 效率、token、费用、上下文长度和缓存中断按原因统计，并自带一致性检查 |
| 费用记账 | 主请求、摘要、宿主原生压缩和旁路调用分别计费 | 缺用量或费率时费用显示未知，不按零计 |
| 宿主契约检查 | 每次渲染和溢出重试都检查 | 工具声明、宿主压缩状态、固定指令、媒体及调用配对；违规阻止转发 |
| 方法机制预检查 | `ctxpress check-method` | 合成历史和假摘要；不代表真实 CLI 执行或方法质量 |
| 全方法冒烟测试 | `ctxpress smoke`：每个方法经真实代理、假模型和真实 `ctxpress mcp` 跑通并输出统一统计 | 合成用量；不代表质量或费用 |
| 代码结构（v1.0.0） | 框架分层 core → methods → live → hosts；评测按职责（jobs / runtime / results / checks）和 benchmark 家族分组 | `tests/test_layering.py` 检查依赖方向 |
| ACM | 统一方法接口与作者机制对照；作者训练模型通过 `browsecomp-plus` 家族接入统一评测 | 只用本地替身检查；作者 9B 权重、索引和真实成绩未取得 |
| 附加 benchmark 家族 | Multi-SWE-bench、SWE-bench Multilingual、AppWorld（官方适配器）；KernelBench、LongBench v2、SWE-QA、OfficeBench、Recovery-Bench（转换官方发布）；Terminal-Bench 2.0 用现有家族 | 设置按用过它们的上下文管理论文对齐（ACON、ARC、AgentDiet、CliffCompaction、CWL、SWE-Pruner）；只用本地替身检查；联网类、OpenClaw 类与未发布的 LongCLI-Bench 暂不接入 |
| Chat Completions 宿主 | 代理改写带工具的 Chat Completions 请求，单次调用原样转发并计费；可代管上游凭据 | 用本地假服务检查，尚未连接真实 vLLM |
| 正式比较 v7 | SWE-bench Verified 130 个、SWE-Milestone 39 个作业跑完，经统一验收：169 个作业的资源清理全部核验，缺陷排除与缺用量分别记录 | 每家族 1–3 次重复，结果是描述性的；长任务受 100 次调用预算截断 |
| 持续集成 | GitHub Actions：Linux / macOS / Windows × Python 3.10 / 3.12，跑全部测试和全方法冒烟测试 | 评测用例只在 Linux 上运行 |

## 本轮框架完善（2026-10-06）

- 新增 `AgentFoldTools`、`TokenPilotLifecycle`、`ACONSource`，统一注册与配置，共用 Codex / Claude Code 代理、模型服务、工具、记账与固定八家族规划。旧近似保留，新实现不替换冻结实验。
- 新增固定源码对照：TokenPilot 选择核心、ACON 模板/协议、ACM 归档边界。合成对照结果和真实成绩分开记录，见 [方法文档](docs/methods.md)。
- SWE-Pruner 检查默认严格要求发表文本和 token 数匹配；旧结果仍不通过，新增离线诊断不覆盖原证据。非法方法参数在启动前拒绝。
- `eval review` 加入按任务聚类的配对 bootstrap 与 sign-flip 检验；缺失/排除的配对不填零，单任务重复不作跨任务推断。
- 作者独立执行不作为 baseline 入口。训练策略、真实摘要对照、长上下文和多宿主正式比较仍待完成；不自动恢复已停止的实验。

## 进行中

1. 正式比较 v8：阶段 A（在 v1.0.1 上补跑 AutoCostModel 和受缺陷影响的两个摘要方法）已完成，结果并入 README；阶段 B（提高 SWE-Milestone 预算，使上下文超过宿主压缩阈值）已暂停，重新启动前确认预算和方法子集。

## 之后

- 其余六类家族的正式比较（需先下载各自的任务镜像和数据）。
- ACM 作者模型的实际运行：需要作者依赖、BrowseComp-Plus 数据与 BM25 索引、已服务的 9B 权重（显存约 24 GB 以上）。
- Terminal-Bench / Science 的真实 GPU 任务、MIG，以及评分侧多服务 Compose。

## 使用入口

- 运行方法：`ctxpress codex --method <Method>`、`ctxpress claude --method <Method>`；Python 使用 `ContextManager` / `apply()`。
- 合成机制检查：`ctxpress check-method <Method> --output <new.json>`。
- 全方法冒烟测试：`ctxpress smoke --output <new-dir>`。
- 官方评测：`ctxpress eval configure / plan / run / status / cancel / recover / resume`。
- 只读验收：`ctxpress eval review --directory <run> --reference <label> --output <new-dir>`。
- 过程分析：`ctxpress analyze <log-or-report> --output <new-dir>`。
- ACM：工具机制用 `ctxpress codex/claude --method ACM`；作者训练模型用 `ctxpress eval` 的 `browsecomp-plus` 家族（见 [ACM 接入](docs/acm.md)）。

接口说明见 [评测文档](docs/evaluation.md)、[Benchmark 适配器](docs/benchmarks.md)、[ACM 接入](docs/acm.md) 和 [添加方法](docs/new_method.md)。
