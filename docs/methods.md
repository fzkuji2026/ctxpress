# 方法

`ctxpress list` 和 `ctxpress methods` 从各方法自己的声明生成方法表，列出来源、是否需要额外模型、能否真实运行以及与原实现的差别。添加新方法见 [添加一个新方法](new_method.md)。

`python -m ctxpress methods` 会从各方法自己的声明生成这张表，所以文档和代码不会对不上。

| 方法 | 类 | 来源 | 要点 |
|---|---|---|---|
| 不压缩 | `NoCompaction` | | 对照 |
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
| TokenPilot | `TokenPilot(entry_budget, a)` | 2606.17016 | 入口截断 + 生命周期清理 + 按哈希取回；LLM 打分计费 |
| SWE-Pruner | `SWEPruner(url=...)` | 2601.16746 | 接作者的 0.6B 剪枝服务；Agent 提供关注问题，输出进入上下文时只留相关行；重放使用显式标明的近似 |
| ARC | `ARC(n, label)` | 2607.25066 | 旧输出换成引用编号，按编号取回 |
| AgentFold | `AgentFold(keep_segments, deep)` | 2510.24699 | 段折叠，再深度折叠 |
| ACON | `ACON(t_hist, t_obs)` | 2510.00615 | 历史摘要 + 单条输出压缩 |
| ReSum | `ReSum(k)` | 2509.13313 | 每 k 次请求摘要 |
| Working View | `WorkingView(k, m, h, ...)` | 本项目此前的方法 | 入口预览 + 批量清理 + 块级摘要 |
| 本文：成本模型 | `CostModel(train, lam, ...)` | 本文 | 见下 |
| 自动成本策略 | `AutoCostModel(policy)` | 本文 | 加载离线筛选的 λ、统计和参数；无合格候选则原生压缩回退 |
| 旧近似（只为复现网站旧表） | `ClearThenSummarize`、`PichayApprox`、`ClawVMApprox` | 本研究 | 不是论文的方法 |

**与原版的对照：**有公开代码的方法，`repro/` 下的脚本把同样的输入交给作者的代码和 ctxpress，逐条比较输出。Complexity Trap、CliffCompaction 和 ClawVM 已通过各自记录范围的对照；Pichay 的操作次数一致，重构后有 68 次请求的存根描述受原版只追加消息的行为影响，详见 [repro/README.md](../repro/README.md)。SWE-Pruner 的 40 个输出处理用例已与作者源码对照，真实模型的轨迹核对单独记录。`ctxpress list` 列出每个方法与原版的差别。

ClawVM 的作者对照覆盖页选择算法；真实历史中的页与需求由本项目适配。全文需求按历史里的最新模型轮次生成，同一轮的并行工具输出一起处理，冻结历史首次载入和历史重建也遵循此规则。全文需求仍受作者选择器的优先级与预算约束，不能保证超预算输出保留全文。

CWL、DTOC 的字符估计已对齐 JavaScript UTF-16 长度，emoji 和其他非 BMP 字符按两个代码单元计算；新增测试直接运行作者 TypeScript，比较混合文本与预算边界前后一单位的上下文。AgentDiet 的反思与 Complexity Trap 的摘要对照仍使用确定性的假模型；实际模型输出和已发表任务分数尚未复现。Claude 已于 2026-10-04 独立复核 ClawVM 冻结历史、增量历史和并行输出；修复前的旧运行不进入后续正式比较。

**无法观测的部分：**有些论文依赖 LLM 判断内容是否相关（SWE-Pruner、TokenPilot、AgentFold），重放时观测不到这种判断，所以改用最接近的可观测规则，并在类的说明里写明。
