# 代码结构

包的分层、依赖方向，以及真实运行与重放共用同一套方法的方式。

```text
ctxpress/
  core/        共用引擎：条目和记账、轮次、宿主大小，以及占位符 / 截断 / 结构化 / 删除 / 段总结 / 整体摘要等操作；参数；文本操作；再用模型
  methods/     每个方法（或一族）一个文件，都只用 core 的操作写成；REGISTRY 和方法说明表在 __init__.py
  live/        真实运行：LiveContext（一段真实对话）、Rewriter（请求 ↔ 上下文条目）、代理、MCP 工具服务、用量计费、会话记录
  settings.py  已安装的配置（$CTXPRESS_HOME）
  hosts/       接入的 Agent：codex/（`ctxpress codex`、`ctxpress install codex`）、claude/（`ctxpress claude`）
  replay/      离线重放预筛（结果标为模拟）：读取轨迹、运行配置、约束指标、再用曲线
  harness/     评测的共用部分：jobs/（计划、后台队列、取消/恢复、固定协议、冻结输入）、runtime/（Docker 调用与资源归属、固定版本的 Codex、
               模型通信、评分服务）、results/（报告、验收、比较、过程评测）、checks/（交互、方法、机制检查，全方法冒烟测试）
  benchmarks/  每个 benchmark 家族一个包（milestone、swe、pro、polybench、bigcode、harbor、deepswe）：数据、Agent 会话和官方评分；
               注册表与共用的计划器在顶层；harbor、swe 同时是执行引擎，pro、deepswe 和 polybench、bigcode 分别接在上面
repro/         与各论文原版代码逐条对照的脚本（见 repro/README.md）
tests/         单元测试和对照测试
configs/       实验配置
```

依赖只朝一个方向：`core` → `methods` → `live` → `hosts` → 命令行，`settings` 不依赖任何模块、各层都可读取；`replay` 只用 `core`、`methods`；评测（`harness`、`benchmarks`）可以用框架的一切，框架不引用评测。评测内部：`harness/runtime` 不引用任何 benchmark；`jobs` 只通过注册表接触 benchmark，只为写最终报告引用 `results`；`checks` 不依赖 benchmark；一个家族只在执行引擎关系上引用另一个家族；命令行（`harness/cli.py`）在最上层。`tests/test_layering.py` 检查这些方向。

同一个方法类既用于真实运行（`live/`），也用于重放预筛（`replay/`）。框架缺少某个方法需要的概念时，补进 `core/`，不在方法里另写一套。

```python
from ctxpress import build, LiveContext, Rewriter
method = build({"class": "ComplexityTrap", "args": {"budget": 10}})
```
