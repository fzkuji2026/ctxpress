# 方法

`ctxpress list` 和 `ctxpress methods` 从各方法自己的声明生成方法表，列出来源、是否需要额外模型、能否真实运行以及与原实现的差别。添加新方法见 [添加一个新方法](new_method.md)。

`python -m ctxpress methods` 会从各方法自己的声明生成这张表，所以文档和代码不会对不上。

| 方法 | 类 | 来源 | 要点 |
|---|---|---|---|
| 宿主默认 | `HostDefault` | Codex / Claude Code | 对照：不改写请求，也不覆盖宿主的原生压缩设置；评测计划里该作业的 `compact_limit` 为空，启动器不传 `model_auto_compact_token_limit` |
| 不压缩 | `NoCompaction` | | 关闭宿主压缩，窗口满了就停 |
| Codex auto-compact | `CodexAutoCompact(t)` | Codex CLI | 超过 t 整体摘要；t = 230k 即标准 |
| Claude Code | `ClaudeCode(t, k, f)` | Claude Code | 先清旧工具输出，仍超再摘要 |
| CliffCompaction | `CliffCompaction(t, keep_recent)` | 2609.26779 | 超过 t：开头 + 机械摘要（≤500 字符的输出原样保留）+ 最近 3 轮；与原版逐请求一致 |
| Sliding window | `SlidingWindow(t)` | | 删最旧的内容 |
| Complexity Trap | `ComplexityTrap(n)` | 2508.21433 | 只留最近 N 条工具输出；与作者轨迹逐步一致 |
| Complexity Trap LLM-Summary | `ComplexityTrapSummary(n, m)` | 2508.21433 | 未摘要轮数达到 n+m 时，最早 n 轮换成检查点摘要（作者提示词原文，读未遮蔽原文）；与作者代码逐请求一致 |
| Complexity Trap 混合 | `ComplexityTrapHybrid(n, m, w)` | 2508.21433 | LLM-Summary（43, 10）后接只留最近 W = 10 条工具输出；与作者代码逐请求一致 |
| 按 token 只留最近工具输出 | `KeepLastTokens(b)` | §4 对照组 | |
| AgentDiet | `AgentDiet(a, b, threshold, reflect_model)` | 2509.23586 | 每步之后反思模型压缩第 s−a 步（超过 θ token），整步换成压缩文本；与作者反思模块逐步一致 |
| CWL | `CWL(budget)` | 2606.11213 | Agent 用 delimiter 工具标注探索 / 行动片段及依赖；超过阈值从最早的已完成片段逐级剥离；与作者 filterContext、delimiter 工具逐轮一致 |
| DTOC | `DTOC(strategy)` | 2609.26121 | 每条工具输出带 tool_key 外壳；Agent 用 manage_context 隐藏 / 恢复；与作者编号、工具与外壳逐轮一致 |
| Pichay | `Pichay(age, min_size)` | 2603.09023 | 4 轮前的大输出换成存根；缺页后内容未变则固定；操作次数与原版一致，68 次存根描述有差异 |
| ClawVM | `ClawVM(budget, policy)` | 2604.10352 | 预算内为每条输出选全文 / 压缩 / 结构 / 指针；选择算法与原版一致 |
| TokenPilot 旧近似 | `TokenPilot(entry_budget, a)` | 2606.17016 | 入口截断 + 闲置步数清理；没有真实 LLM 判断，打分开销仅用于模拟 |
| TokenPilot 生命周期适配 | `TokenPilotLifecycle(entry_budget, batch_turns, estimator_model)` | 2606.17016 | 真正调用模型判断任务生命周期，按批次淘汰；共用摘要调用与记账服务 |
| SWE-Pruner | `SWEPruner(url=...)` | 2601.16746 | 接作者的 0.6B 剪枝服务；Agent 提供关注问题，输出进入上下文时只留相关行；重放使用显式标明的近似 |
| ARC | `ARC(n, label)` | 2607.25066 | 旧输出换成引用编号，按编号取回 |
| AgentFold | `AgentFold(keep_segments, deep)` | 2510.24699 | 段折叠，再深度折叠 |
| AgentFold 自主工具适配 | `AgentFoldTools()` | 2510.24699 | Agent 自己选择完整步骤范围并写摘要，可再次合并旧折叠 |
| ACON | `ACON(t_hist, t_obs)` | 2510.00615 | 历史摘要 + 单条输出压缩 |
| ACON 作者公开模板适配 | `ACONSource(t_hist, t_obs, keep_turns, summary_model)` | 2510.00615 | 公开 AppWorld 初始模板、阈值、输出标记解析；未运行指南优化器或蒸馏 |
| ReSum | `ReSum(k)` | 2509.13313 | 每 k 次请求摘要 |
| Working View | `WorkingView(k, m, h, ...)` | 本项目此前的方法 | 入口预览 + 批量清理 + 块级摘要 |
| 本文：成本模型 | `CostModel(train, lam, ...)` | 本文 | 见下 |
| 自动成本策略 | `AutoCostModel(policy)` | 本文 | 加载离线筛选的 λ、统计和参数；无合格候选则原生压缩回退 |
| 旧近似（只为复现网站旧表） | `ClearThenSummarize`、`PichayApprox`、`ClawVMApprox` | 本研究 | 不是论文的方法 |

**与原版的对照：**有公开代码的方法，`repro/` 下的脚本把同样的输入交给作者的代码和 ctxpress，逐条比较输出。Complexity Trap、CliffCompaction 和 ClawVM 已通过各自记录范围的对照；Pichay 的操作次数一致，重构后有 68 次请求的存根描述受原版只追加消息的行为影响，详见 [repro/README.md](../repro/README.md)。SWE-Pruner 的 40 个输出处理用例已与作者源码对照，真实模型的轨迹核对单独记录。`ctxpress list` 列出每个方法与原版的差别。

ClawVM 的作者对照覆盖页选择算法；真实历史中的页与需求由本项目适配。全文需求按历史里的最新模型轮次生成，同一轮的并行工具输出一起处理，冻结历史首次载入和历史重建也遵循此规则。全文需求仍受作者选择器的优先级与预算约束，不能保证超预算输出保留全文。

CWL、DTOC 的字符估计已对齐 JavaScript UTF-16 长度，emoji 和其他非 BMP 字符按两个代码单元计算；新增测试直接运行作者 TypeScript，比较混合文本与预算边界前后一单位的上下文。AgentDiet 的反思与 Complexity Trap 的摘要对照仍使用确定性的假模型；实际模型输出和已发表任务分数尚未复现。Claude 已于 2026-10-04 独立复核 ClawVM 冻结历史、增量历史和并行输出；修复前的旧运行不进入后续正式比较。

**无法观测的部分：**有些论文依赖 LLM 判断内容是否相关（SWE-Pruner、TokenPilot、AgentFold），重放时观测不到这种判断，所以改用最接近的可观测规则，并在类的说明里写明。

## 新增源码适配与统一入口

旧 `TokenPilot`、`AgentFold`、`ACON` 保留原有行为，已有冻结实验不会被新适配冒名替换。
新增三种实现仍是 `Method`，通过同一个 `build()` 注册表使用：

```bash
ctxpress codex --method TokenPilotLifecycle --args '{"batch_turns":8}'
ctxpress claude --method ACONSource --args '{"t_hist":4096,"t_obs":256}'
ctxpress codex --method AgentFoldTools
ctxpress check-method TokenPilotLifecycle --require-trigger --output lifecycle-check.json
ctxpress smoke --method AgentFoldTools --method TokenPilotLifecycle --method ACONSource --output smoke-new
```

`check-method` 和 `smoke` 使用合成历史与假模型。评测配置的 `methods` 可以直接选择相同类名；固定八家族共用规划、队列、取消/恢复、官方评分、报告和验收链路。模型判断及观察/历史摘要通过 `model_text` 发出，进入 `summary` 调用方费用，可按模型拆分。`AgentFoldTools` 的摘要由主 Agent 在工具参数中生成，费用已包含在主请求输出，不虚增第二次摘要调用。

| 实现 | 已核对范围 | 明确未复现的部分 |
|---|---|---|
| `TokenPilotLifecycle` | [LightRSI](https://github.com/zjunlp/LightRSI/tree/9f0f19308a30445438779efbf3108a7d965a55c7) 的任务归属优先级、淘汰候选选择与顺序，300 个合成案例 | 自定义估计提示词，按代理请求批次调度，输出块与入口截断均有适配；不是完整 LightRSI。另保护最新输出及仍有活动任务共享的输出 |
| `ACONSource` | [微软 ACON](https://github.com/microsoft/acon/tree/d63f9ae18959dc7215ff62899c94c5e8c56847ae) 的公开初始模板、渲染、参数、解析、阈值，共 117 项检查 | 宿主历史序列化和 token 计数不同；未运行优化器、蒸馏或训练后模型。公开初始模板不等于作者最佳优化指南 |
| `AgentFoldTools` | 共同接口和保护约束测试；实现单步折叠与多步再折叠 | 本轮未取得作者 AgentFold 运行实现做逐条对照；原文四块响应和每轮折叠协议改为独立工具，范围选择更灵活；未加载 30B 训练策略 |
| `ACM` | 作者源码对照：500 次规范化上下文、127 份归档；固定版本及文件哈希见 `repro/reference_sources.json` | 同一假摘要、明确的信封/空范围错误文本归一化；未比较真实摘要、提示词、查询、训练后 9B 策略或原 benchmark 成绩 |

以上新增对照是离线逻辑证据，不是新的正式评测成绩。无依赖安装或独立 baseline 服务；作者文件只在对照测试中执行，并先校验固定哈希。引用 ARC 作者代码时使用“尚未找到公开实现”，不把检索结果当成不存在的证明。TokenPilot 原论文本身讨论缓存效率，不能将“考虑缓存”单独作为本项目的新颖性依据。
