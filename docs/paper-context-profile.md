# 论文方法统一参数：128k 实验窗口

配置版本：`paper-context-128k-20261014-v2`。固定 16 篇论文、18 个配置，另加两个对照组（HostDefault、NoCompaction），共 20 个。本文件描述已实现的配置，不代表真实评测已经完成。

## 共同条件

- 输入上下文上限 **128000 token**，整体压缩触发参考线 **102400 token**（80%）。128k 按十进制计；131072 与 128000 之间的 3072 token 不计入窗口。
- 这是实验设计约束，不是模型物理能力声明。固定指令、工具目录、用户消息、历史与方法说明均纳入输入计数。单次生成参数沿用宿主设置，102400 不是摘要输出长度。
- 计数使用框架已有的完整请求字符数 ÷ 4 和媒体估算，并向上取整。它不是精确的模型 tokenizer；请求日志另存 provider 实际输入 token 的合规检查。实际用量超出 128000 的请求必须在验收时列为不符合窗口条件，不能因估算通过就视为合规。
- **方法分两类**（方法类的 `context_role`）：
  - **完整策略**（`full`）管理整段历史，替换宿主原生压缩：Complexity Trap Summary / Hybrid、ACON、ReSum、AgentFold、ACM、CWL、Context-Folding、FoldAct、CoMem、LongSeeker、ZipRL，以及对照组 NoCompaction。
  - **局部策略**（`local`）只处理部分内容（新进入或旧的工具输出），宿主原生压缩保持开启，触发点用宿主自己的规则（Codex 为窗口的 90%，即 115200）：Complexity Trap 遮蔽、AgentDiet、SWE-Pruner、TokenPilot、TACO、DTOC，以及对照组 HostDefault。
- **统一超限规则**（`oldest-tool-outputs-v1`）：方法处理之后，发送前的请求估算仍超过 128000 时，从最老的工具输出开始换成一行说明，直到不超过上限；删除量按 8192 token 向上取整，使下一次请求的截断点不动，缓存前缀保留。用户消息、推理、工具调用和最新一条工具输出不动；只有其他都删完时才截短最新一条（保留开头和结尾）。不调用模型，方法自身状态不变，每次使用都记录在请求日志的 `experiment_context.overflow_fallback`。宿主自己的压缩请求也走同一规则。
- 摘要/反思等辅助模型调用检查相同上限，超出时该次调用失败并记录，不截断方法的输入。
- 关闭框架通用摘要兜底。不设置 Agent 总请求数或运行时间上限。保留单完整作业、评分并发 1，以及已有评分超时；方法服务的网络超时不等于任务预算。
- 自动按容量压缩的方法使用 102400 的整体触发线；逐步、逐输出、按轮次或由 Agent 主动调用的方法保留自身机制。自主工具方法收到相同窗口说明，是否调用是待测行为：2026-10-13 的试跑中，未训练的 gpt-6.1-sol 一次也没有调用 AgentFold、ACM、LongSeeker、ZipRL 的上下文工具（工具链路已用真实 Codex CLI 验证可用）。
- 需要额外部署模型的方法：只有 SWE-Pruner（作者 0.6B 剪枝模型，本地运行）。AgentDiet 的反思模型走同一 API，不需要部署。
- 结果报告每个方法的超限规则使用次数和删除量、宿主原生压缩次数，以及第一次超限规则之前完成的里程碑。

## 每个方法的参数

| 方法 | 本轮配置 | 调整理由与保留机制 |
|---|---|---|
| HostDefault（对照） | 共同窗口配置，局部 | 只有宿主原生压缩（128000 窗口下 Codex 的默认触发点）。 |
| NoCompaction（对照） | 共同窗口配置，完整 | 不压缩，宿主原生压缩关闭，只有统一超限规则；用于衡量方法本身的作用。 |
| Complexity Trap Masking | `n=10, polling=1, pin_spec=false` | 保留最近 10 条输出的滚动遮蔽；10 是条数，不是 10k token。 |
| Complexity Trap Summary | `n=21, m=10` | 保留作者 31 轮触发、摘要前 21 轮、保留后 10 轮。 |
| Complexity Trap Hybrid | `n=43, m=10, w=10, polling=1` | 保留作者 53 轮摘要和最近 10 条输出遮蔽组合。 |
| AgentDiet | `a=2, b=1, threshold=500, min_saving=400, max_ratio=0.8` | 500 是单步可压缩条件；400/0.8 是采纳条件，均不当作总窗口。协议反思模型继续显式使用 `gpt-6-luna`，费用单列。 |
| ACON | `trigger_tokens=102400, t_hist=102400, t_obs=25600, keep_turns=2` | 整体历史触发改用完整输入估算，覆盖原 4096 历史阈值；单条观察阈值从 256 提高到 25600，保留最近两轮。 |
| ReSum | `context_k=125, trigger_tokens=102400, interval=null` | 125×1024 对应 128000；显式触发线覆盖原派生公式，取消独立 40 轮定时摘要。 |
| AgentFold | 共同窗口配置；作者折叠工具和可见标签协议 | 保留 Agent 选择范围和写摘要，不另加自动摘要器。 |
| SWE-Pruner | `threshold=0.5, min_chars=500, chunk_overlap_tokens=50` | 概率阈值、字符数及分块重叠不按窗口比例放大；仍要求关注问题和真实模型服务。没触发必须如实报告。 |
| TokenPilot | `entry_budget=25600, batch_turns=8, min_chars=256` | 单条输出截断从 2000 提高到 25600；保留每 8 次请求估计任务生命周期。256 是字符数。 |
| ACM | 共同窗口配置；`summary_model=null` | 保留主动分段归档、摘要和检索；摘要模型跟随主模型，不新增固定分段长度。 |
| CWL | `budget=102400, full_context=true` | 从 80000 改为 102400，并包含固定前缀和工具目录；依赖导致历史恢复的机制不改，上限由最终请求检查。 |
| DTOC | `strategy=true`；共同窗口配置 | 保留 Agent 主动隐藏/恢复输出，保留方法说明。 |
| Context-Folding | `max_branches=null, branch_max_turns=null, branch_timeout=null, branch_tokens=25600` | 取消原 5 次分支、64 轮、5400 秒限制；分支新增内容容量设为整体窗口的 25600 余量。该检查发生在请求边界，保留完整调用配对保护。 |
| FoldAct | `max_model_len=128000, selection_tokens=102400, max_obs_length=null` | 替换原 16384 窗口和隐含预留；关闭 4096 单条观察截断及其永久保留最后三轮的副作用。仍按作者完整轮次选择，保护项可能使目标无法满足。 |
| TACO | `uncovered_threshold=3000, freeze_rules=false` | 3000 是没有规则覆盖的输出字符数，保留规则生成、应用和反馈，不误改成 102400 token。 |
| CoMem | `trigger_tokens=102400, interval=1, lag=1` | 只在达到整体触发线时启动新异步摘要；已有摘要按一步延迟完成。仍保存完整源历史；源历史加提示词超过共同窗口时明确失败。 |
| LongSeeker | 共同窗口配置；五种作者操作 | 保留 Agent 主动决定操作，不增加统一摘要策略。 |
| ZipRL | 共同窗口配置；摘要等级 1–5 | 保留 Agent 选择等级和摘要内容，不把等级误当 token 限额。 |

25600 是本次实验采用的单条观察/分支增长参数（128000−102400），不是声称所有论文都这样设置。相关性阈值、轮数和压缩采纳比例保留，是为了不把不同算法改造成同一种方法；该配置属于统一窗口适配实验，不是作者原始超参数成绩复现。

配置由 `ctxpress.methods.paper_profile` 统一生成，经现有 `ctxpress eval paper-methods`、`ctxpress eval plan` 和方法构建入口生效。`context_profile` 存于每个方法的 `args`，会传进原有宿主和 MCP 工具进程，没有单独的 baseline 运行器。直接构造方法、不传该字段时保留旧行为，源码对照继续使用作者默认配置。

真实试跑恢复后仍需逐项验收：实际触发、压缩后续接、请求窗口合规、官方评分、完整费用、容器清理。局部窗口目标不保证一定能达到；不可压缩的固定前缀、方法不及时操作、摘要过长等情况均保留失败证据。旧试跑与本轮条件不同，不混合统计。
