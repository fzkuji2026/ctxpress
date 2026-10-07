# 真实实验的统一入口

当前固定范围为 8 个 benchmark 家族，固定实验协议在 [experiment.protocol.json](../configs/experiment.protocol.json)。任务选择、计划、状态、取消、恢复、重试和报告的本地检查与真实官方成绩分别记录；下文是接口契约，不代表某次运行的结果。各适配器的执行与评分细节见 [Benchmark 适配器](benchmarks.md)。

## 概览

**统一评测入口：**固定 8 个 benchmark 家族使用同一 `ctxpress eval` 命令。`benchmarks --start-mode task_start` 查看从头支持范围，`tasks` 发现本地题目，`plan` 冻结可审查设置，`run --background` 后台派发，`status` 查看进度，`cancel` 停止任务并清理所属资源，`recover` 仅恢复已停止资源，`resume --background` 重试未完成任务，`report` 生成 JSON/HTML，`compare --reference` 按任务和重复编号比较八类的原生质量指标与费用。恢复保留已完成成绩和每次尝试的产物，运行所需数据、源码、依赖、镜像及认证仍需显式准备。见 [docs/evaluation.md](evaluation.md)。

**Benchmark 接入：**`ctxpress eval benchmarks` 列出内置适配器及支持范围，`ctxpress eval tasks --benchmark swe-milestone --scripts /path/to/scripts --min-context 128000 --gradable` 查询本地任务 ID。评测配置可指定 `benchmark` 和 `tasks: ["n0-j435"]`，由目录展开并冻结上下文边界；方法、Codex 后端和 benchmark 分别选择，计划与报告保留 benchmark 身份。SWE-Milestone 的本地 Navidrome 录制边界执行和官方评分仍需准备已有脚本、历史、镜像及评分资源。SWE-bench（Verified/Lite/完整集）的从头 Codex 执行、补丁导出与独立官方评分调用代码已连接；Terminal-Bench/Science 的 Harbor CPU/NVIDIA GPU 执行和 verifier 调用代码已连接。各适配器的支持范围与验证边界见 [Benchmark 适配器](benchmarks.md)，不由接口声明推断。

各家族使用同一配置结构，下面模板中的模型、任务 ID 和准备路径需替换为实际值。

| 家族 | 从头配置模板 |
|---|---|
| SWE-Milestone | [swe_milestone.example.json](../configs/swe_milestone.example.json) |
| SWE-bench（Full/Verified/Lite） | [swe_bench.example.json](../configs/swe_bench.example.json) |
| Terminal-Bench | [terminal_bench.example.json](../configs/terminal_bench.example.json) |
| Terminal-Bench-Science | [terminal_science.example.json](../configs/terminal_science.example.json) |
| DeepSWE | [deep_swe.example.json](../configs/deep_swe.example.json) |
| SWE-bench Pro | [V1](../configs/swe_pro_v1.example.json)、[V2](../configs/swe_pro_v2.example.json) |
| SWE-PolyBench | [polybench.example.json](../configs/polybench.example.json) |
| BigCodeBench | [bigcodebench.example.json](../configs/bigcodebench.example.json) |

适配器目录中的 `real_run_verified=false` 是静态能力声明，单题验证不会把整个家族标为已验证。具体运行应检查官方原始评分、请求日志、冻结输入和清理证据；

目前 8 个家族各有一个原始任务的真实执行和官方评分证据；SWE-bench Verified 与 SWE-Milestone 上的正式方法比较正在进行，结果随论文发布。首题结果不代表完整 benchmark 已跑完，也不说明任何上下文方法的效果。

### 固定实验协议

固定方案在 [experiment.protocol.json](../configs/experiment.protocol.json)：8 个家族、明确的主模型/反思模型、方法参数、任务预算和重复次数。首轮正式比较集中在 SWE-bench Verified 和 SWE-Milestone，回答已发表方法在统一真实宿主与计价下的质量和成本表现；AutoCostModel 单列为探索性候选。原始任务 ID 已固定（Verified 10 题，其余六类按 seed 各选 10 题，Milestone 为完整 Navidrome itinerary），官方数据 revision、原始文件和所选附件均已核对。可在仓库根目录只读盘点本地资源（数据默认在仓库旁的 `../data`，SWE-Milestone 作者代码由 `CTXPRESS_MILESTONE_AUTHOR_CODE` 指定）：

```bash
python -m repro.evaluation_inventory --output runs/prepared/inventory.json
```

固定协议可通过 `ctxpress eval configure` 生成普通评测配置，避免手工复制模型、方法参数及任务集合：

```bash
ctxpress eval configure configs/experiment.protocol.json \
  --family swe-milestone --phase pilot --data /prepared/swe-milestone/navidrome_navidrome_v0.57.0_v0.58.0 \
  --bindir /path/to/codex-bin --resources /prepared/milestone-resources.json \
  --prices configs/prices.gpt6.standard.json \
  --output runs/next/milestone-pilot.json
ctxpress eval configure configs/experiment.protocol.json \
  --family swe-bench --phase pilot --data /prepared/swe-verified/dataset \
  --bindir /path/to/codex-bin --prices configs/prices.gpt6.standard.json \
  --output runs/next/verified-pilot.json
ctxpress eval plan runs/next/verified-pilot.json --output runs/next/verified-pilot.plan.json
```

这些命令只读取本地输入并写出配置/来源哈希和资源缺项，不启动下载、容器或模型。配置未声明费率时费用保持未知，缺资源的计划不可启动。`--phase comparison` 加入协议中固定的 12 个基线及必需的 AutoCostModel 候选，验证策略/训练来源哈希和训练任务排除；其他六类也已冻结原始任务 ID，不隐式选择全部任务。生成器拒绝覆盖已有配置，重新生成应使用新输出路径。`artifact_root` 相对协议文件解析，候选策略写为绝对路径；移动配置目录不会改变所选策略。详见 [配置生成说明](evaluation.md#从固定协议生成配置)。

基础链路通过后用 `--phase method-pilot` 生成首题上的 7 方法检查，直接继承正式基线参数，1 次重复、单 worker。正式比较须用 `configure --model-catalog <models.json>` 显式冻结宿主的模型目录，避免 Codex 回退到内置模型元数据、改变宿主提示词和工具接口。

各适配器的执行、评分和资源准备细节见 [Benchmark 适配器](benchmarks.md)。

## 统一验收与分层状态

```bash
ctxpress eval review --directory /runs/frozen-evaluation --reference no-compaction \
  --exclusions reviewed-exclusions.json --inspect-resources --output /reports/new-review
ctxpress eval benchmarks --start-mode task_start --evidence /reports/new-review/review.json
```

`review` 只读冻结队列，不执行模型、评分、取消或重试。新目录保存严格比较、缺陷排除、当前资源清理、配对覆盖及 `analysis/` 过程报告。原始评分和费用始终保留；缺陷排除只影响方法效果比较。质量配对与费用配对分别计数，不将未知费用当零。

每个候选的 `statistics` 另给出费用及各原生质量指标的探索性配对分析。仅纳入该指标所有计划重复均可比较的任务；先在任务内平均候选减参考的差值，再对任务等权重做 2,000 次 bootstrap，报告 95% percentile 区间。sign-flip 双侧检验在不超过 16 个任务时精确枚举，否则用固定种子的 Monte Carlo 与 +1 校正。`report.md` 也显示费用区间。缺陷筛查和过程分析完成后才计算，不跨 benchmark 混合。

独立任务不足两个时区间和检验明确不可用；一条 Navidrome itinerary 跑三次仍只有一个任务。小样本、未校正的多重比较、符号可交换性假设及任务选择偏差均在输出中注明；结果不证明随机种子匹配、非劣性或整个 benchmark 的质量结论。原始描述性汇总仍保留。

缺陷审查声明绑定单个冻结计划与代码源；排除条目需给原因和已验证的 JSON 证据哈希。例如：

```json
{
  "schema": "ctxpress.eval.exclusions",
  "version": 1,
  "plan_sha256": "计划的64位哈希",
  "code_sha256": "运行代码的64位哈希",
  "review_note": "说明已检查的缺陷范围和限制",
  "excluded": [
    {"job_id": "m003-t000-r000", "reason": "已复现的宿主工具目录缺陷",
     "evidence": [{"path": "/evidence/reproduction.json", "sha256": "证据的64位哈希"}]}
  ]
}
```

审查后没有需要排除的作业可以明确填 `excluded: []`；省略整个 `--exclusions` 则为“未审查”，不会进入方法效果聚合。审查记录不能证明不存在其他缺陷。`--inspect-resources` 仅查询 Docker 标签和所属 worker；不传此参数、Docker 查询失败或缺少清理回执时，清理保持未验证。活跃作业不要求已清理。报告输出不得覆盖既有文件或写入冻结证据目录。

`benchmarks --evidence` 分开列出静态适配能力、绑定资源、真实任务验证数量和方法操作观察数量，并明确 task、method、release、plan 与 source 范围。旧的静态 `real_run_verified` 字段不作为整套 benchmark 已验证的证明；真实证据来自绑定的 `verification_evidence`。记录过资源不等于当前机器依赖仍然可用。

## 批量前的机制预检查

```bash
ctxpress check-method ComplexityTrapSummary --turns 60 --require-trigger --output summary-check.json
```

此命令不联网、不调用真实模型，使用确定性摘要分别检查逐步增长、一次载入长历史与宿主修订历史。报告分别给出宿主契约和操作计数。`--require-trigger` 未触发时以状态码 2 退出，避免把“请求能通过”误当成“方法已触发”；原生压缩需要真实宿主验证，不在这里模拟。外部剪枝服务等没有纯离线 fixture 的方法会明确拒绝，不偷偷联网。

每次 Responses 渲染与溢出重试都会检查工具声明、宿主 compaction 状态、固定指令、媒体及调用配对。媒体周围的文字可以按宿主协议压缩，媒体内容及所属工具不能丢失。发现契约错误时阻止该请求转发，记录 `host_contract_failed`，在执行健康检查里标为无效；这不代替真实 CLI 的执行验证。真实 CLI 本地假 API 检查仍使用 `doctor --offline-check`。

ACM 工具适配与作者模型独立入口见 [ACM 接入](acm.md)。

## 从固定协议生成配置

`configure` 使用协议中的明确模型、方法参数、精确任务 ID、重复次数及 Agent 预算，生成 `ctxpress.eval` 配置和同名 `.provenance.json` 来源记录。它复用各家族模板的官方评分选项，再由实际规划器验证任务和资源；不运行 Docker、评分器或模型。

正式比较的重复次数默认取 `comparison.repeats`；某个家族可在自己的 `families` 条目中显式声明正整数 `comparison_repeats`。这个覆盖只影响该家族的 `--phase comparison`，不改变试跑、其他家族、题目集合或方法参数。来源哈希绑定这项设置，规划后的作业数与 Agent 时间预算按有效重复次数展开。调整长短任务分配时应先修改协议并重新生成计划，不修改已启动的运行；未声明覆盖时保持原设置。

```bash
ctxpress eval configure configs/experiment.protocol.json --family swe-bench --phase pilot \
  --data /prepared/swe-verified --bindir /prepared/codex-bin \
  --model-catalog /prepared/codex-models.json \
  --resources /prepared/swe-bench-resources.json --output runs/proposals/verified-pilot.json
ctxpress eval plan runs/proposals/verified-pilot.json --output runs/proposals/verified-pilot.plan.json
```

`--phase method-pilot` 使用同一首题、1 次重复和单 worker，检查 NoCompaction、CodexAutoCompact、ComplexityTrap、ComplexityTrapHybrid、AgentDiet、CWL、DTOC，共 7 个作业。它按协议 `method_pilot.method_classes` 从正式基线中选取方法，直接继承比较参数及反思模型，不另外复制参数或加载探索性候选的训练文件。初始 NoCompaction 链路试跑通过后再执行此阶段；配置生成本身不会启动作业。

```bash
ctxpress eval configure configs/experiment.protocol.json --family swe-bench --phase method-pilot \
  --data /prepared/swe-verified --bindir /prepared/codex-bin \
  --model-catalog /prepared/codex-models.json \
  --resources /prepared/swe-bench-resources.json --output runs/proposals/verified-method-pilot.json
```

`--resources` 可省略，此时仍能审查配置，但资源缺项保留，计划不能启动。`--prices rates.json` 可提供与普通配置 `prices` 字段相同的对象（例如 `{"models": {...}}`），不会补猜缺少的反思模型费率。相对数据/CLI/资源/费率路径按当前工作目录解析；协议模板按协议目录解析，候选策略按协议的 `artifact_root` 解析；生成配置内的运行路径均为绝对路径。

`--model-catalog` 对应 `environment.model_catalog`，显式绑定 Codex 的本地 `{"models": [...]}` 目录。规划检查精确模型条目和推理档位，将原文件及条目哈希写入计划；执行只使用冻结副本，通过隔离、无真实凭据的 CLI 解析检查后，再只读挂载给 Agent。所有方法共用同一目录，恢复时不重新读取用户缓存。目录缺少指定模型、内容改变或 CLI 解析不一致时，在模型调用前失败。该选项保留旧配置兼容，但本项目新的正式比较必须提供；相同 CLI 版本和模型名不能代替元数据冻结。反思模型直接使用 API，不由这个 Codex 目录选择。

比较器另读实际宿主日志：明确的模型元数据回退会排除该次方法比较，基础提示词指纹不同会排除配对；原始官方成绩和用量保持原件。`api_cost_complete` 只表示记账完整；宿主已知不一致时 `api_cost_comparable=false`、费用差值为 null，两边实际费用仍保留。缺少历史日志只表示身份信息不完整，不能由此宣称宿主等价。

比较配置必须加载协议声明的 AutoCostModel 策略和 provenance 文件，核对策略哈希、训练会话、来源文件哈希及任务排除集合。来源记录中的 `synthetic_inputs=false` 和排除声明仍属本地来源证据，不证明原始语料真实性或任务质量；正式候选的来源仍需审查。没有冻结任务 ID 的家族会拒绝生成，不回退到全部题目或录制历史。输出不得覆盖协议、策略、训练文件、任务输入、CLI 或官方输入树，也不覆盖已有提案。

来源记录包含协议/模板/候选/训练文件哈希、所选任务哈希、作业数量与资源缺项，用于审查配置如何生成。后续 `plan` 独立冻结实际运行设置及输入；生成来源记录不会代替计划验证或支出许可。

候选 provenance 的 `policy_sha256` 指策略自身经过 canonical JSON 校验的内容哈希；可选 `policy_file_sha256` 声明原始文件字节哈希。生成器验证这些声明，并始终在新提案的 `sources` 中记录实际文件字节哈希。协议中的任务数量也须一致；Navidrome 明确使用一个完整 itinerary，其余比较集合以各 10 题为目标。

## 多模型用量与费用

每次主请求、原生压缩、摘要或反思调用记录请求 `model`、供应商返回的 `response_model` 以及 API token 用量。报告保留总用量，同时按请求模型列出 `usage_by_model`；返回快照名保留在各模型的 `response_models` 中。旧主请求可由运行/计划主模型归属，报告明确计入 `legacy_model_assignments`；旧反思日志没有模型身份时保持未归属，不套用主模型价格。

代理原样转发、未经方法改写的模型调用（宿主的旁路请求，如 Codex 生成任务标题、Claude Code 的无工具辅助请求）同样计入：日志记为 `type: "passthrough"`，各模型分项另有 `passthrough_calls`。账单的 `api_cost_by_role` 把同一声明费率估算按调用方拆成 `main`（Agent 的主请求）、`summary`（方法摘要/反思）、`native_compaction`（宿主原生压缩）、`passthrough`（旁路调用）；某一类缺费率或缺用量时只有该类为未知，日志缺行或计数冲突时各类均为未知。旁路调用的模型没有声明费率时，总费用保持未知而不是略去。记录旁路之前的日志没有这类行，其费用不变。

返回身份覆盖独立于费用完整性：`observed_response_model_requests` 统计有效返回身份，`missing_response_model_requests` 统计缺失身份（含用量汇总声明存在但缺少逐请求记录的调用），`response_model_observation_complete` 只在已有调用记录、身份无缺失且汇总无冲突时为 true。旧聚合对象没有这些字段时保持未知；本地 `native_compaction_blocked` 不属于上游调用。完整的请求模型和 token 用量仍可用于声明费率估算，即使没有返回身份；返回快照也不自动改用另一模型的价格。记录齐全不证明返回快照与所请求模型在语义或版本上等价。JSON 的每个模型及总计、HTML 的返回身份列均保留这项区别。

GPT-6 的声明费率示例在 [prices.gpt6.standard.json](../configs/prices.gpt6.standard.json)，依据 2026-10-04 核对的 [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol) 和 [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) 官方模型页。它用于按公开 API Standard token 费率统一估算，不表示 ChatGPT 订阅的实际账单，不包含工具费、区域溢价、Fast/Batch/Flex 档位及宿主计算费用。用 `configure --prices configs/prices.gpt6.standard.json` 显式绑定；生成来源记录和计划保留该文件的哈希。

`cache_write` 为每百万写入 token 的美元费率；[官方缓存说明](https://developers.openai.com/api/docs/guides/prompt-caching) 将普通输入、缓存读取、缓存写入分开计费，普通输入为 `input_tokens - cached_tokens - cache_write_tokens`。声明写入费率后，写入用量缺失的旧日志保持费用未知，不能默认零；已观测到写入但没声明写入费率时也不能给出完整费用。旧六字段静态费率仍按其有限范围处理。

可选 `long_context` 声明 `input_tokens_threshold` 与 `input/cached/cache_write/output` 四项费率，继承父级的币种、来源和观察日期。仅在单次请求输入严格超过阈值时将该请求全部 token 按这一档计价；恰好等于阈值仍用基础费率，多次短请求累计超过阈值也不升档。示例使用官方 272000 阈值；计价不会从模型名猜测阈值或服务档位。

配置可省略 `prices`，费用显示未知；也可用 `prices.models` 对每个实际调用模型显式声明费率。下例只是本地假费率的格式，不是当前供应商价格：

```json
{
  "prices": {
    "models": {
      "main-model": {
        "input": 1,
        "cached": 0.1,
        "cache_write": 1.25,
        "output": 2,
        "unit": "USD_per_million_tokens",
        "source": "example fixture rates, not a provider quote",
        "as_of": "2000-01-01"
      },
      "reflection-model": {
        "input": 0.2,
        "cached": 0.02,
        "cache_write": 0.25,
        "output": 0.4,
        "unit": "USD_per_million_tokens",
        "source": "example fixture rates, not a provider quote",
        "as_of": "2000-01-01"
      }
    }
  }
}
```

模型键使用请求日志中的实际 ID。旧的平铺 `prices` 格式仍接受，但仅适用于计划主模型；不同的反思模型不会继承它。费率必须非负、有限且有来源/日期。费用为普通输入、缓存读取、缓存写入和输出分别乘对应费率之和；普通输入从 API 总输入中扣除缓存读取与写入，避免重复计费。单位为每百万 token 美元。

JSON 和 HTML 报告按模型保留费用观察范围。缺少任一调用的模型、用量或费率时总费用为 null/未知；已知模型的局部费用仍可显示。失败调用缺少 API 用量时不能视为零费用，未完成的计划也不能用部分费用代替完整比较费用。无 token 记录时不从字符数估算美元。

该字段叫 `api_cost_at_declared_rates_usd`，只计算声明费率覆盖的 token 成本。缓存写入按独立费率计价，逐请求按声明阈值选择长上下文价格；缺少必要用量或费率时保持未知。服务档位和区域须与所选价格来源一致，宿主费用不包含在内，不能把这个估算称为完整供应商账单。

## 从头任务的配对比较

```bash
ctxpress eval compare --directory runs/verified-method-pilot \
  --reference native-230k --output runs/verified-method-pilot-comparison.json
```

`--reference` 必须是冻结计划里的明确方法标签。分析按同一任务 ID、重复编号和方法配对，只读取现有作业与产物，不调用模型或重新评分。输出必须是新文件，不能覆盖或写入运行的冻结输入、代码和作业证据目录。该命令记录执行代码与分析代码的身份，并核对官方产物哈希、实际请求日志和冻结设置。

每个作业的 `mechanism_observation.input_exposure` 给出成功主请求的输入长度：实际 API token、代理改写前估计、代理改写后估计分别统计最小值、均值、中位数、P90 和最大值，并记录有效/缺失请求数。P90 使用最近秩，摘要、原生压缩和失败请求不混入主请求分布；没有 API 用量时不以代理估计补值。长度暴露和实际管理操作分别报告，DTOC 的格式封装、长期保留的指针以及一次新的压缩动作不能混为触发次数。

配对证据读取器覆盖固定八类：SWE-bench 的 `resolved`、SWE-Milestone 的作者指标、Terminal/Science/DeepSWE/Pro V2 的原始 `rewards.*`、PolyBench 的 `repository.resolved`，以及 BigCodeBench 的 `code.sample_passed`。Pro V1 的原生报告仍可读取，但配对证据读取器当前明确拒绝该版本。BigCodeBench 另保留作者完整 cohort 与 pass@k 估计证据，不从单个样本制造 pass@k。

各读取器核对实际请求、冻结任务与官方评分产物，并保留原始指标名与分母。里程碑分数不转换成仓库解决率，reward 与函数样本的 pass@k 也不混用。缺失、失败、运行中、重复或证据变化的条目列出原因，不当作零分；有效的官方失败成绩仍保留。质量证据完整性与用量/费用完整性分别判断，评分不可用时仍能报告已核实的调用成本。

BigCodeBench 的 `author_code_cohorts` 按方法单列已有 `report.json` 中的作者 cohort 与 pass@k 来源。没有该报告时明确标为不可用；报告过期、样本不齐或来源校验失败时记录原因，不重跑评分器，也不改变独立有效的样本成绩与调用费用。

重复编号仅用于对应运行，并不表示模型共享随机种子。小规模试跑的差值是描述性结果，不提供统计显著性、非劣性或上下文方法有效性的结论。

比较结果同时保留上下文改写和反思/摘要失败的观察；官方任务评分有效不等于方法的全部机制已通过验证。运行时工具无法启动会排除质量指标，已发生的用量仍保留。摘要请求失败只记录白名单错误代码、参数及固定类别，不保存供应商错误文本；无用量的失败请求不能计作免费。

`ctxpress eval` 管理真实 Codex 续跑任务，与 `ctxpress run` 的重放模拟器分开。JSON 配置列出模型、Codex 二进制目录、冻结边界、方法、重复次数、并发数、调用上限和超时。现有后端使用本地 SWE-Milestone / C03 数据与官方评测脚本；Docker 执行需要相应的 Linux 环境。

## 过程评测（统一运行分析）

Benchmark 自带的评分回答"任务做没做成"；过程评测回答"上下文过程怎么跑的"：效率、token、费用、上下文长度、缓存，以及方法实际做了什么。它分三层，任何运行都能用同一条命令分析。

**1. 运行时记录。** 代理为每个主请求写入的行里，除原有的用量、延迟、改写前后大小和方法操作外，现在还有：

| 字段 | 含义 |
|---|---|
| `prefix` | 本次发出的条目与上一次发出的条目从头相同的条数、估计 token 数、是否改动了上一次已发出的内容（逐条摘要比对） |
| `host_prefix` | 宿主发来的原始历史与上一次相比是否改动了已有内容（如宿主自带压缩、历史修订） |
| `fixed_changed` | 指令、工具列表等非历史部分是否变化（只传历史的适配器记为空） |
| `composition` | 本次发出内容按类别的估计 token：指令、用户、助手、工具调用、工具输出、推理、其他 |
| `rewrite_seconds` | 方法改写本次请求的耗时 |

代理原样转发、未经方法改写的模型调用（宿主的旁路请求，如 Codex 生成任务标题、Claude Code 的无工具辅助请求）另记一行 `type: "passthrough"`：协议、请求模型、返回模型、状态、耗时和用量。它们计入正式计费，并按调用方单独列出（见"多模型用量与费用"）。

前三项使缓存断裂的原因可以直接测量，而不必从用量推断。旧日志没有这些字段时自动退回推断口径，并在结果里标明测量覆盖率。

**2. 统一分析。**

```bash
ctxpress analyze <路径> --output <新目录> [--eligibility <排除名单>] [--prices <价格文件>] [--reference <参照方法>]
```

| 路径 | 读取方式 | 任务质量 |
|---|---|---|
| 请求日志，或 `ctxpress codex`/`claude` 的运行目录 | 直接读日志；价格用 `--prices`（协议、计划文件或价格对象） | 无 |
| 评测目录（含 `jobs.sqlite`） | 只读打开数据库，每个已完成作业的结果里已含全部请求行；价格取自计划 | 按 `harness.results.outcomes` 解释官方评分 |
| 严格报告 `summary.json` | 报告的作业列表、可比性与产物哈希；逐作业核对哈希后重算，费用须与报告一致 | 取自报告 |

同一批作业从评测目录和严格报告两条路径分析，逐作业的质量、费用、缓存与上下文指标完全一致。

| 类别 | 指标（每作业） |
|---|---|
| 质量 | 官方指标（Verified `resolved`、代码样本通过、Milestone `score_1000`、Harbor reward） |
| 效率 | Agent 秒数、成功/失败主请求、工具调用、模型延迟（主请求 + 辅助 + 旁路）、方法改写耗时、会话数与主会话以外的请求数 |
| Token | 输入 = 未缓存 + 缓存读 + 缓存写；输出、推理；主请求、辅助调用（方法摘要/反思、宿主原生压缩）、旁路调用分开 |
| 费用 | 上述四部分逐请求按声明价格计价（按单次输入选长上下文档位）；总费用 = 主 + 辅助 + 旁路，与正式计费一致并按这三部分拆开；辅助占比 |
| 上下文 | 只看主会话（请求最多的会话，即 Agent 自己的对话）：每个请求的 API 输入首个、最大、均值、中位数、P90、末个、每请求增长；改写前/后历史估计与缩减比例；发出内容的平均构成 |
| 缓存 | 缓存读占比；缓存断裂 = 缓存读比可缓存前缀 min(上一请求输入, 本请求输入) 少至少 1024 token 且不足一半；原因分为方法改写、宿主改写、服务端未命中；各自的未命中 token 与按（输入价 − 缓存价）计的损失 |
| 方法活动 | 发出历史被改动的请求数、首次改动位置、宿主改动次数、非历史部分变化次数、各类操作次数、辅助调用按用途计数 |

方法汇总只用可用作业，给出均值、中位数与合计；有任务质量时按 (任务, 重复) 与参照方法配对，报告合并比（方法总量 / 参照总量）与逐对比值的几何均值。输出 `report.md`、`analysis.json`、`jobs.csv`、`requests.csv`、`methods.csv`，以及每个家族的费用构成、缓存损失（按原因）和上下文长度图（PNG/SVG，`--no-figures` 跳过）。

**3. 自检。** 每个作业都核对自己的记录，硬问题使该作业退出汇总并列在报告里，警告只提示：

| 检查 | 级别 |
|---|---|
| 日志中无法解析的行、同一会话的请求编号缺口 | 硬问题 |
| 重算费用与 `eval_usage` 计费不一致（两套独立实现）；报告输入时与报告费用不一致、产物哈希变化 | 硬问题 |
| 成功请求缺完整用量 | 警告 |
| 真实 API token 与代理估计之比（中位数）超出 0.5–2 | 警告：方法的预算按估计值执行，比值漂移说明预算与实际不符 |
| 过程遥测覆盖率 | 记录 |

只读：不调用模型、不评分、不重试，只写 `--output`。这些是描述性统计：每题单次运行不支持因果或非劣结论；服务端不报告缓存未命中原因，"服务端"一类是排除方法与宿主改动后的剩余；历史大小与构成是字符数 / 4 的估计。

## Benchmark 与任务选择

Benchmark 适配器与上下文方法、Agent 执行后端分别声明。固定八类从头任务通过 `ctxpress eval benchmarks --start-mode task_start` 查询支持范围；每类都需要实际数据和准备资源。本节以下的录制边界示例属于 SWE-Milestone 的 `local-recorded-boundaries` 模式，用于已有 Navidrome 历史续跑，与从头任务的计划和成绩分开。适配器负责本地任务发现、任务 ID 选择、所需输入文件、执行和失败资源恢复。

```bash
ctxpress eval benchmarks
ctxpress eval tasks --benchmark swe-milestone --scripts /path/to/scripts
ctxpress eval tasks --benchmark swe-milestone --scripts /path/to/scripts --min-context 128000 --gradable
```

以上命令只查询元数据，不导入本地执行脚本、不启动容器、模型或评分器、不下载资源。`--gradable` 表示存在官方评分映射，不表示本机评分资源已准备好。任务列表来自所选目录，缺失目录或目录歧义会报错，不生成替代任务。

配置保留原有字段，在 `backend: "codex_docker"` 外指定 `benchmark: "swe-milestone"`。可以使用 `tasks: ["n0-j435"]` 代替 `boundaries`；两种选择不能同时出现。任务 ID 展开为该目录实际记录的坐标和上下文 token 数，然后进入同一冻结计划和任务队列。未知、重复或缺少有效 token 记录的任务 ID 会被拒绝。旧配置省略 `benchmark` 时默认选择当前 SWE-Milestone 适配器。

新计划把适配器名称、版本、任务集合和运行模式纳入计划哈希；新执行结果记录 benchmark 与任务 ID，报告同时显示任务集合和模式。旧计划没有声明 benchmark 时报告保留未知身份，不把新元数据追填为旧记录。`ctxpress/benchmarks/` 提供当前续跑接口和内置注册表；新增完整任务集还需要相应的数据加载、从头启动和官方评分实现，仅增加注册名称不代表接入完成。

共用任务实例现在声明 `id`、`benchmark`、`start_mode`、`initial_state`、按用途标注的文件输入及评分类型。checkpoint 的录制坐标属于其适配器的初始状态；共用 worker 通过适配器执行任务，不再直接解释目录和坐标。任务输入的 SHA-256 必须与冻结计划的 artifacts 一致，双写的旧 boundary 与任务状态不一致时拒绝执行。旧冻结计划保留原哈希和兼容恢复流程。

```bash
# 官方本地数据发现，不启动模型、容器或评分器
ctxpress eval tasks --benchmark swe-milestone --start-mode task_start --data /path/to/SWE-Milestone-data
```

从头任务以完整仓库 itinerary 为单位发现，保留有效、评分及非评分里程碑和有效依赖，不把每个里程碑当作可独立启动的 issue。读取器核对 SRS、评分配置和分类文件，拒绝缺项、重复选择或循环依赖；官方 DAG 的注释行被保留为非执行数据。数据发现、资源绑定、官方原生准备与连续 Trial 执行已接通；执行/评分代码能力为 true；真实 Navidrome 从头试跑已核验，具体方法覆盖见执行安排。

官方 Codex hook 已提供进程内替换 Agent 启动的代码：挂载声明的 ctxpress 与 Codex，分别包裹新会话和恢复命令，保留官方 Agent 的参数及任务流程。初始化不安装或下载 CLI，alpha 版本按完整字符串核对，压缩阈值显式传入。该 hook 已接入 native worker 的 trial 生命周期和所属资源组件；真实 CPU 任务链路的证据另见执行安排；离线命令检查本身不构成完整从头执行证据。

SWE-Milestone 的原生准备使用冻结的 `code` 官方源码树和 `dependencies`（已有 yaml、pathspec 及传递依赖），并绑定 Linux Python ≥3.10 的路径、版本与二进制哈希。计划逐项列出源码/接口、数据 release、元数据和每个有效 DAG 节点的准备评分镜像缺项。各工作区的 `dataset_manifest.json` 明确 `dataset: "swe-milestone"` 和与资源 `release` 一致的 `revision`；预检查还核对官方 `manifests/BENCHMARK_VERSION`。准备检查可只使用声明和文件哈希；连续执行另要求资源 specification 的 `native_data_version: true`。捕获只读原数据 checkout，要求 HEAD 与 release 标签指向同一提交，保存真实 commit/tag 对象及工作区 dirty 状态。worker 在私有输入目录恢复最小 Git 对象库，固定 release 并移除继承的版本校验关闭项，再调用作者版本 gate。该证明不声称冻结文件等于干净提交树，文件内容仍由各自哈希绑定；不复制历史 blobs、远端配置或 Git hooks。

发现器保留所选 milestone 的全部 `dockerfiles/` 评分文件、共享 `scripts/`、`tests/`、`grading_assets/` 与其中空目录，原历史 trial、克隆仓库和缓存不进入输入。仓库配置沿用作者优先级：相邻数据 `config/<repo-id>.yaml` 优先于官方代码的 config。单工作区选择时用独立 `dataset-config` 树冻结相邻配置，数据根目录和任务集合不扩大。任务 remap 只允许文件路径变化，仓库状态、目录清单、评分集合、输入角色/哈希及顺序必须与冻结原任务一致。

原生准备目录恢复为 `native-inputs/<repo-id>`，避免把快照目录 `task-data` 错当仓库 ID。实际作者的 resolve/freeze API 固定仓库配置和运行政策，metadata 解析器读取 source/test/exclusion 分区，配置与分类过滤器先校验，实际 DAGManager 保留依赖类型及配置语义。即使原始选择涵盖全部 CSV，也为原生 DAG 写明确的 selected IDs，保留没有边的独立节点。非评分节点仍参与依赖和作者提交流程；它们的输入/评分镜像需按 native watcher 的需要准备，不能从质量分母中排除后就丢掉其执行资源。

`milestone.driver.preflight` 的独立 worker 用 `-I -S -B` 启动，只导入已冻结的框架与依赖，先校验输入树和 Python 身份，再写 `preparation.json`，保留作者配置哈希、政策模式及逐文件角色/哈希。准备过程不读取认证，不创建容器或开启模型通信，不执行 Agent 安装器。可用 `tests/check_milestone_author.py` 对已存在的本地源码、yaml/pathspec 包做显式离线检查；脚本使用合成任务与镜像 ID、删除原临时来源后调用冻结副本中的实际作者 API，保存 `test_only: true`，不是官方任务成绩。原生容器归属、总预算下的新会话/恢复、作者提交捕获、异步 PatchEvaluator 与退出组件已在 worker 内串接；准备能力及组件检查不会把完整执行标成已完成。

`SWEMilestone.read_itinerary_grade` 已提供只读结果入口：独立 worker 调用冻结的 `harness.e2e.collect_results`，在私有临时布局中只复制所选 itinerary 的 summary、逐次 evaluation JSON 与 Agent 统计。按作者规则选择 summary 指定或最新有效 attempt，优先 filtered 结果；较新的 summary-only 重试不被旧的通过文件覆盖。作者汇总指标原样保留，non-graded 节点不进入质量分母，基础设施无效结果与零测试的编译失败分别处理。另列 `scoring_complete`、逐节点原始结果和文件路径/哈希；缺失 summary 不编造 verdict，缺失或基础设施无效的评分证据使整体 `resolved` 保持 null。该入口只读已有产物，不运行 PatchEvaluator，不把 `official_grading_supported` 或 `real_run_verified` 改为 true。

连续执行的资源组件已提供：`milestone.resources` 为单个持久 Agent 与并行 verifier 分别保存 daemon、容器 ID、镜像、运行标签和进程身份；verifier 共用本次运行的内部网络。启动前写归属日志，清理检查进程、私有凭据、容器删除与网络脱离；恢复拒绝仍存活的 worker、变化的 daemon/镜像/标签及有残余容器的网络，不复用旧名称。认证通过内存 tar 上传到容器私有 home，宿主输出目录不复制认证文件。

`milestone.containers` 在独立 worker 内临时接管作者 ContainerSetup/Orchestrator/PatchEvaluator，调用实际启动函数并把 Docker 创建和清理转给归属组件，保留原用户/cache/Git 初始化、配置绑定、提交处理与评分函数。sudo 安装块按明确 AST 形状替换为已有工具检查；Python/sudo/git/bash/tar 必须在镜像中，不执行安装。Agent 使用无 IP 网络与模型 socket 的比较协议；普通 verifier 使用本次运行的内部网络，testcontainers verifier 则沿用作者 host 网络并使用所属 API 网关；这一网络策略属于 `ctxpress_comparison`，不冒充作者原网络协议。Agent 启动要求私有 Codex framework；testcontainers 的服务归属已由 `service_resources`/`service_gateway` 接入。

`milestone.codex_hook` 的私有模式已包装作者的新会话与恢复命令，固定原 compact limit、CLI 版本和进程组记录。它只挂载冻结 ctxpress 包、CLI、所属通信目录和输出，不继承宿主认证/config 或 API 环境；sessions 挂载为实际目录，保留作者的 find/stat 恢复读取。私有 home 初始化拒绝预置 auth/config，凭据由归属组件单独上传；命令必须绑定同一 loopback relay，不能在恢复时切换。所属通道与 relay 初始化现由 `milestone.transport` 提供，通过原生容器启动完成后的回调接入；完整 worker 已实现，使用合成 IO 的 native trial 组合已核对，生产资源组件组合已核对，具体任务服务的真实执行尚未验证，计划执行能力保持未完成。


`milestone.transport` 在绑定 Unix socket 前写所属目录日志，仅允许 HTTPS upstream 对应的 CONNECT 目标，支持显式宿主代理。容器内 relay 通过独立进程组运行，核对 ready 的进程身份后绑定同一 loopback 地址，再单独上传认证。回收检查原 daemon 和 Agent 删除/认证清理证据，Agent 仍存在时保留通信目录，避免其 bind source 消失后无法重启清理。无认证或目标信息进入通道恢复日志。

`milestone.agent` 包装实际作者 E2EAgentRunner 与 E2ETrialRunner，恢复时只刷新显式认证文件，不读取宿主默认 home/config。每次新会话/恢复退出后检查所属进程组停止，原作者超时处理仍保留。`milestone.budget` 共用一个起始时间与全 sessions 的工具调用计数，单次 native timeout 限制为剩余总时间；达到调用上限后等已记录的工具输出齐全才中断。Linux 主线程 watchdog 包住原生 recovery loop，覆盖重试等待，退出后撤掉计时器，不把最终评分/容器清理置于 Agent deadline 内。预算审计记录调用数、输出完整性与 stop reason。

本轮 156 项 Linux 回归检查通过。实际本地 loopback/Unix 转发、禁止未声明目标与 relay 进程组停止已核对；冻结作者 Agent/Trial 构造、启动函数和预算 gate 也已离线检查，但 Docker、用户/runtime gate 与模型调用仍使用替身或提前失败分支。没有真实容器或模型实验，不能据此证明完整连续评分正确。

评分镜像 hook 调用作者的原缓存 overlay 函数计算与检查政策、父镜像、closure 和 Go 工具链替换，只替换其 Docker 边界。有效评分镜像必须已准备；需要 overlay 时，资源清单的 grading image reference 应指向已存在的作者派生 alias，其 ID/标签必须匹配冻结 Agent closure 与政策。别名缺失、标签漂移或未声明镜像直接失败，不 build/tag/pull。离线检查使用冻结 v1.0.2 源码与合成镜像 metadata；启动后检查函数在启动投影用例中使用替身，只验证保留调用顺序和失败清理，不能证明真实 cache/toolchain 环境有效。完整 worker/trial 派发和 Git 数据版本证据已实现；testcontainers 服务 API 和归属管理已串接，整条执行链路已通过作者完整 Trial 与生产资源组件的合成 IO 组合检查，task-start execution/official_grading 为 true，real_run_verified 为 false。

原生 testcontainers 任务在资源 specification 的任务记录中声明 `service_images: {"database": "PREPARED_LOCAL_IMAGE"}`，捕获其引用和不可变 ID。计划按作者 list-format `requires_docker_socket` 标记列出缺项，保留 legacy dict-format 与缺失配置的原语义，同时拒绝漂移哈希和字符串布尔值。每次评分拥有独立 Unix API 目录和 services journal；作者的宿主 socket 挂载改为此只读目录，`DOCKER_HOST` 指向所属 socket。verifier 保留作者 host 网络与独立 webServer 端口，辅助服务使用所属内部 bridge，只在宿主 loopback 发布端口；Ryuk 关闭，资源由框架清理。这属于明确记录的比较协议，尚未证明所有原测试网络语义等价。

服务 API 支持准备镜像的 inspect/已有镜像响应、容器/内部网络/本地卷的创建与生命周期、日志、事件、exec upgrade 及 archive 复制。镜像指令中的匿名卷先替换为写入 journal 的所属卷，创建回复中断后也能按名称/标签/镜像找回。所有操作只访问本次 scope，长日志和 exec 不占用控制锁；服务 API 线程退出后先确认 verifier 已移除，再清理容器、网络、卷和目录。修改的 daemon、归属、ID 或镜像会阻止删除。当前服务 provider 要求本地 Unix Docker endpoint、Linux CPU 镜像及所属 volumes；宿主 bind、特权/foreign namespace、镜像构建和运行时下载不开放，遇到这些请求返回具体缺项，不能据此把任意 testcontainers 任务标为可用。

本轮 152 项本地检查通过，包含实际 Unix HTTP、分块 archive、实时日志并发与 exec upgrade。已有 Docker CLI 使用隔离空配置，仅连接假 API，版本/创建流程通过；作者冻结 v1.0.2 的真实 testcontainers 启动函数也已检查挂载投影和退出顺序，Docker 与启动后 gate 为替身。证据保存在 `runs/milestone-native-services-author-check.json`；没有真实容器或模型调用；随后已补 native trial 的合成 IO 组合检查。接口参考 [Docker Engine API](https://docs.docker.com/reference/api/engine/) 与 [Testcontainers runtime configuration](https://node.testcontainers.org/supported-container-runtimes/)。

新增的 `check_milestone_lifecycle.py` 在冻结作者 v1.0.2 源码上调用完整 `milestone.runner.run()`：使用实际 Trial、新会话/恢复命令、watcher、DAG、Git 提交捕获、异步结果写入和 collector。本地 Git 生成的源码快照经过作者完整性检查；模拟 IO 的 M1 → M2 → M3 流程保留非评分节点分母，M3 测试失败不会被 DAG 的 early-unblock 完成状态改成评分通过。预算超时用例也完成线程退出/清理，缺失评分保持不完整。新增组合检查使用生产 Registry/Owner、模型 Channel 与服务 Gateway；Docker IO、容器 runtime gate、模型输出及测试执行仍为替身。M2 通过真实 Unix HTTP 创建模拟 Synapse、复制配置 archive 和启动服务，退出后核对所有资源 journal 已清理；超时用例也核对资源退出。结果保存到 `runs/milestone-native-resources-author-check.json`，没有真实容器或模型调用。另用现有 Docker CLI 对隔离假 Engine 检查 verifier/服务顺序、worker 恢复和 verifier 删除失败时保留服务与挂载目录；本轮相关 93 项回归全部通过。这些证据不证明真实任务环境已跑通。

实际任务 API 形状另参照 Element Web v1.11.97 的 [Synapse wrapper](https://github.com/element-hq/element-web/blob/v1.11.97/playwright/testcontainers/synapse.ts) 与它声明的公共 fixture：Synapse/Dendrite 使用配置 archive 和 HTTP readiness，Mailpit 使用内部 alias，MAS 使用 archive/exec。服务网络 endpoint 现在核对 alias 与 link 归属，拒绝外部容器 ID、静态地址和管理配置，同时接受 Docker CLI 发送的空默认字段。这里仅核对作者源码中的 API 用法，没有下载 npm 包或镜像，也没有运行真实 Node testcontainers SDK；具体任务需先准备其声明的服务镜像。

组合检查修复了两处启动兼容问题：作者镜像版本 gate 读取冻结的原始 image reference，容器仍按其不可变内容 ID 启动，不能把 Docker config ID 冒充 registry manifest digest；已有 Codex prerelease 二进制不进入作者 npm 安装版本 selector，实际 CLI 仍按完整版本字符串核对。本轮相关 52 项回归通过。

评分退出组件 `milestone.trial` 在 Agent hook 之后安装，保留作者的累计预算包装。trial 注册后，资源 registry 保护其 Agent 和整体资源；cleanup 先停止 Agent/删除认证，设置 watcher 停止标记，再按显式期限 join watcher，从而等待作者线程池中的评分工作退出。超时或作者 cleanup 失败时保留保护，拒绝提前删除 Agent/整体容器。成功后保留作者的统计/工作区复制及锁释放，禁用作者未经归属核对的容器删除，再放行 registry 清理；调用方的 SIGTERM 行为和 remove_container 设置均恢复。退出证据保存到 trial 的 `ctxpress-drain.json`。该组件已在完整 worker 中接到 Agent hook 之后；本地实际线程池测试和冻结作者源码的已停止 watcher/cleanup 检查不构成真实 trial 验证。已确认 worker 死亡后的恢复不重建进程内保护，仍先核对所属 daemon/容器及凭据清理。

Terminal-Bench 与 Terminal-Bench-Science 的 Harbor CPU/NVIDIA GPU 任务另有完整执行入口：冻结的独立子进程调用官方 `Trial`、Codex hook 和 verifier，分别保留各阶段的超时。需要预先声明完整 `harbor` 源码树、`dependencies` 依赖树（含真实 Harbor dist-info）、固定 Linux/Python ≥3.12 与每个服务的本地镜像 ID；启动前以 `-I -S -B` 核对官方导入，缺失依赖直接失败，不安装或读取宿主 site-packages。`CTXPRESS_CODEX_AUTH_FILE` 只在运行时传入子进程环境，凭据只上传到容器私有目录，计划和官方 Trial 配置不保存凭据内容。

任务目录从冻结的 `task.toml` 输入位置派生，不再读取原数据目录；方法的 profile/policy 仅复制明确选择的文件并以只读目录挂载，不把整个数据根目录或评分测试挂给 Agent。工具预算达到已完成的调用边界后停止 Agent 进程组，超时/取消也先停止进程并核对凭据删除。官方 verifier 可在 Agent 超时/非零退出后产生有效 reward，这些结果保留 Agent 异常并分别汇总数值指标，不自动记为解决率。每个 attempt 启动前写资源日志；失败重试先核对 Docker daemon、项目/运行标签与镜像身份后清理，只处理属于本次运行的容器、网络与卷，保留镜像。

GPU 任务在资源 specification 对应 task 中提供 `gpu_device_ids`（完整 NVIDIA GPU UUID，数量等于 `environment.gpus`）。只为主服务建立设备 reservation；启动后、凭据上传前检查实际数量、UUID 和 `gpu_types`，记录型号/显存/驱动。进程间锁按同宿主/用户、Docker daemon 和 UUID 排队，同卡任务不会由 ctxpress 并发派发，不同卡可并行；同一计划的冲突任务保持 pending，不占用全部 CPU worker 槽位。GPU 等待不计入 Agent 超时。尚未清理的所属容器继续保留设备 claim。资源日志在上传凭据前写入可能存在标记，只有确认删除后才清除，GPU 初始化失败而未上传时无需重启容器即可恢复清理。

GPU 观察结果及声明设备进入 JSON/HTML 报告；未知硬件保留为未记录，驱动版本只报告观察值，没有被冻结。完整 NVIDIA GPU 的执行代码已提供，MIG、辅助服务的独立 GPU 分配以及缺失阶段 GPU UUID/数据版本会列出具体缺项或在启动前拒绝。本机 Linux 已准备首题的 Harbor 运行依赖；GPU/Trial、本地 socket/进程和跨进程锁的合成检查不构成真实 GPU/CUDA 运行证据，`real_run_verified` 仍为 false。TB 4.0、Science、DeepSWE 原发布协议的预算、资源、Agent、重复数仍需逐项绑定，普通 Harbor verifier 不能代替 DeepSWE 的独立干净容器评分。

Terminal-Bench/Science 的新版 Harbor 路径由冻结树中的 `src/harbor/trial/single_step.py` 识别，独立子进程先检查实际官方 API 与源码/分发包身份。调用 `await Trial.create(config)`，由作者工厂选择 SingleStepTrial；不直接实例化新版抽象 Trial，不依赖其创建子类的旧行为。保留作者 collect hook、服务产物映射、ArtifactHandler 的上传/下载、测试上传与 verifier 原始结果。支持单步骤 Linux CPU/NVIDIA、Agent 多服务与共享评分，独立评分暂限单服务。多步骤、评分侧 Compose、TPU、Windows、MIG、网络 allowlist 或运行中网络切换有具体缺项/启动前错误。

独立评分资源使用 `grading_images: {"verifier": "PREPARED_VERIFIER_IMAGE"}`。评分环境可由作者 `verifier.environment`、`tests/Dockerfile` 定义，也可按官方定义回退到初始 Agent 环境；回退模式由官方 verifier 上传测试，不能假设每个任务都已把 tests 烘焙进镜像。包含测试的镜像会在评分启动后逐项核对 `/tests/` 与冻结哈希。Agent 不挂载评分日志或测试；评分容器不挂 ctxpress/Codex、方法目录、模型 socket 或凭据。作者主服务收集后如需先停 main 再取辅助服务证据，先完成进程/凭据清理和日志权限修复，再调用官方 stop_service；整个 Agent 项目停止确认后才允许独立评分。

评分阶段 GPU 在资源 specification 中单独提供 `verifier_gpu_device_ids`，数量匹配解析后的 `verifier.environment.gpus`；显式空 environment 使用官方默认资源，省略 environment 则继承 Agent 环境声明。调度器及 daemon/UUID 进程锁保留两阶段 UUID 的并集，同一 UUID 两阶段复用只计一份 claim。JSON/HTML 分别保存两阶段绑定与实际观察；没有观察值时标未记录。原始 artifacts/verifier 文件保存路径与哈希。

两种 Harbor API 都采用 ctxpress 的私有服务网络与仅模型 socket 通信。新版不调用官方会构建或下载探测镜像的 egress sidecar；这项网络约束明确记录为 `ctxpress_private_services_model_socket_only`，因此这些运行属于 `ctxpress_comparison`，不是对发布评测网络政策的完整复刻。实际官方依赖与任务/镜像仍需事先准备，`real_run_verified` 仍为 false。

DeepSWE v1.1 另有 `deep-swe` 的执行适配，当前范围为单服务 Linux CPU 任务。数据根目录需要 `dataset_manifest.json`，明确 `dataset: "deep-swe"` 和 `revision`；任务保留作者的 `environment_mode: "separate"`、`verifier.environment`、`verifier.collect` 与 `artifacts = ["/logs/artifacts/model.patch"]`，并提供 `tests/Dockerfile`。发现任务时保留目录 ID 与 Pier 的 `datacurve/<id>` 报告身份，排除 solution 文件。多步骤、Compose 辅助服务或 GPU verifier 未提供执行支持，不能由其他任务的结果补推覆盖。

资源 specification 中每个所选任务使用不同的 `agent_image` 和 `grading_images: {"verifier": "PREPARED_VERIFIER_IMAGE"}`。评分镜像必须事先由对应作者 `tests/` 构建好，运行期间不 build/pull；启动评分容器后逐项核对 `/tests/` 文件与冻结输入的字节哈希（Dockerfile 除外），不符则失败。官方树改用 `pier`（完整仓库布局）和 `dependencies`（完整传递依赖、真实 `datacurve_pier-*.dist-info`），仍绑定 Linux/Python ≥3.12；worker 核对源码与已捕获分发包版本一致且 >0.3.0。缺少依赖在模型通信和容器启动前失败，不自动安装。

执行调用官方 Pier `Trial.create` 和 `run`，保留作者 collect/评分的命令及失败语义。Codex hook 使用固定二进制和同一 ctxpress 方法入口，保留 Pier 的提示渲染与官方轨迹转换。Agent 没有 tests 或 verifier 日志的挂载；停止 Agent 进程并确认删除凭据后，作者 hook 导出已提交 patch，官方 Trial 停止 Agent 环境，再进入全新评分环境。未完成的 Agent 环境清理会阻止评分，不依赖官方 best-effort 清理警告。原始 artifacts 全部留在 attempt 下，官方 transfer 接收独立 staging 目录，仅有 `model.patch`；缺失/空 patch 按作者基准状态规则评分。两个环境各有镜像 ID、Compose 哈希和所属资源恢复日志；grader 没有模型 socket、二进制/方法目录或认证挂载。

结果保存作者的 reward、CTRF、test-stdout、run.log、原生 reports 与 patch 的路径和哈希。DeepSWE 官方 `reward` 是定义明确的二元成功指标，0/1 对应失败/成功，`-1` 为 verifier 崩溃的基础设施无效标记；其余 pass fractions/counts 原样保留，不重新实现评分。JSON/HTML 保留独立环境证据、`protocol: "ctxpress_comparison"` 和 `real_run_verified: false`。Terminal/Science 已取得 CPU 首题的真实官方 reward，DeepSWE 首题执行中；单题结果不覆盖全部任务与资源配置。

模板 [configs/deep_swe.example.json](../configs/deep_swe.example.json) 用于填写模型、任务、已准备资源和方法，显式默认 100 次工具调用、1800 秒；它不是作者榜单的预算设置，也没有授权启动实验。忠实复现作者 Pier + mini-swe-agent + Modal 榜单仍需后续宿主和原协议设置接入。旧 Harbor 路径遇到新版 separate/collect 任务会列为具体计划缺项，不能静默使用旧共享环境评分。新版 SingleStepTrial 路径见前文。

### SWE-bench 的从头执行与官方评分

Verified、Lite、完整集共享执行接口，保留各自 dataset 身份。执行时数据根目录必须包含 `dataset_manifest.json`，其中 `dataset` 匹配所选适配器、`revision` 与资源 specification 的 `release` 相同。原始实例除问题/仓库/完整 base_commit 外，还需要官方 `version`、`test_patch`、`FAIL_TO_PASS` 和 `PASS_TO_PASS`；参考 patch 不传给 Agent。目录发现允许缺少这些评分资源，执行计划会列出缺项并拒绝启动。

资源 specification 对应 task 提供 `agent_image` 和 `grading_images: {"verifier": "PREPARED_IMAGE"}`。两阶段使用全新独立容器，镜像必须已存在且为 Linux CPU 环境，`/testbed` 为无未提交或未跟踪文件的 `base_commit`。Agent 镜像还须已有固定 Codex 所需的 Python/Git/系统依赖；执行期间不安装。官方输入树使用完整仓库布局的 `swebench` 与 `dependencies`（完整传递依赖和真实 `swebench-*.dist-info/METADATA`），Linux Python ≥3.10 的路径、版本和二进制哈希也进入资源清单。worker 用 `-I -S -B` 启动，先核对实际源码/分发包与官方 `run_instance` 接口，再创建资源。不读取宿主 site-packages 或自动加载宿主 `.env`。

根据冻结源码区分以下两种 API；未知接口在启动前失败。

- 旧版 `harness/test_spec.py` 或 `harness/test_spec/test_spec.py`：额外冻结 `test_specs` 树，每个实例有 `<instance_id>.json`，包含已准备的官方 TestSpec 全部构造字段（如 repo/env/eval_script_list、arch 和两组测试 ID）。恢复实际作者 TestSpec，并用作者的本地 `make_eval_script_list` 核对评分脚本与冻结实例。不会调用可能访问网络的环境脚本构建函数。仅替换官方 build_container 的容器创建，使其使用已声明镜像，保留作者测试用户、平台与 nano_cpus。
- 新版 `types.py`/`image_builder/constants.py`：数据另须包含官方 `image`、`eval_script`、`log_parser`、`eval_type`，由实际作者 `make_test_spec` 构建评分对象。所选 task 的资源 specification 必须明确 `verifier_cap_add: ["SYS_ADMIN"]`，对应作者容器能力。普通旧版实例数据不能直接作为新版数据使用。多模态 image_assets 尚不支持，自动资源下载被拒绝。

Agent 只挂载 ctxpress 包、固定二进制、所选方法 profile、模型 socket 与自身日志，数据集、官方 TestSpec 和 grader 日志不挂载。工具网络关闭，模型通信仅通过 socket；结果记录 `network_policy: "no_tool_network_model_socket_only"`、`protocol: "ctxpress_comparison"`。这套 Codex 比较设置没有宣称还原任何发布评测的完整 Agent/网络协议。

每个 attempt 写入两个资源归属日志；容器创建响应丢失时按精确名称查回并核对身份。新会话结束、工具预算耗尽或超时后，先停止所属 Agent 进程并确认凭据删除，再从不可变基准导出包含提交、暂存、未暂存、新增和二进制文件的 Git diff。`predictions.jsonl` 保留官方三字段和空补丁，`model.patch` 单独保存。Agent 容器删除确认后才进入独立评分，verifier 不挂载 Agent 日志/凭据/模型 socket，官方 `run_instance` 负责补丁应用、测试执行与结果解析。评分 run_id 每次唯一，拒绝缓存报告；保留官方日志与产物路径/哈希，缺失或非布尔实例报告保持未评分。Agent 异常与官方任务结果分别保留。

失败恢复先核对 worker 已停止、Docker daemon、名称、运行标签、角色、镜像及容器 ID，再清理所属资源；可能存在凭据时先核对其删除，不移除镜像。模板 [configs/swe_bench.example.json](../configs/swe_bench.example.json) 明确 Agent `timeout` 和独立 `grading_timeout`（默认 1800 秒），支持统一后台队列和 JSON/HTML 报告。Verified 首题已执行真实模型并取得独立官方评分；具体任务未解决，属于有效失败结果。适配器静态 `real_run_verified` 不据单题改为 true。

```bash
# 先填写实际模型和当前 Codex 二进制目录；示例本身不代表批准实验
ctxpress eval plan configs/live_mechanism.example.json --output runs/mechanism-plan.json

# 设置经确认后再启动；命令返回后可以继续开发
ctxpress eval run runs/mechanism-plan.json --directory runs/mechanism --background
ctxpress eval status --directory runs/mechanism
ctxpress eval report --directory runs/mechanism
```

`plan` 只校验与展开配置，不启动容器或模型，不读取凭证内容。计划记录代码哈希、可用的基准脚本与二进制哈希、存在的 code-mode 伴随程序、所选边界的原始历史，以及成本模型统计或自动策略文件哈希（包括包装器内部）。大文件按块计算哈希，不把整个二进制读入内存。缺少的环境文件会列在计划中；准备好环境后需重新生成计划。

新计划启动时把框架代码固定到 `runtime/`，把计划中已声明的文件复制到 `inputs/`。`inputs/manifest.json` 绑定原始路径、相对副本路径、内容哈希及原计划哈希；副本完成并校验后才发布。计划与任务声明保留原路径，执行时由共用解析器转换到副本路径，方便核对批准的设置。

调度器、任务进程和容器内安装路径使用冻结代码；二进制、所选历史和策略读取输入副本。复制完成后可以继续修改或移除对应原文件，恢复与重试继续复用已验证的副本。副本内容或路径绑定被改动时拒绝启动任务，不能退回原文件。复制中源文件变化或复制失败时不发布半成品。`auth.json` 不进入输入快照，凭证处理仍只走容器运行时的专用流程。旧计划保留原有版本检查行为；新计划包含 `input_snapshot_version: 1`。

输入副本覆盖计划声明的文件。可以进一步声明续跑镜像、完整辅助工作区及官方评分输入清单，见下文。宿主 Python、系统库、Docker 实现和外部模型服务的权重版本尚未整体冻结；不能把这些校验当作完整的实验环境复现。不同框架版本另建计划与输出目录。

## 镜像与需求目录

先生成一个可审核的环境清单。这个命令只执行本地 `docker image inspect` 和文件哈希计算，不启动容器、模型或评分器，不拉取镜像。输出文件必须在需求目录之外。

```bash
ctxpress eval capture-environment \
  --workspace /absolute/path/to/e2e_workspace \
  --base-image swe-milestone/navidrome_navidrome_v0.57.0_v0.58.0__base-offline:v1.0.2 \
  --boundary 3:14 \
  --output runs/environment.json
```

`--boundary` 可重复，列出所有计划选择的边界。把输出路径填入评测配置的 `environment.snapshot`（相对配置文件解析），再生成计划。编译计划只读取清单与文件，不执行 Docker 或导入基准脚本。所选边界必须有清单条目；辅助工作区不能和脚本、二进制、清单路径重叠。

清单保存基础镜像及各 `hsnap-n:j` 的完整内容 ID、平台与仓库 digest，保存需求目录的所有常规文件、内容哈希、目录和可执行文件列表。符号链接与 `auth.json` 会在读取内容前被拒绝。启动时需求目录进入 `inputs/workspace`；空目录也保留。恢复与重试使用同一副本；新增文件、丢失目录、内容或可执行标志改变都会阻止任务启动。

实际容器和凭证清理使用完整镜像内容 ID，标签之后移动不会换掉镜像。声明的镜像若已被删除，任务失败并保留明确错误，不自动下载或改用当前标签。需求副本在复制到本次临时挂载目录后再次校验，再覆盖由历史还原的任务队列。清单哈希与实际镜像 ID 写入每次结果，JSON / HTML 报告保留固定范围。未提供清单的旧配置仍可运行，每次运行内部固定当时的镜像 ID，但不同任务间的环境一致性尚无保证。

这一清单固定续跑镜像和辅助需求目录。`grade.sh` 指向的官方评分资源可用下面的独立清单声明；不提供评分清单的旧配置继续使用原评分脚本，其外部输入未固定。

临时工作区复制完成后的校验在读取凭证和启动 Agent 之前执行。环境清单与评分用的结束快照使用独立状态；复制期间出现内容变化、额外文件或丢失目录都会拒绝运行。离线回归检查覆盖这些复制时变化，不能替代真实容器运行验证。

## 官方评分输入

在已有官方 SWE-Milestone 仓库、数据和 Python 依赖的 Linux 评分主机上生成清单。这个命令只读取文件和本地镜像元数据，不运行评分、创建容器或下载资源；评分主机需要已经安装 `yaml` / `pathspec`。输出路径必须在三个输入目录之外。

```bash
python3 -m ctxpress eval capture-grader \
  --code /absolute/path/to/SWE-Milestone \
  --data /absolute/path/to/navidrome_navidrome_v0.57.0_v0.58.0 \
  --trials /absolute/path/to/e2e_trial \
  --boundary 3:14 \
  --output runs/grading.json
```

`--boundary` 可重复，须有后端的官方里程碑/试验映射。将输出路径填入评测配置的 `environment.grading`，再生成计划；可以同时提供 `environment.snapshot`。清单记录官方 `harness`、版本 manifests、quarantine 配置、许可证及项目配置；所选里程碑的数据元信息、测试配置目录、分类与已有过滤名单；试验冻结的 repo config、runtime policy、捕获模板；配置中声明的后处理脚本；已安装的两个依赖包。镜像包括版本明确的里程碑、离线基础镜像和捕获模板绑定的原始 closure 内容 ID，不按当前标签替换捕获时的环境。

启动时这些文件进入 `inputs/grading`，按原相对路径重建目录。恢复和重试不再读取原仓库、原试验目录或原依赖包。每次评分前核对副本、捕获模板的镜像绑定和本地镜像内容；缺失镜像或变动输入会报错，不回退到原目录。所选宿主 Python 的二进制哈希写入清单并在调用前校验，评分子进程还检查实际 Python 版本与平台。

打包与评分通过 `-I -S -B` 启动独立子进程，从冻结副本加载官方 `SrcFileFilter`、`yaml` / `pathspec` 和官方 evaluator。Agent 运行前先用同一隔离路径导入评分入口，缺失依赖或不兼容入口会在模型调用前失败；这个预检不运行测试或启动容器。环境中的 `PYTHONPATH` 与 site-packages 不参与解析。适配器沿用现有 `make_sidecar.py` 的捕获配方；评分参数保持试验绑定的 repo config / runtime policy 哈希和 protected 模式。官方镜像解析函数被绑定到清单内容 ID；请求未声明的镜像会失败。官方报告的 harness revision 写为 `ctxpress-frozen:<评分清单哈希>`，避免复制目录误把父项目 Git HEAD 当作官方仓库版本。

评分清单哈希、实际镜像和固定范围写入结果及报告；子进程输出保存在本次尝试的打包/评分日志里。合成评分器返回的 `test_only` 标记会保留，不能作为真实效果证据。现有原打包脚本与冻结打包路径已用合成源码检查 tar 字节和全部元数据字段一致，这不证明真实任务评分已通过。

当前适配器面向本地 SWE-Milestone 的已映射边界和现有评分入口。宿主 Python 的标准库、动态链接系统库、Docker 服务端和其他宿主工具仍是运行时资源；只有 Python 二进制身份受到显式校验。因此评分输入副本与镜像 ID 固定不等于完整容器化评分环境，正式真实任务验证仍待运行设置确认。

`CodexAutoCompact(t=...)` 会把阈值写入 Codex 的实际启动配置；其他方法默认使用配置里的原生压缩阈值。组合方法携带宿主配置，冲突在启动前报出。模型名称与二进制目录均显式指定；研究脚本的历史默认值不表示当前最新版。

直接续跑与容器内安装路径都传入 `--no-daemon`，因此所选 CLI 需支持这个参数。可以先在兼容宿主上执行 `ctxpress doctor codex --codex-bin /path/to/codex --offline-check` 检查配置和转发；它只有合成用量，不属于正式实验。安装包装器让管理命令保持原始 CLI 行为。

## 后台与恢复

如果实验容器关闭 DNS，而宿主本身能直连 HTTPS 上游，可以在宿主启动限定目标的 CONNECT 转发入口，避免依赖另一个系统的仅回环代理：

```bash
# 地址须是本机 Docker bridge 的网关；不要直接使用示例地址代替核对。
python3 -m ctxpress.harness.runtime.connect_proxy \
  --bind 172.17.0.1 --port 0 \
  --target chatgpt.com:443 --target auth.openai.com:443 \
  --ready-file runs/network/relay.json
```

该命令前台运行，JSON 文件记录实际端口和进程身份；把其中的 `url` 填入评测配置的 `environment.via`，再生成计划。宿主负责域名解析，TLS 内容原样转发，不记录请求头或隧道内容。未声明目标及普通 HTTP 代理请求会拒绝；不会改动 Windows 代理、容器 DNS 或防火墙。运行期间须保持入口存活，任务全部结束后停止属于本次运行的进程。先做不带凭证的 TLS 连接检查，再开始真实任务；HTTP 401 可以证明到达上游，但不证明账号登录或模型调用可用。

每个计划保存一个 SQLite 任务日志，每个任务用独立进程、容器、日志和产物目录。调度器只运行有限的任务集合，结束后退出，不安装常驻服务。关闭观察终端不等于停止任务。

录制历史以完整 JSONL 副本恢复时，实验目录内的首条会话元数据使用 `history_mode: legacy`。分页导出文件若仍标为 `paginated`，新版 CLI 会查找未随导出提供的分页数据库，导致恢复失败。导入只适配存储标记，保留其他元数据、所有正文及原始文件；结果的 `session_restore` 记录源标记、实际标记和行数，不执行原用户目录的迁移。

输入副本使用内容哈希作为文件名，但会话目录必须恢复原目录记录的 `rollout-…-会话ID.jsonl` 名称；CLI 按该名称发现会话，不能直接使用哈希文件名。报告分别保留任务的结束状态和收到模型响应的观测证据，历史任务即使曾标成完成，也不会因此被认定已执行到模型。

没有观测到主模型请求，或请求全部未返回 HTTP 2xx 的续跑，任务状态为 `failed`，并保留 `result.json` 与原始日志。HTTP 成功只证明请求到达并获得响应；不代表压缩机制已触发、任务解决或正式评分通过。

再次执行同一条 `run` 命令会保留已完成的结果，并接回仍存活的任务。进程身份包含创建时间，避免 PID 重用；无法确认进程状态时不会自动重复实验。失败与中断单独记录，不会被默认重跑。

```bash
# 明确要求重试失败或已确认停止的任务
ctxpress eval run runs/mechanism-plan.json --directory runs/mechanism --background --retry
```

重试保留各次尝试的目录，先核对并清理属于该任务的旧容器和临时存档。凭证副本只能在容器内删除，确认文件消失后才删除宿主临时目录。清理失败会保留目录并报告失败，避免把未完成的清理写成成功。镜像必须事先存在；实验入口不会下载镜像。

### 取消、资源恢复和继续运行

全部家族的从头配置模板集中列在本页“概览”一节的表格。SWE-Milestone 的资源 capture specification 另需 `native_data_version: true`，以冻结实际本地 Git 版本证据；模板不自动捕获/下载资源。

固定 8 个家族共用以下入口。`benchmarks` 默认列出各模式的代码能力和真实验证状态；`--start-mode task_start` 仅列从头模式。版本/子集归到同一家族。

```bash
ctxpress eval benchmarks --start-mode task_start
ctxpress eval status --directory runs/evaluation
ctxpress eval cancel --directory runs/evaluation
ctxpress eval recover --directory runs/evaluation
ctxpress eval resume --directory runs/evaluation --background
ctxpress eval report --directory runs/evaluation
```

`cancel` 先在 SQLite 中停止派发并取消待运行任务，等调度器退出，再给已核对出生时间的 Linux worker 发 SIGTERM。worker 的 benchmark 资源保护正常展开；超过退出期限才终止其所属进程组。所有 worker 停止后，按 Agent/verifier/服务/网络/通信顺序调用对应恢复器，保持 daemon、镜像、标签和容器 ID 检查。身份不明、worker 仍活着或清理失败时保留 journal 和产物，状态为 `cleanup_failed`，不会重新派发。

`recover` 停止派发，确认 worker 已退出后仅清理资源，不启动模型、容器或评分。`resume` 先做同样恢复与冻结输入检查，再重试失败、中断或取消任务；已完成任务不重复，各次 attempt 的日志、代码/补丁和原始评分保留。取消后的普通 `run --retry` 拒绝启动，需明确使用 `resume`。未被取消的运行仍可通过原 `run --retry` 重试。并发控制操作有独立进程身份锁；停止派发的状态和取消计数出现在 status 与报告中。

SWE-Milestone 的从头代码能力已开放，官方 v1.0.2 Trial/生产资源组件的合成 IO 组合再次通过。计划要求 `run.grade=true` 和捕获 `native_data_version=true`；预检查本身仍记录零容器、零模型调用及 `execution_supported=false`，表示它只检查准备输入，不表示适配器缺少执行代码。准备 CPU 镜像、各节点 verifier 与服务镜像之外的资源请求仍返回明确缺项。

## 结果与费用

产物包括 `plan.json`、`jobs.sqlite`、每次尝试的 `requests.jsonl` / `result.json`，以及 `report.json` / `report.html`。报告保留未完成、失败、中断、无效基础设施评分和缺失用量；机制检查不证明任务效果。

每项任务的 `mechanism_observation` 区分收到成功响应、观察到渲染改写和具体操作。`operations_on_successful_requests` 仅汇总成功且未中断的请求上的新增操作，摘要按 observation、segment、history 分别记录。没有操作日志的旧结果只描述已观察到的改写，不能补推具体操作；未记录原生压缩的旧结果显示“未记录”。这些证据不自动证明原算法忠实度或覆盖全部触发条件。

用量汇总包括主模型、摘要 API 和有记录的原生压缩调用。专用剪枝模型的输入 token、未知用量及规则决策估计另列。配置若提供 `prices`，需要明确 `input`、`cached`、`output`、`unit: USD_per_million_tokens`、`source` 与 `as_of`。只在 API 用量完整时按这些声明价格换算；它不是整个实验的完整账单，也不包括尚未定价的专用模型计算。

用量和计价从原始主请求、摘要请求和已记录的原生压缩请求累加；token 数必须是非负整数，缓存 token 不能超过输入。请求/摘要计数、已声明总量与记录不一致时会显示用量冲突，不将缺失或非法数字当成零费用。缺失价格时仍保留 token 数据，费用显示未知。

## 与基线的质量约束比较

在配置中预先指定参考方法的唯一 label 和逐边界质量规则。比较设置进入原计划哈希，不能在保留同一计划的同时改动参考方法。

```json
"comparison": {
  "reference": "native-230k",
  "quality": "per_boundary_no_regression"
}
```

例如将 `CodexAutoCompact(t=230000)` 的 label 设为 `native-230k`，其余方法与它比较。CLI 会展开同一边界、同一重复编号的完整运行集合；重复编号是观测队列的对应关系，不表示模型使用了相同随机种子。质量规则比较每个边界的完整观测解决率，容差为零。总平均成功率更高也不能掩盖某个边界退步。

`configs/live_comparison.example.json` 展示 native-230k 与冻结 AutoCostModel 策略在两个已记录的大上下文边界上各重复三次，共 12 个任务。它是设置模板，尚未获准或执行；模型、二进制、两个资源清单、独立训练的策略及实际价格仍需填写和审核。没有价格时示例只报告用量，不输出金额节省结论。例中的边界 token 来自现有目录，执行时仍重新核对。

比较器保留所有计划运行。正式质量判断要求双方完成、官方有效评分及原始报告的内容哈希、评分版本、试验配置与 protected runtime policy、里程碑和捕获提交绑定均可核对；模型、推理设置、≥128k 的边界上下文和冻结续跑环境也必须匹配。原始评分中的捕获完整性需通过，不能用 legacy 未验证快照。缺少镜像/评分清单、失败、未完成、合成记录、被修改的报告或计划外任务声明会使对应 cohort 不完整，而不会删掉这些运行来提高成功率。

`report.json` 中的 `comparison` 保留逐对证据、缺失原因、各边界的计划/有效数量和约束结果；HTML 显示结论范围与费用。只有全部边界完整时才给出“观测质量约束通过/未满足”。机制检查不产生正式质量判断，合成数据会明确标注。

质量与费用分别判断。API 用量完整且价格已声明时，费用计入主模型和摘要调用，再比较整个 cohort 的总费用；缺失用量不会通过筛掉该运行来计算节省。质量退步时即使费用下降也不会给出满足质量约束的节省结论。专用剪枝模型、宿主计算和完整账单仍单独列出，这里判断的只是已报告 API 费用。

这是一项预先声明的观测约束检查，不是统计非劣效证明，不保证新任务质量，也不会自动修改已冻结的成本策略。真实效果结论仍需完成正式执行与官方评分。

正式评测用 `scope: formal` 和 `submit: true`。边界需声明至少 128k 上下文，执行前还核对基准目录中的原始输入 token 记录；原生压缩阈值至少 128k。正式设置、运行次数、时间与费用范围仍须先确认。示例配置用于展示接口，尚未做正式真实运行验证。

M1 入口也生成这套计划，默认只写计划，边界必须显式声明：

```bash
python -m ctxpress.harness.checks.mechanism \
  --model MODEL --reasoning medium --bindir /path/to/current-codex-bin \
  --scripts /path/to/benchmark-scripts --boundary 0:435:198018 \
  --only CodexAutoCompact ComplexityTrap CliffCompaction \
  --out runs/mechanism-plan.json
```

`--boundary N:J:CONTEXT_TOKENS` 可重复，表示录制坐标与声明的上下文大小，执行时仍核对目录。没有 `--only` 时，集合包含所有标注为可真实运行的方法及包装器、组合示例；重放专用的 WorkingView 不进入集合。SWE-Pruner 需要 `--pruner-url`，CostModel / AutoCostModel 分别需要独立训练得到的 `--cost-profile` / `--cost-policy`。默认集合会明确列出缺少资源而省略的方法；显式选择这些方法时缺少资源会报错。成本方法的计划生成不代表已经批准 M5 实验。

原生压缩默认 230k，CliffCompaction 为 200k，SlidingWindow / ACON 历史为 230k，Trigger 为 128k。ReSum 保留每 40 次请求的周期，短续跑可能没有摘要动作；跑通方法不证明每项操作都触发。AgentFold / ACON / ReSum 可使用同一上游摘要服务，这仍是机制检查范围。

`--environment-snapshot` / `--grading-snapshot` 可分别绑定上述清单，`--via` / `--upstream` 声明传输设置，`--timeout` / `--compact-limit` 声明每次运行的超时与原生压缩阈值。方法自身的触发设置保留在计划里，不因修改原生阈值而一并改写。设置确认后加 `--run`，使用同一后台队列与结果格式。示例边界不自动创建镜像、下载历史或启动实验。

连续派发由 `milestone.driver.execute` 经隔离 worker 调用 `milestone.runner`：先核对冻结输入/接口，再准备私有 itinerary、通过作者版本 gate、启动所属通信和容器，保存原生 trial metadata 后调用作者 TrialRunner。Agent 预算从首次调度开始，环境准备不消耗预算；`run.grading_timeout` 可覆盖退出时等待评分线程的期限，默认取作者配置 `evaluation_timeout`。退出后恢复 hook/信号，清理所属资源，再读官方成绩。父进程取消先请求 worker 退出，超时才终止进程组，确认终止后按 Agent、verifier、网络、通信通道顺序恢复。资源身份变化时保留日志并报错。新会话/恢复的请求日志合并到共同用量报告；离线替身验证、版本 gate 验证与旧提交的 773 项全套回归分别记录，不把它们混同为真实任务成绩。


`benchmark: swe-polybench` 使用 [PolyBench 配置](../configs/polybench.example.json) 和同一 `eval tasks/plan/run/status/report` 入口。数据目录提供 `instances.jsonl`（或 JSON）及 `dataset_manifest.json`；manifest 中 `dataset` 必须是 `AmazonScience/SWE-PolyBench`、`AmazonScience/SWE-PolyBench_500` 或 `AmazonScience/SWE-PolyBench_Verified`，`revision` 与资源 release 相同。数据使用作者字段 `F2P/P2P`、`Dockerfile`、`language`、`task_category` 和 `modified_nodes`，不转换成 SWE-bench 的 Python TestSpec。目录扫描保留 Python/Java/JavaScript/TypeScript 及 Bug Fix/Feature/Refactoring；Python 接口的 `task_instances(data, languages=[...], categories=[...])` 可筛选，CLI 计划按明确 task IDs 选择。

资源清单绑定 `polybench` 源码树（保留 `pyproject.toml` 与 `src/poly_bench_evaluation/` 布局）及完整 `dependencies` 树，包括真实 `poly_bench_evaluation` dist-info。固定 Linux Python ≥3.10，worker 用 `-I -S -B` 只导入冻结路径，核对源码、分发元数据与支持的 `evaluate_instance` API；缺少 repository parser 时在启动资源前拒绝。`agent_image` 与 `grading_images.verifier` 均已准备，内容 ID 固定，镜像必须声明相同的绝对 repository WorkingDir。所有容器用 CPU 与隔离网络，模型只走所属 socket；不构建、拉取或给镜像改标签。

Agent 停止、凭据删除及容器清理后，作者评分入口在全新 verifier 内按原顺序应用测试补丁与模型补丁、运行 `test_command`、调用语言 parser 与 F2P/P2P scorer。默认使用作者 Java/其他语言评分超时，显式 `run.grading_timeout` 可以覆盖并进入计划。空补丁与模型补丁失败保留官方失败语义；作者测试补丁失败使评测无效，不能计成任务失败。官方 `<instance_id>_result.json`、日志、prediction JSONL 和 patch 保存到单次运行并记录哈希。作者参考源码检索指标会触发额外 checkout，当前明确省略该指标，不输出虚构的零值；原生包 repair 也不启用。网络隔离属于 `ctxpress_comparison`，不是原榜单网络协议复现。协议依据为 [官方数据模型](https://github.com/amazon-science/SWE-PolyBench/blob/9c836c5d7f3cb991934132b77d29e6941d912a07/src/poly_bench_evaluation/polybench_data.py) 与 [官方评分入口](https://github.com/amazon-science/SWE-PolyBench/blob/9c836c5d7f3cb991934132b77d29e6941d912a07/src/poly_bench_evaluation/run_evaluation.py)。首题镜像与原始参考补丁评分已验证，真实模型试跑另行记录；`real_run_verified` 静态目录标志保持 false。

本轮先有 138 项 PolyBench、现有 SWE-bench 与共用入口检查通过，随后 48 项 PolyBench 检查通过，包括原数据/源码删除后的隔离 frozen worker preflight、版本/parser/API 拒绝、四语言评分调用顺序、空补丁/模型补丁/测试补丁失败、重复清理与相对路径产物。以上使用合成官方接口和假 Docker SDK，不是对真实作者依赖或容器的运行验证。

### SWE-bench Pro 的版本与独立评分

`benchmark: swe-bench-pro` 复用 `eval tasks/plan/run/status/report` 和已有资源捕获入口。数据根目录必须有 `dataset_manifest.json`，声明 `dataset: "swe-bench-pro"`、明确 `revision`、`benchmark_version: "v1"` 或 `"v2"` 及 `subset`。`revision` 与 resource release 一致；版本不能从目录或模型名称推断。两阶段使用 Linux CPU 的准备镜像，仅声明 `agent_image` 和 `grading_images.verifier`，不使用辅助服务/GPU。凭据仅进入 Agent 私有目录，评分侧不挂模型通道、Codex 或运行凭据。所有结果记录为 `ctxpress_comparison`，`real_run_verified: false`。

V1（[配置示例](../configs/swe_pro_v1.example.json)）提供一个 `instances.jsonl` 或 `instances.json`，使用作者字段 `instance_id/repo/base_commit/problem_statement/requirements/interface`、`fail_to_pass/pass_to_pass/selected_test_files_to_run/before_repo_set_cmd`；`subset` 必须为 `public`。Agent 只得到问题、requirements/interface 与干净 `/app` 仓库，测试定义和参考补丁不进入提示。测试 ID 列表用安全 literal parser 核对，交给作者入口时仍是原列表语义。实例 ID 中的 Git SHA 是 fixing commit，基准取 `base_commit`。

V1 的资源树为 `pro_v1` 与 `dependencies`。前者冻结作者 `swe_bench_pro_eval.py`、`helper_code/image_uri.py`，以及每个所选实例的 `run_scripts/<id>/run_script.sh`、`parser.py` 和 `dockerfiles/{base_dockerfile,instance_dockerfile}/<id>/Dockerfile`；后者冻结 Docker SDK、pandas、tqdm 及其完整依赖/metadata。评分调用实际作者 `eval_with_docker` 和 `main()`，保留其 binary hunk 处理、entryscript 与测试集合判定。作者的镜像 pull 步骤只绑定已有 image 内容 ID，不调用 registry；容器只挂此次评分的 `/workspace`。先核对干净基准，再执行原 entryscript，默认 3600 秒外层超时，`run.grading_timeout` 可显式覆盖。作者吞掉的 Docker 异常、外层超时或缺少 parser 输出使评测无效，不将其写成模型失败；正常 parser/verdict 保留作者结果。`official-logs/eval_results.json`、parser 输出、prediction 和两个清理 journal 的哈希保存在 `pro-v1-grade.json`。

V2（[配置示例](../configs/swe_pro_v2.example.json)）提供完整 Harbor 任务目录，manifest 另声明 `base_commits: {"<task-id>": "<full Git SHA>"}`，必须来自同一官方数据 revision；`subset` 为 `public` 或 `hard51`。HARD-51 另提供唯一 ID 列表 `hard51_ids.txt`，列表随数据冻结；成员来源由本地声明负责，不将任意 51 题称为官方子集。要求任务的 `agent.network_mode="no-network"` 和完整 `tests/test.sh/run_script.sh/parser.py/config.json/test_patch.patch`；仅支持单步骤，评分时另建 trial，不使用 Harbor shared/collect 模式。

V2 资源树为 `harbor`、`pro_tooling`、`dependencies`；`pro_tooling` 冻结作者 `locked_codex.py` 与 `patch_replay.py`。固定 Codex 包装 LockedCodex；Agent 进程与凭据清理后再次调用作者 capture，超时与预算停止保留部分补丁。Agent trial 禁用 verifier，完成容器清理后复制补丁并用准备的 verifier 镜像创建第二个 trial，调用实际 PatchReplayAgent 和官方 verifier，重新评分阶段模型调用为 0。重评分保持任务自身 verifier timeout，`run.grading_timeout` 不用于 V2。两个原始 result、补丁、镜像、基准和清理 journal 哈希写入 `pro-regrade.json`；只有 reward 而没有这套证据时，`read_grade()` 返回未知。协议依据为 [V1 官方实现](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/swe_bench_pro_eval.py)、[V2 官方协议](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/v2/README.md) 和 [官方数据字段](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro/blob/main/README.md)。

本轮两组回归分别为 190 项和 194 项通过，最终的完整两阶段/报告证据检查为 18 项通过。另对固定作者提交的实际 V1 evaluator/main 做五种合成 IO 组合核对，包括正常、空补丁、超时、缺少 parser 输出和错误基准；使用生产 Docker 资源边界，pandas/test IO 与 Docker 是替身，源码仅在内存中读取。没有真实容器、模型调用或 benchmark 成绩，作者完整冻结依赖和真实任务环境仍需之后核验。

### BigCodeBench 的代码样本与 pass@k

`benchmark: bigcodebench` 使用相同 `eval tasks/plan/run/status/report`、资源捕获、冻结 worker 和恢复入口，配置见 [示例](../configs/bigcodebench.example.json)。数据目录提供一个 `instances.jsonl` 或 `instances.json`，并在 `dataset_manifest.json` 声明 `split: "complete"` 或 `"instruct"`、`subset: "full"` 或 `"hard"`、明确 `revision`。Full 的 `dataset` 为 `bigcode/bigcodebench`，Hard 为 `bigcode/bigcodebench-hard`；数据 revision 与资源 release 相同。使用官方 `BigCodeBench/<number>` task ID 和 `complete_prompt/instruct_prompt/code_prompt/canonical_solution/test/entry_point` 字段。

Agent 只取得所选 prompt、entry_point 和 Complete 的公开代码起点，Instruct 从空文件开始；参考答案和测试留在评分输入。新的 Codex 会话在容器私有 `/ctxpress-task/solution.py` 写完整 Python 代码，退出、超时或预算停止后保留代码样本，包括空字符串；符号链接或不合法文件不会当作代码。导出 JSONL 使用官方 `task_id/solution` 格式，重复 task ID 表示独立样本，样本编号在执行/评分记录中另存，不输出 Git patch。

`repeats` 即每题、每方法的样本数，每个 repeat 独立启动和恢复。`run.code` 声明 `pass_k`（默认 `[1,5,10]`）、`calibrated`（默认 true）、`min_time_limit`（默认 1 秒）、`max_as_limit/max_data_limit`（默认各 30720 MB）和 `max_stack_limit`（默认 10 MB）；内存值是正整数。Calibrated 模式按作者 evaluate() 在完整 solution 前加 `code_prompt` 和 pass 起点。配置不接受温度或 top-p：此入口使用 Codex Agent，解码由模型服务控制，报告明确为 `ctxpress_comparison`。

资源只声明准备的 Linux CPU `agent_image` 与 `grading_images.verifier`，不使用辅助服务/GPU/额外 capability。冻结 `bigcodebench` 树的 `pyproject.toml`、`bigcodebench/__init__.py`、实际 `_version.py`、`eval/{__init__,utils,_special_oracle}.py`、`gen/__init__.py` 和 `gen/util/__init__.py`；`dependencies` 包含匹配的 BigCodeBench、Docker SDK、NumPy metadata、Python 扩展与完整测试库。Host preflight 只核对冻结官方 checker/API，不执行代码或评分；verifier 启动后再核对其 Python ABI 和真实包版本。冻结依赖须与准备 verifier 的 Python major/minor 相同，不自动安装缺失库。

Agent 进程、凭据与容器清理完成后，独立 verifier 仅挂只读运行代码、官方输入和本次评分 payload，以及所属可写输出目录；不挂 Codex、模型通道或凭据。评分先用实际 `trusted_check` 运行参考答案并取得作者计时，参考答案失败使评分无效。随后实际 `untrusted_check` 运行样本，保留作者测试 details 和 pass/fail/timeout。参考答案与样本的子进程、资源限制和内部超时来自冻结作者代码；`run.grading_timeout` 是外层完整评分超时，默认 1800 秒，触发时是基础设施无效，不能冒充作者 sample timeout。

原始结果保存在 `official-logs/outputs/sample-result.json`，`code-resources.json` 绑定样本、Agent/verifier 镜像及两个清理 journal 的哈希。报告重新读取这些原始证据，再按同一任务/方法的完整样本集合汇总；pass@k 使用实际作者 `estimate_pass_at_k` 在独立 verifier 中计算的冻结估计值。少于 k 个样本时该 k 保持未知，缺少计划样本或评分证据时整个方法的 pass@k 保持未知。每题 n、通过样本数、任务集合、calibration 和所选 k 分别保留，代码样本不进入仓库任务的解决率，也不能作为长上下文压缩证据。[官方 local evaluate 实现](https://github.com/bigcode-project/bigcodebench/blob/09dd993f46c3fbf3a799465bb96d524edcb0b199/bigcodebench/evaluate.py) 与 [官方 checker/估计器](https://github.com/bigcode-project/bigcodebench/blob/09dd993f46c3fbf3a799465bb96d524edcb0b199/bigcodebench/eval/__init__.py) 是协议依据；已取得一个原始任务的真实通过样本，仍不构成完整 benchmark 成绩；`real_run_verified` 静态目录标志保持 false。
