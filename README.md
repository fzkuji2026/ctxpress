# ctxpress

ctxpress 是编码 Agent 的上下文管理工具箱，思路与 [kvpress](https://github.com/NVIDIA/kvpress) 相同：
- 自带多种方法；
- 能即插即用到现成的 Agent 里；
- 换方法只改一个参数。

上下文管理指每次请求前改写历史：换占位符、截断、只留结构、删除、专用记忆、摘要。

路线图见 [ROADMAP](ROADMAP.md)。真实评测的入口和协议见下文“真实评测”一节和 [docs/evaluation.md](docs/evaluation.md)；[ACM](docs/acm.md) 的工具适配与作者模型运行入口分开说明。

**支持的系统：**
- 框架（方法、代理、接入 Codex / Claude Code、MCP 工具、`install`/`doctor`）：Linux、macOS、Windows。
- 正式评测（`ctxpress eval`：容器内执行、官方评分、资源清理）：只支持 Linux；Windows 上在 WSL 里运行。评测依赖 Linux 容器、进程组和文件权限检查来保证可复现，执行进程在其他系统上会拒绝运行。
- 测试在 Windows 上会跳过评测相关的 Linux 专用用例（`linux_only` 标记）；没有开发者模式时，创建符号链接的用例也会跳过。macOS 上尚未实测。

## 快速上手：装进你自己的 Codex

```bash
git clone https://github.com/fzkuji2026/ctxpress.git && pip install ./ctxpress
ctxpress install codex --method ComplexityTrap --budget 10
codex
ctxpress use Pichay --args '{"age": 4}'
ctxpress list
ctxpress status
ctxpress uninstall codex
```

- `pip install ./ctxpress`：只依赖 Python 标准库。
- `ctxpress install codex`：选定一个方法，打开新终端后生效。
- `codex`：和平时一样启动，会自动用上选定的方法。
- `ctxpress use`：切换方法，下次启动 codex 生效。
- `ctxpress list`：列出所有方法、对应论文、需不需要额外模型、能否真实运行、与原实现的差别。
- `ctxpress status`：查看当前方法，以及最近几次运行改了什么。
- `ctxpress uninstall codex`：恢复成原来的 `codex`。

**输入 `codex` 时发生了什么：**

`ctxpress install codex` 在 shell 配置（`~/.bashrc`、`~/.zshrc`、PowerShell 的 `$PROFILE`）里加了一个同名的 `codex` 命令。之后每次输入 `codex`：
1. 对话运行在当前进程里启动 ctxpress 代理，用一个空闲的本机端口；`login`、`mcp`、`features`、帮助等管理命令直接交给原始 CLI；
2. 把这个端口写进本次运行独立的配置档 `~/.codex/ctxpress-<run-id>.config.toml`，若指定了已有 `--profile`，先合并其设置到独立副本，再用本次的 `--profile` 和 `--no-daemon` 启动原来的 Codex；
3. Codex 的每次模型请求都先经过选定的方法，再发给真实 API；
4. Codex 退出，代理随之结束，删除本次配置档，并打印这次方法改了什么，以及 API 报告的 token 用量（包括摘要调用）。

**有几点保证：**
- 没有常驻的后台服务。
- 不修改 Codex 本身，也不修改你的 `config.toml`。
- 多个窗口各用各的代理、配置档、日志和原文存档；同一运行里的不同对话也分别存档。
- `ctxpress use NoCompaction` 保留主请求历史，关闭提前压缩；达到宿主窗口上限时拒绝原生摘要并停止当前轮次。

模型地址写在配置档里，而不是用 `-c` 传：ChatGPT 登录时，Codex 不接受命令行传入的 `openai_base_url`（已在容器里实测）。

**当前 CLI 的离线接入检查：**已用 `codex-cli 0.159.0-alpha.12.1` 对本地假 API 检查默认配置、已有配置档和自定义 Responses 提供商的请求接入、模型设置、方法说明、用量解析与配置档清理。没有真实任务、真实模型费用或登录验证；不替代上面的正式效果验证。配置档布局依据 [OpenAI 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。

```bash
ctxpress doctor codex --codex-bin /path/to/codex
ctxpress doctor codex --codex-bin /path/to/codex --offline-check --output runs/codex-offline.json
ctxpress doctor codex --offline-check --scenario profile
ctxpress doctor codex --offline-check --scenario custom-provider
ctxpress doctor codex --codex-bin /path/to/codex --offline-tools-check --directory runs/codex-tools --output runs/codex-tools.json
```

`doctor` 不下载 CLI、不复制登录凭证。默认只检查所选二进制版本、哈希和能力；`--offline-check` 用独立空目录、假认证与本地 SSE 响应运行真实 CLI，辅助联网请求由本地拒绝代理拦截。报告里的用量明确标为合成数据。当前启动器针对支持独立配置档和 `--no-daemon` 的新版 CLI；`--ignore-user-config` 和远程服务器模式会绕开本地配置，因此直接报错。已有配置档的模型、推理强度、权限和其他 MCP 服务会保留。自定义 Responses 提供商从用户配置、所选配置档和命令行覆盖中确定；其 `base_url` 自动作为上游，运行副本把请求地址改为本地代理。显式 `--upstream` 或非默认 ctxpress 上游设置优先。提供商认证字段留在配置中，不放进生成的命令行参数；原有配置文件保持原样。

运行必需的代理地址、关闭请求压缩、ctxpress 的 MCP 配置，以及方法声明的原生阈值优先于用户的冲突覆盖。其他命令行配置保持原有顺序，`--` 后的提示文本原样传递。配置档叠加方式依据 [OpenAI 高级配置文档](https://learn.chatgpt.com/docs/config-file/config-advanced)。当前接入支持 Responses 提供商；内置本地模型模式需要显式设置兼容的代理端点。

TOML 解析在 Python 3.11+ 使用标准库；Python 3.10 使用包内随附的 MIT 许可解析器（来自本机 CPython 3.12 标准库），无需新增运行依赖。

`--offline-tools-check` 用真实 CLI、ctxpress MCP 和代理检查 CWL 的 `delimiter`、DTOC 的 `manage_context`，核对调用结果是否改变下一次模型请求。已在上述 CLI 版本跑通探索片段淘汰/依赖恢复、工具输出隐藏/恢复，并确认独立配置档清理。MCP 从 ctxpress 包所在目录启动，因此 Codex 可在其他项目目录工作；需要自带工具的方法要求 MCP 成功启动。内置工具的只读提示指不修改宿主文件或访问外部服务，上下文登记状态仍可改变；自定义方法需自行声明工具提示。配置字段依据 [OpenAI MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

**只跑一次、不安装：**

```bash
ctxpress codex --method ComplexityTrap --budget 10 -- exec "fix the failing test"
ctxpress codex --method ARC --budget 10 -- --profile research exec "fix the failing test"
```

**统一的保留量参数：**支持保留量的方法接受 `budget`，CLI 可用 `--budget`，Python 可用 `ContextManager("ARC", budget=10)` 或 `apply("ARC", items, budget=10)`。`ctxpress list` 显示每个方法的范围、单位和默认值。

| 方法 | budget 的范围与单位 | 兼容的旧参数 |
|---|---|---|
| `ComplexityTrap`、`ARC`、`ClawVMApprox` | 最近的工具输出条数 | `n` |
| `KeepLastTokens` | 最近工具输出原文的 token 总量 | `b` |
| `ScoredMethod` | 可处理工具输出当前表示的 token 总量 | `budget` |
| `ClawVM` | 驻留页表示的 token 总量 | `budget` |
| `TokenPilot`、`EntryTruncation` | 单条输出入口截断的 token 目标 | `entry_budget` / `budget` |
| `AgentFold` | 保留完整内容的最近已完成工作段数 | `keep_segments` |

旧参数、位置参数和默认值保持兼容。显式同时给出不同的 `budget` 与旧参数会报错。条数预算允许为零，ARC 此时把全部可处理输出换为存档引用；入口截断预算至少为 1。预算遵循各方法已有算法，固定指令、受保护内容、调用记录、占位符和摘要仍有开销，不能当作整个请求的硬上限。

`PinRequirements`、`WithMemory`、`Trigger` 的声明式配置把顶层 `budget` 传给 `inner`；`EntryTruncation` 使用自己的入口预算，内部方法的预算独立设置。`Composed` 要逐个设置内部方法。Pichay 的轮龄、ReSum 的周期、Codex 原生压缩阈值以及冻结成本策略保留各自参数；对这些方法传 `budget` 会明确报错。

**其他宿主：**
- 用 `ctxpress serve` 单独启动代理，把宿主的 API 地址指向它即可。
- 目前支持 OpenAI Responses API 和 Anthropic Messages（Claude Code，见下文）；Chat Completions 在[路线图](ROADMAP.md)中。

**方法可以组合：**

| 包装器 | 作用 |
|---|---|
| `Composed` | 串联多个方法 |
| `EntryTruncation` | 入口处截断 |
| `PinRequirements` | 需求文档固定保留 |
| `WithMemory` | 专用记忆 |
| `Trigger` | 超过阈值才动作 |

组合方法按顺序看到前一步保留的内容。例如 `EntryTruncation(ScoredMethod(budget=16000), budget=2000)` 先截断单条输出，再按总预算处理旧输出。共用截断与结构化操作不会重新读出存档原文；无法继续缩小时保持当前表示不变，原文仍可通过存档找回。

**新方法：**大多数方法只差一个打分函数，继承 `ScoredMethod` 即可，见 [docs/new_method.md](docs/new_method.md)。

Responses 上游明确返回 HTTP 400 超长错误时，声明了能力的方法可通过共用代理调整当前历史并重试。`CliffCompaction` 提供作者的升级阶梯，最多重试 4 次；不重新摄入生成的摘要。其他错误原样返回，较新会话请求会使旧重试失效。每次上游尝试单独计入日志；失败请求缺少用量时，不假定其费用为零。这一路径通过本地假 API 和合成作者代码对照，真实任务行为仍待验证。

`NoCompaction` 的宿主语义已在固定 CLI `0.159.0-alpha.12.1`、同一模型目录和本地假 API 上验证：240k 合成输入用量处继续，270k 处在摘要发往上游前停止；没有扩大模型窗口。API key 自定义提供商与假 ChatGPT 登录两条路径均验证。原生压缩除旧 `/responses/compact` 端点外，也可能通过 `/responses` 的 `request_kind=compaction` 元数据发送；两者均单独记录且不再经过方法改写。本地拒绝记为 `native_compaction_blocked`，不计作已发送的 API 调用。旧快照没有这项保证，旧日志的零压缩计数不能证明未发生压缩；统一分析的 `native_compaction_metadata_coverage` 明确区分完整识别与历史未知。这是离线检查，不构成真实长任务效果证明。

## 在 Claude Code 里使用

```bash
ctxpress claude --method ComplexityTrap --budget 10            # 常规交互对话，参数照常写在 -- 之后
ctxpress claude --method DTOC -- --continue
```

`ctxpress claude` 在本机回环启动代理，只给这次启动的 Claude Code 设置 `ANTHROPIC_BASE_URL`，上游取原有的 `ANTHROPIC_BASE_URL` 或 `https://api.anthropic.com`。登录方式（API key 或订阅登录）不变，代理转发请求头、不记录请求头，也不写入或修改 Claude Code 的设置文件。方法的 Agent 工具与 `ctxpress_retrieve`、`ctxpress_status` 由 `ctxpress mcp` 提供（`--mcp-config`），并用 `--allowedTools` 预先放行；`--no-tools` 不提供这些工具。Claude Code 自己的自动压缩保持用户设置。

Anthropic Messages 把工具调用、工具结果和思考放在消息内的块里。代理把每个块展开成 Codex 用的同一种条目，套用同一个方法，再组装回合法的请求：角色交替、每个 `tool_use` 的结果在下一条用户消息里且排在最前、最后一轮助手消息的 thinking 原样保留；`cache_control` 取自当前请求，不参与历史比对（Claude Code 每轮移动缓存断点）。没有工具的旁路请求原样转发；子 Agent 和不同模型的对话按首条消息与模型分开。Claude Code 每轮结束后用完整历史加一句指令请求"下一句建议"，回答随后丢弃；这类一次性分叉之后，下一轮从分叉前的方法状态继续，不算历史改写（Codex 同样适用）。用量按 Anthropic 流计入：输入 = 未缓存 + 缓存读取 + 缓存写入，分别记录。

常规交互验证：`python -m ctxpress.harness.interactive_check --host claude --bin <claude>`（或 `--host codex --bin <codex>`）在 tmux 里启动真实 TUI，键入两轮对话，假模型在第一轮调用 `ctxpress_status`；第二轮请求必须带着前一轮的问答、被 DTOC 改写的工具输出和方法说明，整个会话不能出现历史重置，退出后进程正常结束。Claude Code 2.1.59 与 Codex 0.159.0-alpha.12.1 在 WSL 上均通过；配置放在临时 `CLAUDE_CONFIG_DIR` / `CODEX_HOME`，凭据为假值，不是真实模型证据。

## 代码结构

```text
ctxpress/
  core/        共用引擎：条目和记账、轮次、宿主大小，以及占位符 / 截断 / 结构化 / 删除 / 段总结 / 整体摘要等操作；参数；文本操作；再用模型
  methods/     每个方法（或一族）一个文件，都只用 core 的操作写成；REGISTRY 和方法说明表在 __init__.py
  live/        真实运行：LiveContext（一段真实对话）、Rewriter（请求 ↔ 上下文条目）、代理、MCP 工具服务、用量计费、会话记录
  settings.py  已安装的配置（$CTXPRESS_HOME）
  hosts/       接入的 Agent：codex/（`ctxpress codex`、`ctxpress install codex`）、claude/（`ctxpress claude`）
  replay/      离线重放预筛（结果标为模拟）：读取轨迹、运行配置、约束指标、再用曲线
  harness/     评测：在容器里跑真实任务、官方评分、结果比较、过程评测
  benchmarks/  评测用的各 benchmark 适配器
repro/         与各论文原版代码逐条对照的脚本（见 repro/README.md）
tests/         单元测试和对照测试
configs/       实验配置
```

依赖只朝一个方向：`core` → `methods` → `live` → `hosts` → 命令行，`settings` 不依赖任何模块、各层都可读取；`replay` 只用 `core`、`methods`；评测（`harness`、`benchmarks`）可以用框架的一切，框架不引用评测。`tests/test_layering.py` 检查这一点。

同一个方法类既用于真实运行（`live/`），也用于重放预筛（`replay/`）。框架缺少某个方法需要的概念时，补进 `core/`，不在方法里另写一套。

```python
from ctxpress import build, LiveContext, Rewriter
method = build({"class": "ComplexityTrap", "args": {"budget": 10}})
```

## 它做什么

> **关于"重放"：**本节描述的是重放模拟器：在录制好的轨迹上算账。它只作为跑真实实验之前的快速筛选，结果都是模拟的。结论以真实执行为准，见"真实执行"一节。

> 文中的 §N 指本研究预实验报告的章节，报告随论文公开。

输入是 Agent 的真实会话轨迹（Codex 或 Claude Code 的 JSONL）。引擎按原顺序重放每次请求，并在每次请求前让方法改动上下文，然后统一计算：

| 部分 | 内容 |
|---|---|
| 计费 | 命中缓存的前缀按 `cached` 价，其余按 `write` 价；输出按 `out` 价。缓存按与上一次请求的最长公共前缀计算，也可以只命中某次完整请求的末尾；按轨迹时间戳计算缓存过期（TTL） |
| 需要什么 | 改一个文件需要它最近一次被读的内容和之后的补丁；第一次读或改某个文件，需要列出它的搜索结果；每次改代码都需要需求文档 |
| 找回 | 需要的内容不在上下文里时，Agent 以概率 q(形式, 类型) 把它找回来（多一次请求），否则就是一次静默缺失。被截断或只留结构的内容，先在实际保留的文本上检查补丁的锚点行还在不在 |
| 重新探索 | 删掉正在用的工具输出，Agent 会重做一部分工作（e_warm / e_cold，在 §4 上拟合） |
| 窗口 | 超过 230k 时，由共用的兜底摘要压缩一次 |
| 决策开销 | 用 LLM 或小模型打分的方法，打分的费用计入成本 |
| 延迟（次要指标） | 额外请求的模型时间，加上重新执行命令所需的时间（来自轨迹时间戳） |

性能的硬约束：每条会话都不能比标准压缩差。标准压缩指 Codex 默认的 auto-compact（230k）。约束检查用三种代理指标：strict、harm、measured，见 `ctxpress/core/metrics.py`。

## 框架：三层、五个维度、专用记忆

| 部分 | 可选的值 | 代码里的操作 |
|---|---|---|
| 第 1 层：单条工具输出 | 原文、截断、结构化压缩、占位符、删除 | `sim.truncate` `sim.structure` `sim.to_placeholder` `sim.delete` |
| 第 2 层：一段工作 | 按"读→改→测"循环或用户消息分段，已完成的段折叠成段总结 | `sim.segments()` `sim.summarize_segment` |
| 第 3 层：整个会话 | 整体摘要（保留什么、多长） | `sim.summarize(keep=..., keep_user=...)` |
| 时机 | 入口处、每次请求、超过阈值、一段结束、由成本决定 | `on_ingest`、`step` |
| 找回 | 重新执行原调用、从专用记忆按编号取回、压缩时提示重读 | `memory = 'id' / 'label'`、`hint = True` |
| 决策器 | 固定规则、小模型 / LLM 打分（计费）、成本模型 | `overhead()` |
| 内容类型 | 需求文档、读代码、读其他文件、搜索、运行 / 测试、补丁、其他调用、文字、推理、记忆注入、摘要 | `trace.content_type` |

## 已实现的方法

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

**与原版的对照：**有公开代码的方法，`repro/` 下的脚本把同样的输入交给作者的代码和 ctxpress，逐条比较输出。Complexity Trap、CliffCompaction 和 ClawVM 已通过各自记录范围的对照；Pichay 的操作次数一致，重构后有 68 次请求的存根描述受原版只追加消息的行为影响，详见 [repro/README.md](repro/README.md)。SWE-Pruner 的 40 个输出处理用例已与作者源码对照，真实模型的轨迹核对单独记录。`ctxpress list` 列出每个方法与原版的差别。

ClawVM 的作者对照覆盖页选择算法；真实历史中的页与需求由本项目适配。全文需求按历史里的最新模型轮次生成，同一轮的并行工具输出一起处理，冻结历史首次载入和历史重建也遵循此规则。全文需求仍受作者选择器的优先级与预算约束，不能保证超预算输出保留全文。

CWL、DTOC 的字符估计已对齐 JavaScript UTF-16 长度，emoji 和其他非 BMP 字符按两个代码单元计算；新增测试直接运行作者 TypeScript，比较混合文本与预算边界前后一单位的上下文。AgentDiet 的反思与 Complexity Trap 的摘要对照仍使用确定性的假模型；实际模型输出和已发表任务分数尚未复现。Claude 已于 2026-10-04 独立复核 ClawVM 冻结历史、增量历史和并行输出；修复前的旧运行不进入后续正式比较。

**无法观测的部分：**有些论文依赖 LLM 判断内容是否相关（SWE-Pruner、TokenPilot、AgentFold），重放时观测不到这种判断，所以改用最接近的可观测规则，并在类的说明里写明。

## 成本模型（本文）

`CostModel` 在每次请求前，按期望成本比较以下几种做法，选最便宜的一种：

- 什么都不做；
- 第 1 层：一批内容各用其类型允许的最好操作；
- 第 2 层：折叠已完成的段；
- 第 3 层：整体摘要。

参数全部从其他会话自动估计，不手调：

- 再用曲线 p_k(a)、m_k(a)：按类型和闲置步数估计，并向全体曲线收缩；
- 截断和结构化压缩后仍够用的比例：在训练轨迹的真实文本上测量；
- λ：用留一会话法选，取能让其余会话都满足约束的最小值，找不到就退回标准压缩。

| 开关 | 含义 |
|---|---|
| `lookahead=8` | 比较"现在删"和"等 1/2/4/8 次请求再一起删" |
| `harm='harm' / 'measured'` | 静默缺失按危害加权 |
| `ops={类型: [placeholder, truncate, structure]}` | 每类内容允许的操作 |
| `segments=True` | 第 2 层段总结 |
| `memory='id' / 'label'` | 专用记忆，占位符带编号，或带编号和标签 |
| `hint=True` | 摘要后提示重读需求文档和正在改的文件 |
| `item_model=True` | 逐条预测再用概率（需要 scikit-learn） |
| `cache_ttl`（参数） | 缓存已过期时，删内容不再付缓存失效的代价 |

历史统计可以离线拟合成一个 JSON 文件，再直接接入真实代理：

```bash
ctxpress fit --manifest configs/train.yaml --output ~/.ctxpress/cost-profile.json
ctxpress use CostModel --args '{"profile": "/absolute/path/cost-profile.json", "lam": 1000000}'
codex
```

拟合命令需要 `pip install -e '.[sim]'`；在线加载只用标准库。训练清单应明确列出历史会话，`--sessions` 选择会话，`--exclude` 排除评测会话。文件包含再用曲线、长度分布、保留覆盖率和输入哈希，不保存工具原文。`fit` 只拟合统计量，上面的示例 λ 用于展示手动配置。

需要把自动 λ 选择一起部署时，使用 `tune`：

```bash
ctxpress tune --manifest configs/train.yaml --exclude evaluation-session \
  --lambdas 0 100000 1000000 5000000 --kind strict --workers 2 \
  --args '{"lookahead": 8, "segments": true}' --output ~/.ctxpress/cost-policy.json
ctxpress use AutoCostModel --args '{"policy": "/absolute/path/cost-policy.json"}'
codex
```

每个历史会话分别留出作筛选，其余会话拟合统计量。选择在所有留出会话上满足模拟约束的最小 λ；没有候选通过则明确退回 `CodexAutoCompact`。最终统计量只在所选历史会话上重新拟合，策略文件同时保存方法选项、逐折证据、参数和哈希。`--params` 可以显式指定计价比与找回假设，`--reference-limit` 指定原生回退阈值；筛选窗口与这个阈值保持一致。加载策略时复用这些参数，拒绝与宿主显式参数冲突。见 [docs/cost_policy.md](docs/cost_policy.md)。

`tune` 是离线模拟筛选，没有真实任务质量保证。上线仍需真实执行和官方评测；不能把历史会话的模拟约束通过率当作新任务的性能保证。

只打开占位符和摘要、关闭记忆，并用 §8 的记账（`spec_recovery=False`、`dedupe_recovery=False`、`recompress_retention=1.0`）时，它与 §8 的模型逐位相同（`tests/test_repro.py`）。默认用修正后的记账：摘要后重读当前需求文档要计费，找回的副本替换原件，每轮摘要保留率取实测的 0.975（实验页 §9.4）。

本轮交付范围固定为 **8 个 benchmark 家族**：SWE-Milestone、SWE-bench、Terminal-Bench、Terminal-Bench-Science、DeepSWE、SWE-bench Pro、SWE-PolyBench、BigCodeBench。版本与子集不额外计数。数据读取、从头执行与官方评分代码均已接入，各自支持的资源和协议范围见 [路线图](ROADMAP.md)。统一入口本地检查已通过，全部从头适配器仍保持 `real_run_verified=false`；真实评测在代码收尾后另行安排。

## 安装与运行

```bash
pip install -e .[itemmodel,test]
python -m pytest tests -q
python -m ctxpress run configs/constraint.yaml
python -m ctxpress run configs/components.yaml
python -m ctxpress session navidrome-a TokenPilot
python -m ctxpress params
python -m ctxpress curves
python -m ctxpress coverage
```

录制数据与作者对照测试读取 `CTXPRESS_DATA`（默认仓库旁的 `../data`）。例如，`CTXPRESS_DATA=/path/to/data python -m pytest tests -q -rs` 会列出缺少数据或平台条件导致的跳过；数据缺失时的跳过不算已通过对照。`configs/sessions.yaml` 等清单里的相对 `root` 按仓库根目录解析；旧 §8 对照测试使用临时 manifest 绑定此目录，仓库副本也不要求目录名为 `ctxpress`。

CWL/DTOC 的作者 TypeScript 对照还要求支持 `--experimental-transform-types` 的 Node（本轮用 24.10.0）。可将隔离 Node 的 `bin` 目录加入这次测试的 `PATH`；无需改动系统 Node。

- `run constraint.yaml`：复现 §8.7 的表 22。
- `run components.yaml`：在成本模型上逐个打开框架里的各项功能。
- `session`：在一条会话上跑一个方法，打印全部指标。
- `params`：列出每个参数和它的来源。
- `curves`：输出再用曲线。
- `coverage`：截断和结构化压缩后，后续修改要用的内容还剩多少。

结果写到 `results/<name>/`，包括：

- `runs.json`：每次运行的全部计数；
- `summary.json`：约束检查结果；
- `report.md`：表格。

## 加一个新方法

```python
from ctxpress.methods.base import Method

class MyMethod(Method):
    name = "My method"
    memory = None            # 或 'id' / 'label'
    framework = dict(L1="...", L2="无", L3="...", cross="无", memory="无", decider="固定规则")

    def on_ingest(self, sim, item):   # 内容进入上下文时
        ...
    def step(self, sim, r):           # 每次请求前
        for s in sim.outputs():
            if r - s["last"] > 10 and s.get("form", "full") == "full":
                sim.to_placeholder([s])
```

放进 `ctxpress/methods/` 下自己的文件，在 `ctxpress/methods/__init__.py` 的 `REGISTRY` 和 `METHODS` 里注册，然后就可以在配置里写 `{class: MyMethod, args: {...}}`，或者 `ctxpress use MyMethod` 在 Codex 里真实运行。

## 加一条会话或一个宿主

- **加会话：**在 `configs/sessions.yaml` 里加一条，`harness: codex` 或 `claude-code`。
- **加宿主：**在 `ctxpress/replay/loaders/` 里写一个 `load(path, ...)`，返回与 `core/trace.py` 说明相同的结构。

## 在真实宿主里使用（编程接口）

Python 宿主可以直接改写 Responses 请求，不启动代理：

```python
from ctxpress import ContextManager, apply

manager = ContextManager({"class": "ComplexityTrap", "args": {"n": 10}})
request = manager.apply({"model": model, "input": original_history}, session="task-1")
response = client.responses.create(**request)

# 单次使用；每次新建状态，适合无需跨请求状态的方法
items = apply({"class": "ComplexityTrap", "args": {"n": 10}}, original_history)
```

持续会话应复用同一个 `ContextManager`，每次交入宿主保留的完整原始历史。方法的轮龄、摘要计数、原文存档按 `session` 分开；多个 manager 的磁盘存档也分别保存。传完整请求时保留模型、工具及其他字段；传条目列表时返回列表，调用不修改原输入。`return_info=True` 返回改写诊断；其中的 token 大小是估计，并非 API 用量。`manager.retrieve(id, session=...)` 取回该会话的原文。

正常追加历史时，方法状态继续保留。重复的相同消息各自计数；若宿主修改、删除或重排先前条目（包括自行压缩历史），活动状态按当前历史重新建立，轮龄和摘要周期从新历史开始。旧摘要与复用索引随之清除；原文存档及其编号仍可取回，新版本使用新编号，不覆盖旧内容。诊断中的 `history_rebased` 标明这次是否重建，`history_epoch` 记录重建次数；`request` 仍是整个会话的累计请求数。

声明式方法配置与外部统计、策略文件在构造 manager 或启动 Codex / CLI 代理时加载一次；各会话使用独立副本。修改文件在下次构造或启动时生效，当前运行不会中途切换策略。显式传入自定义方法工厂时，由工厂负责返回独立对象。

目前这一接口支持 Responses `input`，不接受 Chat Completions 的 `messages` 或依赖 `previous_response_id` 的隐藏历史。字符串 `input` 原样传递。`summarizer` 回调、专用存档目录和方法工厂可在构造 manager 时提供。`manager.codex_config` 暴露方法需要的原生配置，由宿主负责应用。

`ctxpress.LiveContext` 把同一个方法和同一套文本操作用在一段真实对话上：

- 添加内容：`add_message` / `add_call` / `add_output`；
- 每次请求前：`before_request()`，返回要发送的历史；
- 取回原文：`retrieve(id)`，从专用记忆按编号取回。

**给 Agent 的工具：**方法可以声明自己的工具（`agent_tools`，例如 CWL 的 `delimiter`、DTOC 的 `manage_context`）；`ctxpress codex` 启动的 `ctxpress mcp` 按当前方法提供这些工具，代理在之后的请求里看到调用与结果。

ReSum、ACON、AgentFold 和 Complexity Trap 的 LLM-Summary / 混合方案通过代理直接调用当前 Agent 的同一上游模型写摘要。复现方法可以自带原文提示词（`summarize_segment(..., prompt=(system, user))`），服务原样发送，不加通用信封；摘要也可按方法的格式与角色插回（例如 Complexity Trap 的助手消息 "Checkpoint for the last N turns:"）。Python 接口也接受 `summarizer(items) -> str`，或带 `summarize(items, purpose, budget)` 方法的服务。单条输出、工作段和整个历史共用这项能力；调用失败时保留原文，媒体及其调用配对、系统和开发者指令保留。日志记录摘要 token 和耗时，不记录请求头。

共用服务还把后续用户修正和新增系统、开发者约束交给摘要模型，均作为数据传入。只有完成的 Responses JSON 或终止事件包含有效文本时才提交摘要；排队、进行中、失败、空文本或中断响应保留原历史。宿主的加密 `compaction` 条目受保护，不能交给文本摘要或删除规则处理。

ReSum 的默认摘要指南依据 [v3 附录 C](https://arxiv.org/html/2509.13313v3) 改写：整理与任务有关、明确有依据的信息，用 `<summary>` 返回，不强制提供缺口清单或行动计划；仍保留周期触发近似，没有移植作者的提示词全文、续接模板或训练。ACON / AgentFold 默认使用通用续接摘要。上述方法的任务效果尚未复现。没有摘要服务的 Python 调用会跳过摘要，把窗口管理留给宿主。

每次摘要操作可以传入 `guidance`，因此组合方法各自选择指南。`ReSum(summary_guidance=...)`、`AgentFold(summary_guidance=...)` 和 `ACON(history_guidance=..., observation_guidance=...)` 将配置传给共用服务；声明式 `--args` 和评测计划也保留这些字符串。`None` 使用通用指南。ACON 的作者指南经过任务数据优化，本项目没有完成该优化流程，传入指南也不证明原方法已复现。[作者说明](https://arxiv.org/html/2510.00615v2)

Python 自定义服务可实现 `summarize_with_guidance(items, purpose, budget, guidance)`。已有 `summarizer(items)` 或 `summarize(items, purpose, budget)` 接口继续使用自己的提示词；方法提供的指南未传入时，诊断计数 `summary_guidance_skipped` 会增加。指南不会写进用量日志，Responses 服务仍使用当前 Agent 的模型和连接。

运行日志分别记录主模型、摘要 API 和已观察到的原生 `/responses/compact` 调用的 token 用量、剪枝服务返回的输入 token，以及规则决策的 `method_overhead_estimate`。原生压缩请求和响应原样转发，单独记录 `native_compaction_calls`，缺失用量保持未知；旧日志未记录原生调用时不会把它解释为零次。这里只核算有记录的独立压缩调用，不推断服务端隐式压缩的额外费用。[接口说明](https://developers.openai.com/api/docs/guides/compaction)

每个请求的 `operations` 记录本次新增的共用操作，例如占位、截断、删除、观察摘要、工作段摘要、整体摘要和机械摘要。后续请求继续使用同一占位符时不会再次计作操作；超长重试只记录该次尝试的新操作。报告的 `mechanism_observation` 分别给出成功响应、实际渲染改写、具体操作、摘要用途和原生压缩证据；完成续跑不等于验证所有触发条件或任务效果。估计值不冒充实际账单；剪枝服务未返回用量时单独记录 `pruner_usage_unknown`。

## 真实执行（不是重放）

ctxpress 的方法除了在重放里算账，也可以真的跑：Agent 真的调用模型、真的执行工具，方法真的改写每次发给模型的历史，最后用官方评测器打分。

**做法：代理**

在 Agent 和模型 API 之间放一个本地代理（`ctxpress/live/proxy.py`，只用标准库）。

- Agent 每次请求都带着完整历史（Responses API 的 `input`）。代理把这段历史交给方法改写：
  - 换占位符、截断、只留结构；
  - 删掉整对调用和输出；
  - 把原文存进专用记忆。
- 改写后转发给真实地址，响应流式原样传回。
- 代理日志记录每次请求改了什么，以及 API 报告的真实 token 用量。
- 不记录请求头：请求头里带着登录凭证。

改写器（`ctxpress/live/rewrite.py`）只改输出的文字、删条目、在原位置插入方法写的摘要，其他条目逐字节保留：推理、指令、宿主自己的压缩产物都不动。

**Codex 怎么接**

在 `config.toml` 里写：

```toml
openai_base_url = "http://<代理地址>"

[features]
enable_request_compression = false
```

**SWE-Milestone 上的运行器**

`ctxpress eval` 使用已准备的本地任务镜像与官方评测脚本：
- 明确指定模型与 Codex 二进制目录，记录实际版本；
- 有限的后台并发队列，运行时代码固定，可在开发期间继续实验；
- Agent 的命令不能联网；
- auth 副本在容器内删除，并确认已经删除。

配置里的 `submit: true` 会在提交或调用/时间上限后用官方评测器打分。先生成计划、确认设置，再启动，详见 [docs/evaluation.md](docs/evaluation.md)。

```bash
ctxpress eval plan configs/live_mechanism.example.json --output runs/mechanism-plan.json
ctxpress eval run runs/mechanism-plan.json --directory runs/mechanism --background
```

**检验**

- 不加方法时，改写后的请求与原请求逐字节相同（`tests/test_rewrite.py`）。
- Complexity Trap(5) 在录制会话的真实请求序列上，替换的正好是最近 5 条之外的所有输出，与 Codex 里 Rust 实现的观察遮蔽一致。
- 真实运行：第一次请求的历史从约 60k token 降到 28k，API 报告的输入从 47k 降到 17k，模型正常返回，Agent 正常工作。

**哪些方法能真实运行**

| 已接入真实运行的机制 | 尚未实现的模型能力 |
|---|---|
| 不压缩、Codex auto-compact（使用宿主原生阈值） | ACM 作者 9B 训练模型：运行接口已接，模型尚未部署/验证 |
| ACM 工具机制适配（主动分段摘要、磁盘原文与模型查询；未加载训练策略） | |
| Complexity Trap | TokenPilot 的 LLM 打分 |
| 按 token 只留最近、Sliding window | |
| ReSum、ACON、AgentFold（统一模型摘要；已做本地机制检查，原文效果未复现） | |
| Pichay（缺页检测在真实运行中按"再次读同一文件"判断） | |
| ClawVM | |
| ARC（专用记忆：原文写进挂载目录，占位符给出路径，Agent 用普通读文件取回） | |
| TokenPilot 的入口截断和生命周期清理 | |
| SWE-Pruner（需配置作者剪枝服务地址） | |
| Claude Code、CliffCompaction 的清理部分（之后的整体摘要交给 Codex 自带的压缩） | |

方法在真实运行里需要整体摘要、又没有提供 `summarizer` 时，这一步会跳过，由宿主自己的压缩接管。日志里会记录 `summary_skipped`。

**使用 SWE-Pruner：**先在单独环境启动作者的剪枝服务，指定已下载的模型，然后选择方法：

```bash
swe-pruner --model-path /path/to/model --host 127.0.0.1 --port 8000
ctxpress use SWEPruner --args '{"url": "http://127.0.0.1:8000/prune"}'
codex
```

真实代理会在启动时检查剪枝服务是否已配置。未提供 `url` 或 Python 回调时会报配置错误；定义行与 oracle 近似保留用于重放。方法说明会提示 Agent 在命令后附加关注问题；这些说明也会随 `Composed` 和其他包装器传递。

## 真实评测

**统一评测入口：**固定 8 个 benchmark 家族使用同一 `ctxpress eval` 命令。`benchmarks --start-mode task_start` 查看从头支持范围，`tasks` 发现本地题目，`plan` 冻结可审查设置，`run --background` 后台派发，`status` 查看进度，`cancel` 停止任务并清理所属资源，`recover` 仅恢复已停止资源，`resume --background` 重试未完成任务，`report` 生成 JSON/HTML，`compare --reference` 按任务和重复编号比较八类的原生质量指标与费用。恢复保留已完成成绩和每次尝试的产物，运行所需数据、源码、依赖、镜像及认证仍需显式准备。见 [docs/evaluation.md](docs/evaluation.md)。

**Benchmark 接入：**`ctxpress eval benchmarks` 列出内置适配器及支持范围，`ctxpress eval tasks --benchmark swe-milestone --scripts /path/to/scripts --min-context 128000 --gradable` 查询本地任务 ID。评测配置可指定 `benchmark` 和 `tasks: ["n0-j435"]`，由目录展开并冻结上下文边界；方法、Codex 后端和 benchmark 分别选择，计划与报告保留 benchmark 身份。SWE-Milestone 的本地 Navidrome 录制边界执行和官方评分仍需准备已有脚本、历史、镜像及评分资源。SWE-bench（Verified/Lite/完整集）的从头 Codex 执行、补丁导出与独立官方评分调用代码已连接；Terminal-Bench/Science 的 Harbor CPU/NVIDIA GPU 执行和 verifier 调用代码已连接。各适配器的支持范围与验证边界见 [Benchmark 适配器](docs/benchmarks.md)，不由接口声明推断。

各家族使用同一配置结构，下面模板中的模型、任务 ID 和准备路径需替换为实际值。

| 家族 | 从头配置模板 |
|---|---|
| SWE-Milestone | [swe_milestone.example.json](configs/swe_milestone.example.json) |
| SWE-bench（Full/Verified/Lite） | [swe_bench.example.json](configs/swe_bench.example.json) |
| Terminal-Bench | [terminal_bench.example.json](configs/terminal_bench.example.json) |
| Terminal-Bench-Science | [terminal_science.example.json](configs/terminal_science.example.json) |
| DeepSWE | [deep_swe.example.json](configs/deep_swe.example.json) |
| SWE-bench Pro | [V1](configs/swe_pro_v1.example.json)、[V2](configs/swe_pro_v2.example.json) |
| SWE-PolyBench | [polybench.example.json](configs/polybench.example.json) |
| BigCodeBench | [bigcodebench.example.json](configs/bigcodebench.example.json) |

适配器目录中的 `real_run_verified=false` 是静态能力声明，单题验证不会把整个家族标为已验证。具体运行应检查官方原始评分、请求日志、冻结输入和清理证据；

目前 8 个家族各有一个原始任务的真实执行和官方评分证据；SWE-bench Verified 与 SWE-Milestone 上的正式方法比较正在进行，结果随论文发布。首题结果不代表完整 benchmark 已跑完，也不说明任何上下文方法的效果。

### 固定实验协议

固定方案在 [experiment.protocol.json](configs/experiment.protocol.json)：8 个家族、明确的主模型/反思模型、方法参数、任务预算和重复次数。首轮正式比较集中在 SWE-bench Verified 和 SWE-Milestone，回答已发表方法在统一真实宿主与计价下的质量和成本表现；AutoCostModel 单列为探索性候选。原始任务 ID 已固定（Verified 10 题，其余六类按 seed 各选 10 题，Milestone 为完整 Navidrome itinerary），官方数据 revision、原始文件和所选附件均已核对。可在仓库根目录只读盘点本地资源（数据默认在仓库旁的 `../data`，SWE-Milestone 作者代码由 `CTXPRESS_MILESTONE_AUTHOR_CODE` 指定）：

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

这些命令只读取本地输入并写出配置/来源哈希和资源缺项，不启动下载、容器或模型。配置未声明费率时费用保持未知，缺资源的计划不可启动。`--phase comparison` 加入协议中固定的 12 个基线及必需的 AutoCostModel 候选，验证策略/训练来源哈希和训练任务排除；其他六类也已冻结原始任务 ID，不隐式选择全部任务。生成器拒绝覆盖已有配置，重新生成应使用新输出路径。`artifact_root` 相对协议文件解析，候选策略写为绝对路径；移动配置目录不会改变所选策略。详见 [配置生成说明](docs/evaluation.md#从固定协议生成配置)。

基础链路通过后用 `--phase method-pilot` 生成首题上的 7 方法检查，直接继承正式基线参数，1 次重复、单 worker。正式比较须用 `configure --model-catalog <models.json>` 显式冻结宿主的模型目录，避免 Codex 回退到内置模型元数据、改变宿主提示词和工具接口。

### 多模型用量与费用

主模型、摘要和反思调用分别记录请求模型、返回模型及 API 用量。评测配置的 `prices.models` 可按模型声明 `input`、`cached`、`output` 费率和 `unit`；旧的平铺费率只用于计划主模型。报告列出各模型用量和按声明费率计算的费用；缺少模型身份、用量或任一实际调用模型的费率时，总费用显示未知。缓存写入和逐请求长上下文阈值已支持；服务档位需与声明费率相符，估算不含宿主等费用，不能当作完整账单。详见 [多模型记账](docs/evaluation.md#多模型用量与费用)。

各适配器的执行、评分和资源准备细节见 [docs/benchmarks.md](docs/benchmarks.md)，命令与配置见 [docs/evaluation.md](docs/evaluation.md)。

## 参数和它们的来源

每个参数都带来源（`ctxpress/core/params.py`）：

| 来源 | 含义 |
|---|---|
| measured | 本研究实测 |
| fitted | 在轨迹上拟合 |
| literature | 来自论文 |
| price | 价格表 |
| assumed | 还没测，取保守值 |

以下参数还是 assumed，用之前应先看 `configs/sensitivity.yaml` 的敏感性结果：

- 截断 / 结构化 / 段总结 / 专用记忆各形式下的找回概率 q；
- 没有文本时的覆盖率；
- 摘要再压缩的保留率；
- LLM / 小模型打分的费用。

## 局限

- 性能约束用的是代理指标，不是真实通过率。measured 口径的依据只有 §3 的 40 次续跑和 §5 的 4 个可解点。
- 6 条会话中有 4 条来自同一个仓库，并且只用了一个模型。
- 重放保持 Agent 原来的行为。Agent 因上下文变化而改变的部分，只通过找回概率和重新探索来近似。

## 许可证

[MIT](LICENSE)。`ctxpress/_vendor/tomllib` 随附的解析器保留其原有许可证。
