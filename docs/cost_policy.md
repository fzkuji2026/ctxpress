# 自动成本策略的离线筛选与部署

`ctxpress fit` 保存统计量，让调用方自己指定 λ。`ctxpress tune` 在明确选择的历史会话上筛选 λ，把方法设置、统计量、参数和逐折证据保存成单个 `ctxpress.cost-policy` JSON。在线加载不需要历史原文、NumPy、YAML、训练过程或额外服务。

```bash
ctxpress tune --manifest configs/train.yaml \
  --sessions historical-a historical-b historical-c \
  --lambdas 0 100000 1000000 5000000 --kind strict --workers 2 \
  --reference-limit 230000 \
  --args '{"lookahead": 8, "segments": true, "memory": "label", "hint": true}' \
  --params '{"cached": 0.1, "write": 1, "out": 8}' \
  --output ~/.ctxpress/cost-policy.json

ctxpress use AutoCostModel --args '{"policy": "/absolute/path/cost-policy.json"}'
codex
```

这里的名称、λ 网格和计价比仅展示接口；计价比是模拟假设，不表示所选模型的当前价格。正式实验必须另确认实际模型、二进制、上下文规模、参数、运行次数及费用范围。

## 选择过程

1. 校验历史会话至少两条、名称不同，每条至少两个请求。`--exclude` 排除评测会话；未知名称会报错。不要把将要评测的会话放进历史集合。
2. 每次留出一个会话，只用其余会话拟合再用曲线和覆盖率，在留出会话上重放每个 λ 与原生压缩基准。固定参数参与两者记账。并行按会话分配，不依赖完成顺序。
3. 选在所有留出会话上满足指定代理指标约束的最小 λ；不以留出会话的最小费用另选候选。没有合格候选时记录 `selected_lambda: null`、`fallback: true`。
4. 用全部所选历史会话重新拟合部署统计量，写入相同的策略文件。不会保存命令、工具输出或其他会话原文；输入只留下哈希。

`strict`、`harm`、`measured` 的定义与原重放约束相同（`core/metrics.py`），容差用固定参数里的 `eps`。这些指标包括预期静默缺失与需求文档缺失，属于模拟证据，没有执行新任务或证明真实成功率。

## 共用引擎与原生回退

`AutoCostModel` 是一个方法包装器。选到 λ 时构造原来的 `CostModel`，仍使用共用的占位符、截断、结构化、段摘要、历史摘要和专用记忆操作；未选到时构造 `CodexAutoCompact`。两条路径都把基准的压缩阈值传给实际 Codex 配置。

策略文件保留完整 `Params` 表与各参数的来源，包括价格比、缓存、找回、重新探索和窗口等假设。共用引擎默认采用这些固定参数；宿主明确指定不同参数或组合两个参数不同的策略时会报错。筛选时的窗口与原生回退阈值相同。

所有包装器、Python `ContextManager`、Codex 启动器及 `ctxpress eval` 使用同一方法类。评测计划记录策略文件哈希；容器挂载文件的副本。策略哈希改变、逐折约束证据矛盾或参数不完整时拒绝加载。更改方法选项、λ 网格、计价或找回假设后，应重新 `tune`，不能只修改已生成文件。

策略在 Python 中也可直接加载：

```python
from ctxpress import ContextManager
manager = ContextManager({"class": "AutoCostModel", "args": {"policy": "/absolute/path/cost-policy.json"}})
request = manager.apply({"model": model, "input": original_history}, session="task-1")
```

需要摘要的策略仍需要宿主的真实摘要能力。Codex 代理使用当前上游模型；直接 Python 调用要提供 `summarizer` 回调，否则摘要步骤跳过。添加额外包装器也会改变实际策略，原文件的筛选证据不能证明新组合的任务效果。

## 验证边界

本地机制检查覆盖逐折训练集合排除留出会话、串并行一致、固定参数与原引擎一致、原生回退、错误证据及哈希拒绝、宿主参数冲突、评测会话排除和外部文件挂载。这些不替代正式真实效果评测。
