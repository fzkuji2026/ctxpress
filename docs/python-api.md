# Python 接口

不启动代理，直接在 Python 宿主里改写 Responses 请求。

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
