# ACM 的统一接入与作者参考核对

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

## 作者训练后的 Agent：尚未接入正式评测

作者的 Qwen3.5-9B 策略还没有通过 ctxpress 的统一执行、记账、取消/恢复和评分链路验证。不能把机制适配的通过当成训练模型的通过，也不通过启动另一个作者进程补一个孤立的“baseline”。

历史 `ctxpress acm-author` 现在仅保留固定源码与输入的参考清单核对；`--execute` 和直接执行函数均明确拒绝独立运行。准备清单中的作者命令用于接口核对，`execution_supported` 为 false，不能作为评测计划使用。正常 baseline 入口仍为 `ctxpress codex/claude --method ACM` 和 `ctxpress eval`。将来运行作者训练模型时，需要先接入共享运行和评分链路，另行准备权重、依赖与数据。

## 离线源码对照

```bash
python repro/acm_compare.py --original /prepared/agentic-context-management --output runs/repro/acm.json
```

这只是测试 oracle，运行 ACM 的路径仍是框架的 `Rewriter` 和注册方法。脚本先校验作者文件哈希，再以相同假摘要比较分段边界、保留的上下文及归档。已核对 500 次规范化上下文和 127 份归档；工具信封与两种明确的空范围错误文本进行了归一化，不声称逐字节等同整个宿主请求。真实摘要、query_memory、并行调用、重启、token 提示和 9B 策略不在这份对照的覆盖范围。
