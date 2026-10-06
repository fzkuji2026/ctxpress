# 真实执行：代理与改写

真实执行时，Agent 真的调用模型、真的执行工具，方法真的改写每次发给模型的历史，最后用官方评测器打分。

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

配置里的 `submit: true` 会在提交或调用/时间上限后用官方评测器打分。先生成计划、确认设置，再启动，详见 [docs/evaluation.md](evaluation.md)。

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
