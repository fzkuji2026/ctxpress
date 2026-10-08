# 在 Codex 和 Claude Code 里使用

ctxpress 以本地代理的方式接入现成的编码 Agent：Agent 的每次模型请求先经过所选方法改写，再发往真实 API。本页说明 Codex、Claude Code 及其他 Responses 宿主的接入方式、保证和检查命令。Python 程序直接调用见 [Python 接口](python-api.md)。

## 固定的宿主版本

评测基线固定在各宿主的稳定大版本上，先用宿主出厂设置（`HostDefault`）测基线，再在同一版本上比较其他方法。宿主发布新的大版本时做一次适配：重跑兼容检查（`doctor codex`、离线工具检查）、重新测默认压缩阈值、确认模型目录可解析，再重跑基线。

| 宿主 | 固定版本 | 来源 | 已核对 |
|---|---|---|---|
| Codex CLI | `0.161.0`（稳定版，2026-10-07） | npm `@openai/codex@0.161.0-linux-x64`，sha512 校验通过 | 版本与参数能力、三种离线启动、6 项 Agent 工具检查（CWL / DTOC，含恢复与隐藏控制）、code-mode 配套进程握手、`gpt-6.1-sol` 冻结模型目录解析（medium / xhigh） |
| Claude Code | `2.1.285`（stable 通道） | npm `@anthropic-ai/claude-code@2.1.285` | 仅登记版本，尚未下载和评测 |

Codex 0.161 的发布包把沙箱用的 bubblewrap 放在 `codex-resources/bwrap`；bin 目录需与发布包结构一致（`codex`、`codex-code-mode-host`、`codex-resources/bwrap`），三个文件都写入计划哈希。系统没有 `bwrap` 时缺少它会让 Agent 的普通命令失败。

**默认压缩阈值（实测）**：Codex 0.161.0 + `gpt-6.1-sol`（窗口 272k）在上报用量 244,100 时不压缩、246,100 时发出压缩请求，与窗口的 90%（244,800）一致。测法：隔离的 `CODEX_HOME`、假密钥、本地回环 Responses 服务逐轮增加上报用量，不设任何压缩阈值。`HostDefault` 不覆盖它，真实运行时以日志里的压缩请求为准。复测命令：`python -m ctxpress.hosts.codex.threshold --codex-bin <bin/codex> --model gpt-6.1-sol --model-catalog <models.json>`。

## Codex

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

**当前 CLI 的离线接入检查：**已用 `codex-cli 0.159.0-alpha.12.1` 对本地假 API 检查默认配置、已有配置档和自定义 Responses 提供商的请求接入、模型设置、方法说明、用量解析与配置档清理。没有真实任务、真实模型费用或登录验证；不替代真实任务上的效果验证。配置档布局依据 [OpenAI 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。

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

## 其他宿主


- 用 `ctxpress serve` 单独启动代理，把宿主的 API 地址指向它即可。
- 支持 OpenAI Responses API、Anthropic Messages（Claude Code，见下文）和 Chat Completions（vLLM、LiteLLM 等 OpenAI 兼容服务）。
- Chat Completions：带 `tools` 的 Agent 请求按方法改写，没有工具的单次调用（摘要、评分）原样转发并单独计费。同一条助手消息的文本、`reasoning_content` 和工具调用组装回一条消息；每个工具调用的结果紧跟在它后面，方法写的摘要不会插进未答完的工具调用之间；方法说明追加到第一条 system 消息（很多模型的聊天模板只接受开头的 system）。用量读取 `prompt_tokens`、`prompt_tokens_details.cached_tokens` 和 `completion_tokens`，流式响应需要 `stream_options.include_usage`。
- 有的宿主会给最新一条工具结果附加一个每轮变化的信号（ACM 作者的 Agent 在最后一条工具结果末尾加 `[CURRENT CONTEXT TOKEN: N]`，下一轮从上一条去掉）。`serve(..., chat_volatile=<正则>)` 把这种后缀排除在方法看到的历史之外，所以它的移动不算宿主修订历史；方法改写那条输出时再把后缀接回去。`chat_session` 为专用代理固定会话键，宿主压缩改写首条消息时也不会被当成新会话。
- 代理可以代管上游凭据：`serve(..., upstream_key=...)` 会替换客户端的 `Authorization`，客户端只拿到占位符。BrowseComp-Plus 家族用这种方式运行作者的 Agent（见 [ACM 接入](acm.md)）。

**方法可以组合：**

| 包装器 | 作用 |
|---|---|
| `Composed` | 串联多个方法 |
| `EntryTruncation` | 入口处截断 |
| `PinRequirements` | 需求文档固定保留 |
| `WithMemory` | 专用记忆 |
| `Trigger` | 超过阈值才动作 |

组合方法按顺序看到前一步保留的内容。例如 `EntryTruncation(ScoredMethod(budget=16000), budget=2000)` 先截断单条输出，再按总预算处理旧输出。共用截断与结构化操作不会重新读出存档原文；无法继续缩小时保持当前表示不变，原文仍可通过存档找回。

**新方法：**大多数方法只差一个打分函数，继承 `ScoredMethod` 即可，见 [docs/new_method.md](new_method.md)。

Responses 上游明确返回 HTTP 400 超长错误时，声明了能力的方法可通过共用代理调整当前历史并重试。`CliffCompaction` 提供作者的升级阶梯，最多重试 4 次；不重新摄入生成的摘要。其他错误原样返回，较新会话请求会使旧重试失效。每次上游尝试单独计入日志；失败请求缺少用量时，不假定其费用为零。这一路径通过本地假 API 和合成作者代码对照，真实任务行为仍待验证。

`NoCompaction` 的宿主语义已在固定 CLI `0.159.0-alpha.12.1`、同一模型目录和本地假 API 上验证：240k 合成输入用量处继续，270k 处在摘要发往上游前停止；没有扩大模型窗口。API key 自定义提供商与假 ChatGPT 登录两条路径均验证。原生压缩除旧 `/responses/compact` 端点外，也可能通过 `/responses` 的 `request_kind=compaction` 元数据发送；两者均单独记录且不再经过方法改写。本地拒绝记为 `native_compaction_blocked`，不计作已发送的 API 调用。旧快照没有这项保证，旧日志的零压缩计数不能证明未发生压缩；统一分析的 `native_compaction_metadata_coverage` 明确区分完整识别与历史未知。这是离线检查，不构成真实长任务效果证明。

## Claude Code

```bash
ctxpress claude --method ComplexityTrap --budget 10            # 常规交互对话，参数照常写在 -- 之后
ctxpress claude --method DTOC -- --continue
```

`ctxpress claude` 在本机回环启动代理，只给这次启动的 Claude Code 设置 `ANTHROPIC_BASE_URL`，上游取原有的 `ANTHROPIC_BASE_URL` 或 `https://api.anthropic.com`。登录方式（API key 或订阅登录）不变，代理转发请求头、不记录请求头，也不写入或修改 Claude Code 的设置文件。方法的 Agent 工具与 `ctxpress_retrieve`、`ctxpress_status` 由 `ctxpress mcp` 提供（`--mcp-config`），并用 `--allowedTools` 预先放行；`--no-tools` 不提供这些工具。Claude Code 自己的自动压缩保持用户设置。

Anthropic Messages 把工具调用、工具结果和思考放在消息内的块里。代理把每个块展开成 Codex 用的同一种条目，套用同一个方法，再组装回合法的请求：角色交替、每个 `tool_use` 的结果在下一条用户消息里且排在最前、最后一轮助手消息的 thinking 原样保留；`cache_control` 取自当前请求，不参与历史比对（Claude Code 每轮移动缓存断点）。没有工具的旁路请求原样转发；子 Agent 和不同模型的对话按首条消息与模型分开。Claude Code 每轮结束后用完整历史加一句指令请求"下一句建议"，回答随后丢弃；这类一次性分叉之后，下一轮从分叉前的方法状态继续，不算历史改写（Codex 同样适用）。用量按 Anthropic 流计入：输入 = 未缓存 + 缓存读取 + 缓存写入，分别记录。

常规交互验证：`python -m ctxpress.harness.checks.interactive --host claude --bin <claude>`（或 `--host codex --bin <codex>`）在 tmux 里启动真实 TUI，键入两轮对话，假模型在第一轮调用 `ctxpress_status`；第二轮请求必须带着前一轮的问答、被 DTOC 改写的工具输出和方法说明，整个会话不能出现历史重置，退出后进程正常结束。Claude Code 2.1.59 与 Codex 0.159.0-alpha.12.1 在 WSL 上均通过；配置放在临时 `CLAUDE_CONFIG_DIR` / `CODEX_HOME`，凭据为假值，不是真实模型证据。
