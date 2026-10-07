# ACM：工具机制适配与作者训练模型

来源为 [ACM: Agentic Context Management for Long-Horizon Tasks](https://github.com/lixiaochuan2020/agentic-context-management)，参考提交 `f06f90e728af8580a4515812425c1620144145a2`。作者公开了 Qwen3.5-9B 的训练后权重，使用权重不要求重新训练教师模型。

## 在现有宿主使用工具机制

```bash
ctxpress codex --method ACM --args '{"summary_model":"你的摘要模型"}'
ctxpress check-method ACM --turns 12 --require-trigger --output acm-check.json
```

省略 `summary_model` 时复用当前模型。Codex 启动器为每次运行创建独立存档目录；Python 接口须提供 `store_dir`。没有持久化目录时，压缩返回错误并保留历史，不假称原文已经保存。

Agent 独立调用 `manage_context()`：将自上个成功边界以来、当前控制调用之前的可管理历史写入会话目录，取得摘要后删除该段，摘要作为控制工具的返回值给模型。原任务、系统/开发者指令、工具目录、媒体及跨边界调用配对受保护，之前的摘要保留。`query_memory(summary_id, query)` 校验存档哈希，再通过统一模型服务检索原文。摘要和查询费用均进入正式账单的 `summary` 调用方。

失败时不删除历史；重复发送完整历史不重复执行同一控制调用。不同会话的文件和编号隔离。归档采用内容哈希文件名，历史修订会重建活动边界，但同一会话的旧归档编号继续可查询，之前的文件不被覆盖。查询不接受任意路径。

**这不是训练后的 ACM Agent。** 这是在 Codex/Responses 上的机制适配，宿主模型仍由使用者选择。提示词经过改写；分段以独立控制调用边界表示；不复现作者 token 提示、摘要解析重试及原始模型的训练策略。宿主修订历史时可能重新生成摘要。组合方法提前改写的历史不承诺与作者原始消息相同。单元测试使用确定性的假摘要，不声称论文数值复现。

## 作者训练后的 Agent：`browsecomp-plus` 评测家族

训练过的 ACM 模型离不开作者自己的 Agent 外壳：提示词、工具（`search`、`get_document`、`manage_context`、`query_memory`）和 token 提示都和训练时绑定。所以 ctxpress 不把它塞进 Codex，而是原样运行作者的检索循环，把它的模型调用接到 ctxpress 代理上（Chat Completions）。这样它走统一的计划、记账、取消/恢复、评分和验收，结果可以和其他配置放在同一张表里。

一个计划对应一个 Agent 模型；比较训练效果时用同一题目集编两份计划：

| 计划 | `model` | `run.use_memory_tool` | `methods` | 回答的问题 |
|---|---|---|---|---|
| ACM 训练模型 | `openai/<ACM 检查点的服务名>` | true | `NoCompaction` | 论文方法本身 |
| 原始模型 + ACM 工具 | `openai/<Qwen3.5-9B 的服务名>` | true | `NoCompaction` | 训练带来多少 |
| 原始模型 + ctxpress 方法 | `openai/<Qwen3.5-9B 的服务名>` | false | `ComplexityTrap`、`KeepLastTokens` 等 | 同一 Agent 上与规则方法比较 |

方法栏只能用没有自有 Agent 工具的方法（作者循环只提供它的工具）。配置示例（路径为已准备的本地文件，计划不下载任何东西）：

```json
{
  "schema": "ctxpress.eval", "version": 1, "scope": "benchmark",
  "benchmark": "browsecomp-plus", "start_mode": "task_start", "backend": "acm_author",
  "model": "openai/qwen3.5-9b-bcp-opd",
  "environment": {
    "checkout": "/prepared/agentic-context-management",
    "python": "/prepared/agentic-context-management/.venv/bin/python",
    "data": "/prepared/agentic-context-management/data/bcp_eval_150.json",
    "ground_truth": "/prepared/agentic-context-management/data/BrowseComp-Plus/data/browsecomp_plus_decrypted.jsonl",
    "qrels": "/prepared/agentic-context-management/data/BrowseComp-Plus/topics-qrels/qrel_evidence.txt",
    "index": "/prepared/agentic-context-management/data/BrowseComp-Plus/indexes/bm25",
    "agent_upstream": "http://127.0.0.1:8000/v1",
    "summarizer": {"model": "gpt-5.4-mini", "upstream": "https://api.openai.com/v1"},
    "grader": {"model": "gpt-5", "upstream": "https://api.openai.com/v1"}
  },
  "tasks": ["..."], "methods": [{"class": "NoCompaction"}],
  "run": {"use_memory_tool": true, "timeout": 3600, "context_window": 131072}
}
```

```bash
ctxpress eval plan acm.json --output acm.plan.json
CTXPRESS_SUMMARIZER_API_KEY=... CTXPRESS_GRADER_API_KEY=... ctxpress eval run acm.plan.json --directory runs/acm --background
```

每个作业启动三个本地代理：Agent（应用计划里的方法）、摘要和评分（原样转发），各写一份日志。作者进程只拿到占位密钥，真实密钥由代理从运行环境取得（本地 vLLM 一般不需要 `CTXPRESS_AGENT_API_KEY`），不进入计划、日志或结果。作者进程在独立进程组中运行，超时或取消时整组终止。评分调用作者的 `evaluate_browsecomp_plus`，评判模型的用量单独记录，不计入方法费用。

`ctxpress acm-author` 仍只做固定源码与输入的核对，不启动作者进程；运行作者模型请用上面的评测家族。本机验证（2026-10-07，RTX 3080 16 GB）：作者代码 + ACM iter3 权重经 vLLM 0.31 以 FP8 部署（与作者相同的 `--tool-call-parser qwen3_xml`，32k 窗口），在自造的小语料和接口相同的替代 BM25 检索上跑通统一评测：模型会检索、读文档、在上下文接近上限时调用 `manage_context`；ctxpress 记下每次请求的用量、vLLM 前缀缓存命中和摘要旁路调用，作者评分正常读回。FP8、较小窗口和替代语料下的结果不是论文复现，官方 BrowseComp-Plus 数据需要先在 Hugging Face 接受条款。替身测试见 `tests/test_browsecomp.py`。

## 离线源码对照

```bash
python repro/acm_compare.py --original /prepared/agentic-context-management --output runs/repro/acm.json
```

这只是测试 oracle，运行 ACM 的路径仍是框架的 `Rewriter` 和注册方法。脚本先校验作者文件哈希，再以相同假摘要比较分段边界、保留的上下文及归档。已核对 500 次规范化上下文和 127 份归档；工具信封与两种明确的空范围错误文本进行了归一化，不声称逐字节等同整个宿主请求。真实摘要、query_memory、并行调用、重启、token 提示和 9B 策略不在这份对照的覆盖范围。
