# ACM 的两种接入

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

## 使用作者训练后的 Agent

`ctxpress acm-author` 为固定版本的作者 `src.run` 生成独立启动记录，保留作者 Agent 循环、工具和任务协议，支持连接预先部署的 OpenAI 兼容模型服务。它不通过 Codex 运行，也不在现有 8 类 benchmark 注册表增加 BrowseComp-Plus。

需提前准备：干净的固定版本作者仓库、装好作者依赖的 Python、作者配置、BrowseComp-Plus 题目与 BM25 索引/检索依赖、已服务的 ACM checkpoint，以及作者配置要求的摘要与评分服务。模型服务名称不是权重身份的证明，输出里始终单独说明这一限制。

```bash
ctxpress acm-author \
  --checkout /prepared/agentic-context-management \
  --python /prepared/acm-env/bin/python3.12 \
  --model openai/acm-qwen3.5-9b-opd-iter3 \
  --api-base http://127.0.0.1:8000/v1 \
  --data /prepared/bcp_eval_150.json \
  --config /prepared/acm-eval.yaml \
  --index /prepared/bm25-index \
  --limit 1 --timeout 1800 --output runs/acm-author-prepare
```

默认只准备 `launch.json`：核对源码版本与输入哈希，不安装依赖、不下载、不启动模型或训练。实际执行需另选一个新输出目录，并在同一命令上加 `--execute`。执行目前要求 Linux，超时或取消会终止所属进程组，不隐式重试。保留作者日志和结果；退出码 0 仅表示作者进程正常退出，不自动认定官方成绩有效，也不计入现有 Codex 正式结果。

本次已核对实际作者提交及入口参数，并用本地替身验证启动准备、输入变化拒绝和超时清理。尚未部署 9B 权重、下载检索索引或运行真实 ACM 成绩。运行环境和索引内容尚未整体冻结。
