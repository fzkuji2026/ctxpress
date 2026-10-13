# 论文 baseline 固定清单

版本：`online-context-papers-20261012-v2`。固定 **16 篇有公开作者代码的在线上下文压缩论文、18 个方法配置**。AgentDiet 使用作者公开归档制品；其余为 GitHub 仓库。

这是统一宿主上的机制适配清单，不是“原论文实验全部复现”的声明。训练权重、tokenizer、消息协议和摘要模型差异必须随成绩披露。完整来源哈希见 [reference_sources.json](../repro/reference_sources.json)。

| 论文 | 统一方法配置 | 作者代码版本 | 对照脚本 |
|---|---|---|---|
| [The Complexity Trap](https://arxiv.org/abs/2508.21433) | `ComplexityTrap`, `ComplexityTrapSummary`, `ComplexityTrapHybrid` | [bf15b5fb7d27](https://github.com/JetBrains-Research/the-complexity-trap) | [complexity_trap_compare.py](../repro/complexity_trap_compare.py), [complexity_trap_summary_compare.py](../repro/complexity_trap_summary_compare.py) |
| [AgentDiet](https://arxiv.org/abs/2509.23586) | `AgentDiet` | [Figshare 62752558](https://doi.org/10.6084/m9.figshare.30073654) | [agentdiet_compare.py](../repro/agentdiet_compare.py) |
| [ACON](https://arxiv.org/abs/2510.00615) | `ACONSource` | [d63f9ae18959](https://github.com/microsoft/acon) | [acon_compare.py](../repro/acon_compare.py) |
| [ReSum](https://arxiv.org/abs/2509.13313) | `ReSumSource` | [f72f75d8c3eb](https://github.com/Alibaba-NLP/DeepResearch/tree/main/WebAgent/WebResummer) | [resum_compare.py](../repro/resum_compare.py) |
| [AgentFold](https://arxiv.org/abs/2510.24699) | `AgentFoldSource` | [f72f75d8c3eb](https://github.com/Alibaba-NLP/DeepResearch/tree/main/WebAgent/AgentFold) | [folding_compare.py](../repro/folding_compare.py) |
| [SWE-Pruner](https://arxiv.org/abs/2601.16746) | `SWEPruner` | [96171b5f3eca](https://github.com/Ayanami1314/swe-pruner) | [swe_pruner_compare.py](../repro/swe_pruner_compare.py) |
| [TokenPilot](https://arxiv.org/abs/2606.17016) | `TokenPilotLifecycle` | [9f0f19308a30](https://github.com/zjunlp/LightRSI) | [tokenpilot_compare.py](../repro/tokenpilot_compare.py) |
| [ACM](https://arxiv.org/abs/2607.23809) | `ACM` | [f06f90e728af](https://github.com/lixiaochuan2020/agentic-context-management) | [acm_compare.py](../repro/acm_compare.py) |
| [CWL](https://arxiv.org/abs/2606.11213) | `CWL` | [cd2ea3f1a69c](https://github.com/Kiz8-Team/pi-cwl) | [cwl_compare.py](../repro/cwl_compare.py) |
| [DTOC](https://arxiv.org/abs/2609.26121) | `DTOC` | [57bf2e171f08](https://github.com/chaturvediabhay24/opencode) | [dtoc_compare.py](../repro/dtoc_compare.py) |
| [Context-Folding](https://arxiv.org/abs/2510.11967) | `ContextFoldingTools` | [81626e97bb5f](https://github.com/MiaoLu3/Context_Folding) | [context_folding_compare.py](../repro/context_folding_compare.py) |
| [FoldAct](https://arxiv.org/abs/2512.22733) | `FoldActSource` | [138a54dc6c2d](https://github.com/SHAO-Jiaqi757/FoldAct) | [folding_compare.py](../repro/folding_compare.py) |
| [TACO](https://arxiv.org/abs/2604.19572) | `TACO` | [2b048988e77e](https://github.com/multimodal-art-projection/TACO) | [context_additions_compare.py](../repro/context_additions_compare.py) |
| [CoMem](https://arxiv.org/abs/2605.30842) | `CoMem` | [bb552535dc39](https://github.com/horizon-llm/CoMem) | 同上 |
| [LongSeeker](https://arxiv.org/abs/2605.05191) | `LongSeeker` | [1fc30b70cd51](https://github.com/PolarSeeker/LongSeeker) | 同上 |
| [ZipRL](https://arxiv.org/abs/2605.28069) | `ZipRL` | [8617f204ab24](https://github.com/huzhexin/ZipRL) | 同上 |

## 适配验收标准

按项目约定，以“尽力忠实复现作者代码，并让固定的18个配置通过统一入口运行”为完成标准，不要求复现发表成绩或逐字匹配发表轨迹。作者代码对照用于发现实现错误；训练权重、tokenizer、宿主协议和模型输出的差异如实记录，不作为适配完成的阻塞条件。

运行验收关注配置构建、宿主调用、压缩触发、压缩后继续执行以及用量和费用记录。合成输入、录制响应回放、真实CLI对本地假API、真实模型评测分别标注，不相互冒充；缺少费率或用量时仍保留未知。需要外部服务的方法必须接入实际服务，不能以无动作或替代规则冒充可运行。

## 接入与实验边界

- 全部方法通过同一个 `ctxpress.methods.build` 注册表进入 Codex、Claude Code 和 `ctxpress eval`；作者代码运行仅作为对照测试，不另建评测执行器。
- 后续方法取自这份清单。CliffCompaction、原生不压缩/压缩、自研成本策略、旧 AgentFold/ReSum/ACON/TokenPilot 近似版、长期记忆和静态文本压缩方案不进入新论文队列。保留其已有代码与冻结历史记录。
- Complexity Trap 包含遮蔽、LLM 摘要、混合三个已发表配置，因此 16 篇对应 18 个配置。
- `paper-context.protocol.json` 是新协议模板；所有任务待重新冻结，不继承旧实验的 169 或 49 个作业目标，不启动任何实验或扩大 benchmark 范围。新作业保持单并发。
- SWE-Pruner 必须填写真实剪枝服务地址；无服务时配置生成失败，不得用定义行/理想上界代替真实论文方法。
- `configs/experiment.protocol.json` 等旧协议只保留历史用途。新论文协议不强制加入 NoCompaction，也不附加 AutoCostModel。

## 统一入口

```bash
ctxpress eval paper-methods
# 从已有 benchmark 配置生成同目录下的新配置；不会启动作业。
ctxpress eval paper-methods --config configs/local-benchmark.json --output configs/local-papers.json --pruner-url http://127.0.0.1:8000/prune
# 随后仍使用原有的 plan / run / review 流程。
ctxpress eval plan configs/local-papers.json --output runs/paper-plan.json
```

新配置将比较参考方法设为 ComplexityTrap，保留原配置的 benchmark 与运行预算，workers 固定为 1。模型与费率仍须在实际协议中明确。使用完整协议生成器时选 [新论文协议](../configs/paper-context.protocol.json)，先冻结任务并填入服务地址；方法清单见 [机器可读清单](../configs/paper-context.methods.json)。

## 对照证据与未复现部分

本轮新增的本地合成对照：AgentFold 步骤替换/格式化 2000/2000；FoldAct 窗口选择 500/500（NumPy 张量替身）；Context-Folding 完成分支投影 286/286，覆盖 100 组会话；此前 ReSum 提示/解析/触发/续接 227/227。均不调用真实模型，不是任务成绩。其他已有源码对照见 [repro 说明](../repro/README.md)。

- **The Complexity Trap：** Summary comparisons use deterministic model substitutes; host trajectories and real model outputs differ.
- **AgentDiet：** Fake reflection model and token/compression libraries in source comparisons; Responses prefill is adapted.
- **ACON：** Published AppWorld guidelines and optimizer protocol only; no guideline optimization or distillation.
- **ReSum：** Author inference prompts and trigger; no ReSumTool weights or GRPO training; host turn/token mapping differs.
- **AgentFold：** Author step replacement/formatting and visible compress-block parsing; native host tool execution, protected history and prompt serialization differ; trained policy not loaded.
- **SWE-Pruner：** Requires real pruning endpoint; 271/405 published text matches, residual differences unresolved; heuristic fallback excluded.
- **TokenPilot：** Author selection core; host lifecycle estimation prompt, serialization and scheduling differ.
- **ACM：** Within-task context editing; common host policy, not author trained 9B policy. Fake summaries in source tests.
- **CWL：** Author filter and delimiter port; cannot prevent native host file edits inside exploration chunks.
- **DTOC：** Author output registry/visibility port; host timestamps, strategy instruction delivery and error mapping differ.
- **Context-Folding：** Host branch/return and request-boundary budget/fallback adaptation; cannot interrupt in-flight native tools or reproduce separate author generation/RL policy.
- **FoldAct：** Author inference window selector, observation-prefix clipping and persistent forced last-three-turn mode; host token estimates and native turn grouping differ; no trained weights or loss functions.

特别注意：AgentFoldSource 支持作者可见 compress 块与宿主 fold_context，但工具执行和消息序列仍由统一宿主承担；FoldActSource 不包含论文训练改进；ContextFoldingTools 已实现请求边界的分支预算返回，但不能中断正在执行的原生工具，也不另建作者独立生成器。这些方法可以研究“机制迁移到统一宿主”的效果，不能拿它们的成绩声称重现作者训练模型。

## 本地对照依赖

`pip install -e ".[test,repro]"` 安装轻量测试依赖。CWL/DTOC/TokenPilot 的原码对照还需要本机支持 TypeScript 的 Node；SWE-Pruner 模型对照单独需要作者服务和模型。对照脚本不隐式下载权重。

```bash
python -m repro.folding_compare --agentfold /prepared/agentfold --foldact /prepared/foldact --output runs/folding-source.json
python -m repro.context_folding_compare --source /prepared/context-folding --output runs/context-folding-source.json
```

## 补齐执行适配（2026-10-12）

- AgentFoldSource 接受作者 `<compress>` JSON 块，零起点范围转换为宿主步骤；最终答案不触发压缩，非法块保留历史。仍支持原生 `fold_context`。
- FoldActSource 新增 `max_obs_length`（宿主适配默认 4096）：按观察前缀截断，出现超长观察后持续仅对最近三轮应用作者窗口规则。此处使用宿主 token 估算，不声明与作者 tokenizer 逐 token 一致。
- ContextFoldingTools 新增 `branch_max_turns=64`、`branch_timeout=5400` 与可选 `branch_tokens`；在请求边界执行预算检查，先提示归还，再按作者末响应模板返回。未完成调用、媒体和用户修正继续阻止不安全删除。
- AgentFold、Context-Folding、ACM 的 MCP 控制工具补充只改变上下文而不改工作区的声明，解决无人值守宿主拒绝工具的问题。
- 真实 Codex 与 Claude Code CLI 对本地假 API 的三方法工具检查通过；Codex 还使用固定 gpt-6.1-sol 模型目录检查 Code Mode、普通 shell 命令、异步单元及控制回执。均为离线接口验收，不是模型成绩。
- SWE-Pruner 将已有真实模型响应再次通过作者与统一适配器回放，405/405 输出和调用参数一致；这没有消除与论文发表文本的 271/405 差异，二者不能混为一谈。

ReSum 的公共 Responses 摘要接口现允许作者使用的空 system 提示，只省略空 instructions，不改写作者 user 提示。固定模型目录下，真实 Codex 的原生工具执行、摘要请求和历史续接已通过本地假 API 验收。


## 2026-10-12 新增四项代码适配

四项均进入 `build`、`ctxpress list`、`eval paper-methods`、共同代理和评测入口。新清单版本 v2 为用户本次指定的扩充，旧实验与冻结记录不变。统一机制检查可用 `ctxpress check-method --help` 查看参数。

| 方法 | 已接入的作者机制 | 宿主适配及限制 |
|---|---|---|
| TACO | 规则规划、正则过滤、未覆盖输出生成规则、明确反馈恢复原文并生成替代规则 | 默认任务内规则池，可用 `rules_file` 读冻结先验；不在评测任务之间学习。Pydantic记录换成标准库；未移植Harbor终端屏幕清理。Codex Code Mode只处理完整记录的单个同步成功shell操作，混合/异步/未知复合输出保留。 |
| CoMem | 作者摘要提示词、全量源历史摘要、k步异步等待与追加新历史 | `interval`/`lag`按宿主请求计，默认均为1；不复现R2E-Gym序列化、训练和推理引擎预填优化。会话修订/临时分叉隔离摘要，退出等待已发辅助调用完成记账。 |
| LongSeeker | compress、delete、snippet、rollback、skip 五个作者操作 | 经共同MCP工具提供；稳定步号，用户修正、媒体及未完成调用受保护。rollback只改变上下文，不回滚文件；delete按作者实现跳过没有观察标记的压缩块。未加载训练策略。 |
| ZipRL | 原始五级压缩提示、摘要标签解析；Agent选择等级并提供摘要 | 使用共同 `zip_context` 工具；兼容可见摘要标签，工具输出/用户文本不能触发。原搜索循环改为宿主工具历史，未复现RL训练和作者权重。 |

TACO和CoMem的额外模型请求通过现有摘要服务和用量日志记账，可指定 `summary_model`；LongSeeker、ZipRL的摘要由主Agent输出，计入主请求。缺用量或费率仍为未知，不能填零。TACO固定规则消融可以显式设置 `freeze_rules=true`，默认仍调用作者规划器。

源码校验：42个固定版本文件哈希通过；12个顶层类/函数定义结构一致（含类内方法），ZipRL完整解析器/提示和CoMem提示一致；TACO 300组随机过滤输入一致。比较共用适配后的数据记录类型，不声称Pydantic验证、整套作者运行器或真实模型输出一致。TACO和CoMem的Apache-2.0许可证保留在 `docs/licenses/`；本次读取的LongSeeker/ZipRL源码未附许可证，不能据此宣称其有明确的开源授权许可。


运行验证：四项均通过统一机制检查；真实 Codex 与 Claude Code 各四项，共8组本地假API检查通过，验证压缩/恢复或摘要后续接。TACO、CoMem的辅助请求有独立用量日志；数值为测试夹具，不是实际账单。定向回归163项通过，另补TACO成功演化规则检查。没有新增付费模型实验或发表成绩声明。
