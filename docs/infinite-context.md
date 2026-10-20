# infinite-context：历史拟合与可恢复压缩

2026-10-10。目标是让上下文管理尽量不引起遗忘、额外寻找和返工；无感不是已经得到的性能保证。

本版接回 `context/docs/design.zh.md` 的原文保存、逐条语义候选和缓存成本比较思路，
并复用 ctxpress 的历史复用曲线、文本覆盖评估、真实模型服务记账及检索接口。
此前基于 Cliff 的原型保留为 **infinite-context-rules**，用于对照；当前
**infinite-context** 不再调用 Cliff 压缩器。

## 本项目方法

**本项目的方法是 infinite-context**（注册名 `infinite-context`；代码中目前用 `InfiniteContext(version=5, ...)` 选用，实现在
`ctxpress/methods/infinite_v5.py`）。下文的旧版 1–4（历史拟合策略）以及 `infinite-context-rules`、`WorkingView`、
`CostModel` / `AutoCostModel` 都是此前的版本，保留用于复现，不是本文方法。

做法：每次请求前更新上下文 C_t = R_t(C_{t-1} + 新内容)，R_t 是一列按顺序执行的算子。每个算子是一组显式开关：

- **何时动手**：内容进入时 / 一段工作结束时 / 话题切换时 / 超过阈值 t 时。
- **作用范围**：开头的指令与任务从不动；最近若干轮保留；可保护用户消息和需求文档读取。
- **变成什么**：结构化保留（测试失败、搜索路径）/ 头尾截断 / 编号占位符 / 摘要 / 交给宿主自己压缩。
- **谁来写替换**：机械渲染 / 模型 / 模型写任务状态加机械行（hybrid）。
- **是否值得做**：总是做，或按成本门槛（节省的输入是否抵得上缓存重建、写摘要、预计找回和长上下文的质量代价）。
- **可找回**：被移出的原文存档，Agent 可用 `infinite_context_search` / `infinite_context_read` 读回。

三层算子默认都关：L1 内容进入时缩短单条长输出（只决定一次，之后不破坏缓存）；L2 一段工作结束时把这段的长输出成批换成占位符；
L3 话题切换时提前摘要。运行时信号（工作阶段、移除后是否重读、复用记忆）只用已发生的请求调节这些算子。

与已有方法的关系：

- 默认开关（只有一个算子：超过阈值时机械摘要）与 CliffCompaction 逐请求逐字节一致（97 条会话 × 阈值 2 万 / 5 万 / 10 万，
  共 24,995 次请求），核对脚本 `repro/infinite_v5_cliff_compare.py`。Codex 原生压缩也是一个算子
  （`repro/codex_compact_compare.py`）。因此结果差异都能追溯到具体开关。
- 重放（83 条会话，阈值 10 万）：L1 + L2 + L3 一起比 CliffCompaction 少发约 28% 的 token，估算费用低约 21%。
  重放只能比较大小和费用，不能比较任务质量。

### 下一步：逐请求自适应（设计中，2026-10-20）

上面的写法里，"开哪几层算子"是配置里写死的开关，自适应只发生在已打开的算子内部：成本门槛（`decider="cost"`）只管阈值处的摘要；
L2 要攒够阈值的 5%（`segment_min_frac`）才做；L3 一判定话题切换就执行；`work_factor` 等参数是固定常数。所以评测前要人工挑一组开关。

新设计不再预先选开关。每次请求前：

1. 列出候选动作：(a) 新进入的长输出缩短（L1，每条只决定一次）；(b) 已结束工作段的长输出成批换成占位符（L2）；
   (c) 话题切换时提前摘要（L3）；(d) 超过阈值时摘要或交给宿主压缩；(e) 不动。
2. 每个动作按成本模型算净收益：未来 H 次请求少发的输入（缓存价 + 长上下文代价 `length_penalty`），减去缓存重建
   （见下），减去预计找回（复用概率 × 找回代价，概率来自 `reuse_memory`），减去模型写替换的费用。
3. 执行净收益最大的动作（或一组互不冲突的动作）；都不为正就不动；超过硬上限时必须选一个压缩动作。

缓存重建 = 要重算的 token 数 × (1 − 缓存价) × 这次请求本来会命中缓存的概率。要重算的 token 数从第一个改动的位置
算到末尾（改完后剩下的部分，向下取整到缓存块边界），每条内容的位置已知，逐条精确计算。命中概率用真实运行记录里的命中率
按请求间隔分段估计：缓存会过期（间隔长时本来就命中不了，改动几乎没有额外代价），命中也不保证（Vanilla 实测 95%）。
现有 `_gain` 一律按"开头之后全部重算"估，会高估 L1/L2 的代价，需要改。

不变的硬约束（不是开关）：开头的指令和任务不改；规则 R1/R2（模型没看到的不改、正在用的不改）；需求文档常驻
（基线评测中 AgentDiet、ComplexityTrap 掉分都是因为压掉了 `cat` 读入的需求文档）。阶段、重读、复用记忆这些运行时信号
统一进入"预计找回"一项，不再各自调预算。原来的开关只用于消融；只允许 (d) 时仍是 CliffCompaction / Codex 原生压缩。

待解决：会话开头复用概率不准（只看本会话 AUC 0.758），要靠长期记忆；`length_penalty`
需要从数据估计；视野 H（按请求增长估计剩余请求数，或固定 20）；贪心可能短视，重放中与事后最优比较。

检验：先离线重放（与上面的重放同会话、同费率），对比三层组合和 CliffCompaction；有额度后在 SWE-Milestone 轻场景按
基线设置真实运行，对照 Vanilla Codex 和 CliffCompaction，重点看需求文档是否保住、缓存命中是否保持。
网站说明见 experiment-method.zh.html 第 9.7 节。

状态：尚未在真实 benchmark 上评测；论文方法基线（[SWE-Milestone 结果](results-swe-milestone.md)）跑完后，
按同一设置（128k、Codex 原生压缩兜底）评测本文方法。单元测试：`tests/test_infinite_v5.py`。

## 旧版 1–4：实际决策流程

1. 用户消息、系统/开发者指令、规格和媒体保留；工具调用与输出保持原有结构。
2. 工具输出由现有 LiveContext 存档。每次压缩前核对文件与原文；缺失、损坏或没有
   store_dir 时保留。新输出至少经过一次请求才成为候选。
3. 用历史拟合的复用曲线判断不同内容在不同闲置时间后再被需要的风险。
   在线只读取已经发生的重复访问和恢复动作；这些反馈可以提高风险，不把暂时没读
   当成“永远不会用”。保护近期工作及活跃文件的最新观察。
4. 产生多个操作候选：保留原文、头尾保留、按内容结构提取、可选 Agent 语义摘要。
   Agent 对一批记录输出 retention 和摘要，也能在最早记录上写阶段结论，其余记录
   留短引用。原文地址由程序添加，模型不能选择任意删除 ID。
5. 批量比较未来输入节省、缓存重建、预计恢复请求及辅助调用费用。比较保留为零收益
   的基线，仅提交正收益且满足风险约束的批次。达到压力阈值时尝试接近 target，
   不会为了达到 target 放宽风险要求。
6. 提交前重新核对任务消息、原文版本、保护状态和归档。Agent 标记 persistent 时
   规则候选也不能覆盖它；不合法返回在 control 模式保留材料。
7. 保留 Source ID、会话 ID、原文地址；`infinite_context_search` 搜索归档，
   `infinite_context_read` 分页读回，避免一次找回整段历史。归档校验失败明确报错。
8. 决策写入现有请求日志 `method_decision`，包括候选、恢复代价、辅助调用预算、
   在线重复访问、选中操作和策略哈希。遥测不重复存证据原文。

容量是外部约束。超过拟定容量且没有可接受候选时，live 抛出 HostContractError，
代理按已有约定返回 422，不悄悄删规格或按异常回退把超限请求发出去。
没有策略时保留原文；拟合结果未通过验证时禁用压缩。

## 成本与证据边界

候选成本近似为：

```
净收益 = 未来缓存输入节省
       - 被改写位置后方的缓存重建
       - 再次需要丢失细节的风险 × 一次恢复请求的费用
       - 压缩后重探索的预计开销
       - 本次辅助 Agent 调用的预计费用
```

- 复用概率、预计剩余长度、重复访问请求费用和输出长度来自训练历史；截断/结构提取
  的覆盖能力用训练历史中后续编辑的锚点估计，样本少时向零覆盖收缩。
- 相同路径再次被访问是观察量，不等于压缩造成的因果恢复成本；修改后的 Agent
  可能产生全新的搜索和修复轨迹，这需要真实分支实验。
- Agent 摘要没有被默认赋予高覆盖率，不能凭 Agent 自报“安全”跳过历史风险约束。
- 辅助调用前做乐观节省上界筛选；连乐观估计都付不起时跳过。调用后的费用估计
  用于决策，真实服务计费日志用于最终账单，两者不能混用。
- shadow 模式刻意额外调用 Agent 以收集候选，不改变规则选择；这部分开销仍计入总费用。
- 回放为新增检索工具目录补计固定输入开销；仍是字符估计，真实账单包含实际序列化后的工具定义。
- 最终真实费用 = 主模型全部请求 + 辅助模型全部请求；恢复/重试已经在主模型账单里，
  不重复加一次“恢复费”。
- 长度仍是框架估计；当前价格只支持一个显式冻结的价格档，跨长上下文价格档的
  运行须另行建模，不能用一个常数价格声称是真实账单。

## 哪些参数可以拟合

联合搜索包含以下 11 个行为参数，并保存完整候选和验证结果：

| 参数 | 控制什么 |
|---|---|
| trigger_ratio / target_ratio | 压力触发和目标，按容量比例设置 |
| work_turns | 近期工作保护范围 |
| cooldown | 评估间隔 |
| batch_size | 每批候选/Agent 评估数量 |
| summary_tokens | 单条保留/摘要预算 |
| evidence_tokens | Agent 输入证据预算 |
| horizon_scale | 历史剩余长度和信息存活时间的调整 |
| risk_scale | 恢复风险的保守程度 |
| prior_strength | 历史与在线反馈权重、覆盖率收缩 |
| min_gain | 至少需要的收益余量 |

**“允许搜索”不等于“已识别最优”。** evidence_tokens 在关闭 Agent 的回放中没有
可识别收益；Agent 策略必须进入真实对照后才能选择。其余参数也只是有限候选中的
最优回放配置，不是全局最优。搜索空间、搜索预算、模型身份、价格、容量、风险上限、
允许的任务质量损失是外部声明，不允许优化器偷偷放宽。

## 两级拟合与启用

### 1. 真实历史回放预筛选

```
python -m ctxpress infinite-fit \
  --manifest configs/infinite-context-history.yaml \
  --groups configs/infinite-context-groups.json \
  --holdout-group navidrome \
  --params configs/infinite-context-prices.json \
  --trials 24 \
  --output results/infinite-context/new-history-fit.json
```

使用现有 Retro、Payments 历史开发，Navidrome 单独留出；同一项目多个检查点必须
同组。开发组内留组验证选择配置，留出组只评估选中的配置一次，不允许拿它重新
选择、拟合统计。全部原始历史保留，策略只保存聚合数值与来源哈希。

价格示例来自仓库 `prices.gpt6.standard.json` 的 2026-10-04 声明：input 2、cached
0.10、output 10 USD / 百万 token，转换为每 token 美元；容量 260000 低于该价格表
的长上下文分档点。这是指定价格情景下的回放，不是那些历史运行的真实账单重建。
原有恢复概率、重探索等模拟假设仍在策略 parameters 表中标明来源。

所有开发组须满足静默缺失代理约束，开发平均成本要下降；留出组也须满足约束、
逐组不更贵且合计费用实际下降，否则 `evidence.enabled=false`。不压缩、费用持平
不能作为已经验证节省的证据。

### 导出待测试配置

```
python -m ctxpress infinite-candidates \
  --manifest configs/infinite-context-history.yaml \
  --groups configs/infinite-context-groups.json \
  --fit results/infinite-context/history-fit-v4.json \
  --directory results/infinite-context/new-candidates
```

它核对训练历史哈希，导出每个参数候选及 `methods.json`，不发起实验。增加
`--agent agent-prices.json` 后同时导出 off、shadow、control 三种模式，模型名称和
价格必须明确声明。导出策略标记 experiment_only，只为受控实验启用；它们不能
冒充通过独立验证的部署策略。把 methods 数组放进现有的完整评测计划即可运行。

### 2. 用真实完成任务和完整账单选择整个策略

`infinite-select` 接受各完整候选策略的实际运行结果；不生成任务分数或假账单。
输入 JSON 顶层是 `policies`（候选名称到策略文件/对象）和 `outcomes` 数组。
每条结果包含：

```
{
  "candidate": "candidate-a", "group": "project-a", "task": "task-1",
  "repeat": 0, "comparison": "frozen-protocol-and-environment-identity",
  "policy_sha256": "实际运行的策略哈希", "agent_mode": "control",
  "main_cost": 5.0, "auxiliary_cost": 0.2, "total_cost": 5.2,
  "score": 1.0, "cost_complete": true, "quality_reviewed": true
}
```

以上仅格式示例，不是实验结果。每个任务/重复必须包含 `keep` 参照以及所有候选，
同一比较身份必须对应同样的任务起点、主模型、预算与环境。score 归一化为 0–1。
质量审核和账单完整性须由上游评测流程提供，选择器不能凭这些布尔字段独立证明审核真实。

```
python -m ctxpress infinite-select --input completed-trials.json \
  --holdout-group independent-project --quality-tolerance 0 --output selection.json
```

按开发任务总费用选择符合质量约束的候选，再单独检查留出任务；留出失败即禁用，
不改选另一个在留出上表现更好的配置。报告另输出 `selection.policy.json` 供部署。
候选可以改变任意行为参数和 Agent 模式，因此这一层能选 Agent 策略及其证据预算。

### 3. 接入真实运行

```
ctxpress codex --method infinite-context \
  --args '{"policy":"/absolute/path/to/selection.policy.json"}' -- exec "任务"
```

选择器冻结测试过的 Agent 模式，运行时不能悄悄切换。普通回放策略默认 off。
测试 Agent 的策略必须包含 `agent={model,input,output,unit:"same_as_main_per_token"}`；
用 `agent_mode:"shadow"` 或 `"control"` 和明确的 `agent_model` 启用。
当前选择 DeepSeek：`deepseek-flash` 的 infinite-context 判断与摘要通过独立的
`https://api.deepseek.com/chat/completions` 路由发送，读取进程环境中的
`DEEPSEEK_API_KEY`，未设置时读取用户目录下的 `.config/ctxpress/deepseek-api-key`
（凭据文件位于仓库外，应设为 0600）；不会复用主模型的认证信息。关闭思考，要求 JSON；超时、
截断、无凭据或无用量记录器时返回失败，control 模式保留原文。
其他模型仍使用现有同上游模型服务。

`configs/infinite-context-deepseek.json` 冻结美元/每 token 的辅助模型费率，
按 2026-10-10 官方高峰未缓存价格保守预估；主模型成本也必须使用美元。
服务记录实际输入、缓存命中、输出 token 和返回模型名；真实实验结算需要结合
调用时段及账单，不能把此保守预估当成实际扣费。非思考模式和传输输出上限固定，
更改这些设置后需重新做真实任务对照。

生成包含 off/shadow/control 的 DeepSeek 实验候选：

```bash
ctxpress infinite-candidates --manifest configs/infinite-context-history.yaml \
  --groups configs/infinite-context-groups.json \
  --fit results/infinite-context/history-fit-v4.json \
  --agent configs/infinite-context-deepseek.json \
  --directory results/infinite-context/experimental-deepseek-v1
```

此命令只生成候选，不启动付费任务；旧回放策略仍保持禁用。

## 仍不能声称已经完成的验证

- 尚无本版带 Agent 压缩的完整真实任务对照；无法声称更省钱、质量更好或无感。
- 回放不能拟合尚未尝试的压缩文本对后续行为的因果影响，不能自动消除旧模拟假设。
- 所有用户要求目前以原文持续保留；未实现独立的语义任务台账。
- 阶段归并由同批 Agent 候选表达，未移植 Rust Working View 的全部后台异步生命周期。
- 归档检索针对工具输出，不是所有助手推理和完整会话的统一长期记忆库。
- 无需隐瞒这些边界：流程会拒绝把未识别参数或持平结果包装成“最优”。

## 旧版 2：2026-10-10 容量失败修复

策略 `version: 2` 仍通过 `infinite-context` 加载，由独立实现 `infinite_v2.py`
执行；version 1 的决策行为保留，旧实验不重写。

- 触发、目标、候选节省量和容量检查统一使用宿主序列化长度估计。
  触发点还扣除最近增长量的第 90 百分位与训练输出均值中的较大值，提前留空间。
- 剩余收益期用历史长度分布与几何尾部分布平滑；超过历史支持范围时明确记录
  `horizon_extrapolated`，不再骤降至一两轮。这个外推不是已校准的任务剩余时长。
- 重读概率用于预期恢复费用，不再在容量压力下冒充不可逆质量损失概率。
  `max_risk` 仅作为无压力机械机会压缩的保守筛选。容量压力下先选择可缩减的
  合格候选，再按预期恢复成本排序；允许为继续执行付费，不能把负收益描述为省钱。
- control 模式仅在触发时调用 Agent，且只采用经 Agent 审阅的摘要。
  过大的记录分块完整审阅，保留所有分块摘要；任一块失败或要求 persistent，
  整条原文保留。原文、用户要求、任务规格、媒体、近期工作与调用配对保护继续生效。
- 一次请求可执行多批，直到达到目标或耗尽可处理记录；不存在静默的机械删除兜底。
  若受保护内容、Agent 保留意见或服务失败使容量不可满足，仍明确失败并记录原因。
- 拒绝原因、宿主长度、预留量、收益期、费用、实际提交候选均写入决策日志。
  DeepSeek 的缓存未命中按输入费率计价，没有单独的缓存写入收费类别。

`history-fit-v5.json` 使用新增 go-zero 历史进行开发组回归，并搜索 24 组参数。
go-zero 已是开发数据，后续同任务运行只能称回归，不能称独立泛化验证；Navidrome
也被此前检查过，不再是新的盲测。机械回放的 Navidrome 静默损失代理未通过，
因此拟合产物仍禁用。`pilot-v2.policy.json` 显式标为 experiment_only，仅用于
Agent 机制回归及真实对照实验，不能作为已验证的部署策略。

模型证据预算和摘要质量需要真实模型实验才能识别最优值；离线联合搜索不等于
所有参数已被可靠拟合，也不保证全局最优。旧版 2 control 延后判断至触发点，因此
机械 off 模式的 cooldown/min_gain 搜索结果不构成 control 模式的实测最优值。

真实摘要回归入口（会调用已配置的 DeepSeek）：

```bash
python -m ctxpress.harness.infinite_live_check --rollout /absolute/rollout.jsonl \
  --policy results/infinite-context/pilot-v2.policy.json \
  --directory /absolute/new-regression-directory --agent-mode control
```

它经过真实 LiveContext、归档、宿主配对检查和摘要接口，但后续 Agent 行为仍是
录制轨迹，不能证明质量不下降或真实总费用降低。

## 旧版 3：完整分层工作记忆（实现，不代表效果已验证）

旧版 1/旧版 2 保留用于复现；旧版 3 必须显式使用 version=3、agent_mode=control。
下面替代前文中 旧版 1/旧版 2 的流程描述，不改变旧策略的行为。

| 原设计环节 | 旧版 3 落点 |
| --- | --- |
| 完整记录与调用组归档 | `infinite_memory.py`：内容寻址、校验、原始 wire、用户/助手/推理/调用/输出，全量保存 |
| 后台标注 | `infinite_v3.py`：有界线程池；后台只产候选，主线程才提交；新任务绑定新候选 |
| 短记录处理 | 原文纳入完整 response unit，无需单独付费标注；仅有实际缩减才提交 |
| 长记录审阅 | 完整分块审阅，再有界分层归并；失败期间原始记录保持可见 |
| goal/phase 与相似内容 | Agent 分类；相同标签或可配置词集合相似度；只合并相邻完整组，保持时序 |
| 逐条清理 | 完整 response unit 作为原子单位；调用/结果同时替换，近期完整单元保留 |
| 周期块摘要 | 每 M 次评估合并已完成组；成本门控 |
| 压力块摘要 | 连续旧历史按保护/用户边界分组，可处理工作中阶段；截止时间内重建测量并多轮归并 |
| 旧摘要再整合 | summary 与原始 unit 同为候选，层级来源 DAG 保留全部旧版本 |
| 任务状态 | 每份摘要显式保存 goals/constraints/decisions/verified/failures/unresolved/next；失败和未解决事项跨版本保守保留 |
| persistent 复评 | 压力下提供最新进度重新判断；没有有效降级结果就保留 |
| 过长摘要修复 | 一次修复、同样 schema/长度校验，修复失败不提交 |
| 原子提交 | 任务版本、来源 hash、成员可见性、近期保护及完整请求缩减逐一重验 |
| 原文恢复 | memory_read 分页返回本层及 children，memory_search 搜索全历史与阶段；已有工具输出检索保留 |
| 费用 | 主/辅助调用沿用真实服务记录；缓存重建、预期重读和摘要开销进入周期门控，压力费用明确标注 |
| 恢复观测 | 分开标记显式 memory 引用和同资源重访；不能把相关性当因果恢复 |
| 同策略拟合 | 旧版 3 禁止 off/shadow 模式；control 历史回放与真实任务用同一个 Rewriter/方法；实际任务选择器绑定模式及全部策略哈希 |

容量的触发、候选比较和提交后测量，均由真实宿主 render 回调构造完整请求，
使用同一个 billable_chars/4。它仍是字符估算，不是供应商 tokenizer。
旧版 3 因此把 capacity 当作整理预算，不凭估算制造 HTTP 422；明确的上游长度拒绝才触发
一次额外压力处理。若保护内容本身超过目标，记录 protected_floor/target_feasible，
不偷偷删除用户指令，不声称有无限容量保证。

后台标注的 deadline 约束是否可开始新调用及是否可提交；已经发出的网络调用可能
在服务超时内稍后返回，只缓存结果、不自行提交。代理正常关闭时 drain 后台任务，
保证服务用量有机会写完。强制杀进程仍可能缺用量，必须在结果完整性中报告。

原设计的“相似内容”不是去掉所有近似文本：旧版 3 用 Agent 的 goal/phase 加词集合相似度
形成候选，归并仍由 Agent 基于时间顺序做语义总结；没有引入另一个未声明的 embedding
模型。新用户消息与保护单元是不可跨越的边界。

### 参数与验证顺序

基础 11 参数仍属于策略；旧版 3 另外明确声明 long_tokens、eviction_k、compact_m、
group_tokens、similarity、pressure_seconds、max_aux_calls、annotation_workers、retry_seconds。
这些都可由候选网格变化；价格、质量容忍度、模型和容量约束不能由优化器自行放宽。

```
python -m ctxpress.replay.infinite_v3_candidates --base BASE.json --grid GRID.json --directory NEW_DIR
python -m ctxpress.harness.infinite_live_check --rollout HISTORY.jsonl --policy CANDIDATE.json --agent-mode control --directory NEW_CHECK
```

导出不派发任务。历史回放只能验证机制与在固定轨迹上的开销，不给出任务质量。
最终部署仍须由 `infinite-select` 对完整的真实 control 配对结果按开发组选择并单独
验证留出组。不能使用 旧版 2 off-mode 拟合结果宣称 旧版 3 已经最优。

执行门槛：先完成本表所有环节的实现与本地不变量检查，再做真实小模型历史回归；
只有整体流程通过后才启动新的任务对照。任何阶段失败都修复并保留证据，不以
局部通过代替完整实现，不修改已经冻结运行的旧实验。

旧版 3 集成补充：局部 unit 清理先于块合并，块候选失败不撤销已独立验证的局部候选。
达到目标就停止追加压缩。周期 K 只统计新的、已完成标注的候选身份。
同一任务/材料/预算的不合格返回负缓存；仅改变全局缩减缺口不会重复付费请求同一个摘要。
网络失败仍按退避处理，任务、证据或预算变化才形成新候选。

关键事实归并使用 `required_facts` 与 `coverage`：模型必须把每个旧约束、失败、未解决
事项对应到新状态中一个实际存在的条目；相同含义可以多对一。遗漏、类型错误或越界
对应不能提交；不提供 coverage 时保守保留原条目。这个校验证明结构上的覆盖，
**不证明语义等价**，语义损失必须由任务实测检验。归档同时保留模型原返回作为审核证据，
检索不会把这些未验证的候选当作已提交的记忆。

## 旧版 4：上下文容量与总成本共同决定压缩

旧版 4 保留 旧版 3 的完整分块、事实覆盖校验和不可变归档，但改变压缩决策：
先根据显式模型配置计算输入预算 `C − O`，再计算工作预算 `C − O − G − U`。
`C` 是配置声明的有效模型窗口，`O` 是实际发出的输出上限，`G` 是历史增长预留，
`U` 是 token 估算误差。物理容量与服务输出上限是约束，不能拟合成更大的数字。
缺少输出参数时，新版把配置缺省上限写入请求；缺少模型配置则停止发送，不能静默回退。

代理把完整请求长度与成功响应的真实输入用量配对，按实际模型校准长度；缺用量不当成零，
重试使用重试后的请求大小。实际超限拒绝会收紧估算并触发有预算的恢复，不伪造用量样本。
新历史替换旧历史时，控制器及工作线程状态隔离；旧调用可完成记账，但不能污染新历史。

控制器构造保留、完整单元摘要和阶段合并的候选组合。组合不能重复覆盖同一个原始单元，
不能跨过保护内容；每个组合重新渲染完整请求，并将共同后缀的缓存重写费用只计算一次。
满足容量与事实约束的组合按剩余输入费用、恢复成本选择；同价保持原文。
有界搜索明确记录候选剪枝和超时，不声称全局最优。若保护内容加预留已经不可行，
停止无用摘要并报告原因。最终提交继续检查任务版本、归档、截止时间与宿主调用配对。

生成摘要之前，控制器按操作类型的历史结果预测保留比例、耗时和费用。失败或 persistent
结果视为无压缩收益，缓存命中不增加样本，修复开销计入父操作。未有样本时使用显式先验。
保守费用预留与调用后文本费用估算分开记账，两者都不是服务商账单。
仍须单独检查主模型和摘要模型的完整 usage，再比较实际任务总成本与质量。

`joint_context` 声明模型约束、增长分位数与先验、长度先验、摘要预算、耗时先验、
时间成本、组合搜索宽度和预测样本参数。候选导出保留 version=4，允许搜索可调参数，
排除物理 `models`、摘要输出上限和已不使用的 trigger_ratio/target_ratio/min_gain。
价格、先验、历史拟合值与实验选择结果必须分别标注，不能将假设参数称为拟合最优。

开发配置和证据位于 `runs/prepared/infinite-context-joint-capacity/`。其 16384 输出上限是
实验主动施加的限制，不能解读为服务商最大输出。未来配对对照必须对两组施加相同上限。
本地检查与历史回放证明实现机制，不证明任务质量；go-zero 仍是开发回归。
