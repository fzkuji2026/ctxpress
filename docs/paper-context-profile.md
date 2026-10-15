# 论文方法统一参数：128k 实验窗口

配置版本：`paper-context-128k-20261014-v3`。固定 16 篇论文、18 个方法配置。需要额外部署模型的 SWE-Pruner 本轮不测，所以运行 17 个方法配置，加对照组 Vanilla Codex（类名 `HostDefault`：Codex 原样运行，只有它自带的压缩），共 18 个。本文件描述已实现的配置，不代表真实评测已经完成。

## 共同条件

- 输入上下文上限 **128000 token**，整体压缩触发参考线 **102400 token**（80%）。128k 按十进制计；131072 与 128000 之间的 3072 token 不计入窗口。
- 这是实验设计约束，不是模型物理能力声明。固定指令、工具目录、用户消息、历史与方法说明均纳入输入计数。单次生成参数沿用宿主设置，102400 不是摘要输出长度。
- 计数使用框架已有的完整请求字符数 ÷ 4 和媒体估算，并向上取整。它不是精确的模型 tokenizer；请求日志另存 provider 实际输入 token 的合规检查。实际用量超出 128000 的请求必须在验收时列为不符合窗口条件，不能因估算通过就视为合规。
- **方法类型**（方法类的 `context_role`，只用于报告，运行设置相同）：
  - **完整策略**（`full`）管理整段历史，自己在 102400 触发：Complexity Trap Summary / Hybrid、ACON、ReSum、AgentFold、ACM、CWL、Context-Folding、FoldAct、CoMem、LongSeeker、ZipRL。
  - **局部策略**（`local`）只处理部分内容（新进入或旧的工具输出）：Complexity Trap 遮蔽、AgentDiet、SWE-Pruner、TokenPilot、TACO、DTOC，以及对照组 Vanilla Codex。
- **唯一兜底是宿主原生压缩**：所有方法都保留 Codex 原生压缩，`model_context_window=128000`，触发点用 Codex 自己的规则。离线探针（Codex 0.161.0、假 API）确认：Codex 在发请求之前，用上一次响应的实际输入加上新内容的估算来判断，达到约 115200（窗口的 90%）就先压缩再发，不会等结果。例如上一次 108000，加一条约 10k 的新输出，就先压缩。单条工具输出由 Codex 截到约 10k token。因此 Agent 的普通请求不会超过触发线；唯一可能超过 128000 的是 Codex 的压缩请求本身：它包含压缩前的全部内容，一步里有多条长输出同时返回时会超出（探针中上一次 100000，4 条并行输出，压缩请求约 14 万）。完整策略的 102400 触发线更低，原生压缩只在方法没压下来时才出现。
- **原生压缩请求用方法当前的历史视图**：方法在代理里改写请求，Codex 本地保存的历史不随之变小；Codex 看到的用量是改写后的请求，所以本地历史会远超窗口。2026-10-14 r2 中，方法组的原生压缩请求实际输入达 31–56 万 token（Vanilla Codex 最高 11.8 万），兜底总结的内容与对照组不同，r2 因此作废。现在代理把原生压缩请求里方法已见过的条目换成方法上一次发给模型的形式（被丢弃的就不发），只有之后新增的条目（最新输出、压缩触发）原样保留；不推进方法的轮次，也不额外截断。日志的 `method_projection` 记录已知条目数、新条目数和前后估算。真实 Codex 0.161 离线探针（假 API）确认压缩请求的历史条目与普通请求一致，可逐条对应。
- ctxpress 不截断、不拒绝 Agent 请求，也不调用模型补救。发送前估算超过 128000 的请求照常转发，记录在请求日志的 `experiment_context.over_limit`，并在结果中列为不符合窗口条件。原因：真实服务返回 context_length_exceeded 时，Codex 不会压缩重试，而是直接结束本轮（探针中 HTTP 400 和流内失败都是如此），拒绝请求等于让任务失败。宿主自己的压缩请求只记录估算。
- 摘要/反思等辅助模型调用检查相同上限，超出时该次调用失败并记录，不截断方法的输入。
- 关闭框架通用摘要兜底。不设置 Agent 总请求数或运行时间上限。同时最多 2 个完整作业（各自独立容器），每个作业评分并发 1，以及已有评分超时；方法服务的网络超时不等于任务预算。
- **上游模型故障不计成绩**：Codex 会话记录里，某一轮以宿主错误类别 `server_overloaded`（"Selected model is at capacity"）结束，就说明该作业受到 provider 故障影响，作业判为基础设施失败（`provider_failure`），官方分数只作诊断，之后整作业重跑（`eval run --retry`）。ctxpress 不在请求层重试，Agent 行为保持 Codex 默认。2026-10-14 首批中 Vanilla Codex 与 Complexity Trap 同时遇到此错误，作者 harness 的恢复也因同一错误失败，该批作废。
- **方法工具状态跨恢复延续**：作者 harness 恢复 Agent 时会启动新的 Codex 与 MCP 进程，回执存储也是新的。新存储没有记录时接续上一个启动的已验证控制历史，新 MCP 进程据此重建工具状态。此前只用 Codex 当前保留的调用记录，宿主压缩后会丢掉前面的调用，r2 的 CWL 因此报 `verified control history cannot reproduce author tool state`。
- 自动按容量压缩的方法使用 102400 的整体触发线；逐步、逐输出、按轮次或由 Agent 主动调用的方法保留自身机制。自主工具方法收到相同窗口说明，是否调用是待测行为：2026-10-13 的试跑中，未训练的 gpt-6.1-sol 一次也没有调用 AgentFold、ACM、LongSeeker、ZipRL 的上下文工具（工具链路已用真实 Codex CLI 验证可用）。
- 需要额外部署模型的方法属于另一类比较，本轮不测：只有 SWE-Pruner（作者 0.6B 剪枝模型）。只有显式给出剪枝服务地址（`--pruner-url`）时才加入。AgentDiet 的反思模型走同一 API，不需要部署，照常测。
- 结果报告每个方法的宿主原生压缩次数、超过 128000 的请求数，以及第一次原生压缩之前完成的里程碑。

## 每个方法的参数

| 方法 | 本轮配置 | 调整理由与保留机制 |
|---|---|---|
| Vanilla Codex（对照，`HostDefault`） | 共同窗口配置，局部 | 只有宿主原生压缩（128000 窗口下 Codex 的默认触发点）。 |
| Complexity Trap Masking | `n=10, polling=1, pin_spec=false` | 保留最近 10 条输出的滚动遮蔽；10 是条数，不是 10k token。 |
| Complexity Trap Summary | `n=21, m=10` | 保留作者 31 轮触发、摘要前 21 轮、保留后 10 轮。 |
| Complexity Trap Hybrid | `n=43, m=10, w=10, polling=1` | 保留作者 53 轮摘要和最近 10 条输出遮蔽组合。 |
| AgentDiet | `a=2, b=1, threshold=500, min_saving=400, max_ratio=0.8` | 500 是单步可压缩条件；400/0.8 是采纳条件，均不当作总窗口。协议反思模型继续显式使用 `gpt-6-luna`，费用单列。 |
| ACON | `trigger_tokens=102400, t_hist=102400, t_obs=25600, keep_turns=2` | 整体历史触发改用完整输入估算，覆盖原 4096 历史阈值；单条观察阈值从 256 提高到 25600，保留最近两轮。 |
| ReSum | `context_k=125, trigger_tokens=102400, interval=null` | 125×1024 对应 128000；显式触发线覆盖原派生公式，取消独立 40 轮定时摘要。 |
| AgentFold | 共同窗口配置；作者折叠工具和可见标签协议 | 保留 Agent 选择范围和写摘要，不另加自动摘要器。 |
| SWE-Pruner（本轮不测） | `threshold=0.5, min_chars=500, chunk_overlap_tokens=50` | 需要额外部署模型。概率阈值、字符数及分块重叠不按窗口比例放大；仍要求关注问题和真实模型服务。没触发必须如实报告。 |
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
