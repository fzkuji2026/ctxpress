# 重放预筛与成本模型

重放模拟器在录制好的 Agent 轨迹上按请求重放，让方法改写上下文并统一计算费用、缺失与找回。它只用于真实实验之前的快速筛选，结果都标为模拟；方法效果以[真实执行](live.md)和[官方评测](evaluation.md)为准。文中的 §N 指本研究预实验报告的章节，报告随论文公开。

## 重放做什么

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

## 成本模型

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

每个历史会话分别留出作筛选，其余会话拟合统计量。选择在所有留出会话上满足模拟约束的最小 λ；没有候选通过则明确退回 `CodexAutoCompact`。最终统计量只在所选历史会话上重新拟合，策略文件同时保存方法选项、逐折证据、参数和哈希。`--params` 可以显式指定计价比与找回假设，`--reference-limit` 指定原生回退阈值；筛选窗口与这个阈值保持一致。加载策略时复用这些参数，拒绝与宿主显式参数冲突。见 [docs/cost_policy.md](cost_policy.md)。

`tune` 是离线模拟筛选，没有真实任务质量保证。上线仍需真实执行和官方评测；不能把历史会话的模拟约束通过率当作新任务的性能保证。

只打开占位符和摘要、关闭记忆，并用 §8 的记账（`spec_recovery=False`、`dedupe_recovery=False`、`recompress_retention=1.0`）时，它与 §8 的模型逐位相同（`tests/test_repro.py`）。默认用修正后的记账：摘要后重读当前需求文档要计费，找回的副本替换原件，每轮摘要保留率取实测的 0.975（实验页 §9.4）。

## 运行重放

```bash
pip install -e '.[sim,itemmodel]'
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

## 加一条会话或一个宿主

- **加会话：**在 `configs/sessions.yaml` 里加一条，`harness: codex` 或 `claude-code`。
- **加宿主：**在 `ctxpress/replay/loaders/` 里写一个 `load(path, ...)`，返回与 `core/trace.py` 说明相同的结构。

## 局限

- 性能约束用的是代理指标，不是真实通过率。measured 口径的依据只有 §3 的 40 次续跑和 §5 的 4 个可解点。
- 6 条会话中有 4 条来自同一个仓库，并且只用了一个模型。
- 重放保持 Agent 原来的行为。Agent 因上下文变化而改变的部分，只通过找回概率和重新探索来近似。
