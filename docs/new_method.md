# 添加一个新方法

一个方法就是一个类。写好、注册、加一项测试之后，它就同时可以：
- 在重放里做筛选；
- 通过代理接入真实的 Codex（`ctxpress codex --method 你的类`）；
- 与已有方法组合（`Composed`、`WithMemory` 等）。

## 1. 最简单的情况：只是打分不同

继承 `ScoredMethod`，只改打分函数。分数越低，越先被处理：

```python
from ctxpress.methods.scored import ScoredMethod

class LargestFirst(ScoredMethod):
    """Drop the largest tool outputs first."""
    paper = "arXiv xxxx.xxxxx"
    def score(self, sim, item, r):
        return -item["size"]
```

`budget`（工具输出最多保留多少 token）、`op`（placeholder / truncate / structure / delete）和 `protect`（最近几条不动）都是现成的参数。

`ScoredMethod` 也声明了 `budget_spec`，因此 `ctxpress list` 能说明预算的范围、单位和默认值。预算受保护条目和操作本身限制，可能无法达到；不能据此保证完整请求小于该值。

## 2. 一般情况：自己写规则

```python
from ctxpress.methods.base import Method

class MyMethod(Method):
    name = "My method"
    memory = None          # 'id' / 'label'：被移出的原文进专用记忆
    framework = dict(L1="...", L2="无", L3="无", cross="无", memory="无", decider="固定规则")

    def on_ingest(self, sim, item):      # 内容进入上下文时（例如入口截断）
        ...

    def step(self, sim, r):              # 每次请求前
        for s in sim.outputs():
            if r - s["last"] > 10 and s.get("form", "full") == "full":
                sim.to_placeholder([s])

    def on_fault(self, sim, item):       # Agent 回头又要用被移出的内容时
        ...
```

**可用的操作：**

| 操作 | 作用 |
|---|---|
| `sim.to_placeholder(items)` | 换成占位符 |
| `sim.truncate(item, budget)` | 截断到指定 token 数 |
| `sim.structure(item)` | 只留结构 |
| `sim.keep_text(item, text)` | 把方法或模型选择的文本写回，共用存档与恢复逻辑 |
| `sim.delete(items)` | 删除 |
| `sim.summarize(...)` | 整体摘要（需要模型） |
| `sim.summarize_segment(items)` | 段总结（需要模型） |

`sim.current_text(item)` 返回当前可见文本：全文条目用 `text`，截断或结构化条目用 `kept_text`，已换出的条目返回 `None`。`truncate` 和 `structure` 都基于当前表示继续处理，不从存档原文补回已删除的部分；只有大小严格减少时才提交，失败返回 `False` 且不改变条目或操作计数。已结构化的条目再次 `structure` 不重复压缩；这也避免无文本重放把相同保留率反复相乘。受保护或带媒体的条目不能做这两种纯文本变换。

`ScoredMethod` 可以继续处理入口截断、结构化之后的输出；大小分数使用当前大小。占位操作无法缩小时跳过，删除操作可移除旧存档引用及其调用。预算不足以容纳最近受保护的输出或占位开销时，保留这些内容，不能把预算理解成强制删除要求。

**条目里可用的信息：**

| 字段 | 内容 |
|---|---|
| `kind` | 类型：read、spec、search、command、edit |
| `res` | 涉及的文件 |
| `size` | 大小（token） |
| `born` | 进入上下文的请求序号 |
| `last` | 最后一次被用到的请求序号 |
| `form` | 当前形式 |
| `text` | 原文 |

## 3. 注册

跨请求的可变状态应在 `reset(sim)` 中初始化。新会话与宿主修订旧历史时，共用引擎会调用它；轮龄、缓存、已换出索引不能从旧历史继承。正常追加不会调用重置。声明式方法配置在一次 manager、代理或 Codex 启动内加载一次，随后为各会话复制；外部统计文件的修改在下次启动生效。显式提供的自定义工厂仍须返回独立的方法对象。

方法需要告诉 Agent 如何配合时，在 `instructions` 中提供说明，包装器会传递它，组合方法会去重。依赖外部服务的方法实现 `validate_live()`：真实代理在启动时调用，并递归检查包装器内部的方法；重放近似仍可独立运行。

方法需要模型摘要时，声明 `requires_summary = True`。Responses 代理会提供当前请求同一上游模型的服务，包装器传递这项声明。用 `sim.compress_output(item, budget)`、`sim.summarize_segment(items)` 或 `sim.summarize(...)`，不要在方法里另建 HTTP 客户端。Python 宿主可传入摘要回调；返回空文本或抛出异常时不修改历史。摘要只看到当前保留的文本，媒体和指令保留，实际 API 用量单独记账。使用通用提示词与原论文不同，必须写进方法说明。

共用 Responses 服务同时提供原任务、后续用户修正和新增系统、开发者约束，均作为数据。进行中、排队、失败或不完整的 JSON 不能提交；SSE 需要完成事件。加密宿主 `compaction` 条目属于受保护状态，不参与文本摘要和删除。操作日志的 `operations` 是每次请求或重试新增的计数，不能将继续使用某种表示误计为重复压缩；计数与渲染改写、HTTP 响应及实际用量分别观察。

这些共用操作均接受可选的 `guidance` 字符串，由当前操作传入，不能把多个组合方法的指南合并成一个全局提示词。Responses 服务用它替换通用摘要要求，同时保留将历史视为数据的边界说明、用途和目标 token 数。Python 服务实现 `summarize_with_guidance(items, purpose, budget, guidance)` 可接收它；旧回调签名继续有效，但不接收方法指南，会累计 `summary_guidance_skipped`。一次调用失败不会通过其他回调接口重试。重放接受这一参数但不模拟提示词的语义、质量或额外 token 开销。

需要永久保留某些条目时，用 `sim.protect(items)`，它同时保护对应的工具调用。共用的截断、结构化、占位、删除和摘要操作都遵守此标记；`outputs()` 与 `candidates()` 不返回这些条目。方法直接遍历 `sim.ctx` 时应跳过 `protected`，避免把它们计入可压缩的收益。`PinRequirements` 用这项能力在入口处保护需求文档，不能等压缩后再恢复。

需要宿主原生能力时，用 `codex_config` 声明本次启动需要的 Codex 配置。启动器把它写入独立配置档，包装器传递这些设置，组合时拒绝冲突。`CodexAutoCompact` 的原生阈值走这个接口，不能只在模拟器里调整而遗漏真实宿主。

需要在上游拒绝超长请求后调整当前历史时，实现 `on_overflow(sim, request)`，并声明 `max_overflow_retries`（默认 0，允许 0–16）。返回 `True` 表示已经修改当前表示、可以重试；无法修改时返回 `False`。这与 `on_fault` 的内容找回不同。代理只对明确的 Responses HTTP 400 超长错误调用它，不重试鉴权、限流、其他错误或已经开始输出的 SSE 失败。方法不创建 HTTP 客户端、不重新摄入自己的摘要；包装器传递能力，组合依次找出能修改历史的一个组件。若同一会话已有更新请求，旧请求的重试票据失效。方法应在 `step` 或 `reset` 中重置每个请求的升级状态，并保证失败时不提交半成品。

重试仍使用当前请求的模型、参数和认证，日志逐次记录网络尝试及其用量。被拒绝的请求没报告用量时，费用保持未知。`CliffCompaction` 用这项接口提供四级处理：强制折叠、最近 1 轮、精简摘要内容、仅保留已有摘要中最新的完整部分。最后一级只改已有摘要，保留指令和尾部。

方法有明确保留量时，构造函数接受 `budget`，并声明 `BudgetSpec(parameter, unit, scope, default, minimum=0)`。`unit` 要明确为 tokens、outputs 或 segments 等，`scope` 说明对单条输出、工具输出总量还是工作段生效。需要兼容旧参数时，用 `budget_spec.resolve(budget, legacy)` 保留默认值并拒绝冲突，例如：

```python
from ctxpress import BudgetSpec

class RecentOutputs(Method):
    budget_spec = BudgetSpec("n", "outputs", "recent full tool outputs", 10)

    def __init__(self, n=None, *, budget=None):
        self.n = self.budget = self.budget_spec.resolve(budget, n)
```

轮龄、周期、触发阈值和已冻结的策略不能冒充保留预算，没有这项语义时保持 `budget_spec = None`。透明包装器声明 `forwards_budget = True`，构建器会把预算传给声明式 `inner`；`PinRequirements`、`WithMemory`、`Trigger` 已采用这项能力。直接构造包装器时先给内层方法设置预算。`EntryTruncation` 的预算只管入口，`Composed` 的各方法分别设置。

离线策略需要固定决策参数时，声明 `parameters` 为一个 `Params`。共用引擎在默认宿主配置下采用它；宿主明确提供不同参数时会报冲突，组合方法也拒绝不同的固定参数。包装器传递这项能力。`AutoCostModel` 使用它保留 `tune` 时的计价、找回和窗口假设，不能只带上 λ 却悄悄换掉筛选条件。

在 `ctxpress/methods/__init__.py` 里：
- 把类加进 `REGISTRY`；
- 在 `METHODS` 里登记一行：来源、要不要额外模型、能不能真实运行、与原实现的差别。

`ctxpress list` 会自动列出它。

## 4. 测试

在 `tests/` 里至少加一项：用 `LiveContext` 喂几条内容，检查方法改了该改的、没动不该动的。复现别人的方法时：
- 如果有原实现，与原实现逐条比对：在 `repro/` 下写一个脚本，把同样的输入交给作者的代码和 ctxpress，逐条比较输出（参考 `repro/cliff_compare.py`）；框架缺的概念补进 `ctxpress/core/`，不要把原版代码搬进方法里；
- 如果没有原实现，对照论文的描述写测试，并在 `METHODS` 里写明差别。

## 5. 在真实 Codex 上用

```bash
ctxpress codex --method MyMethod --args '{"...": 1}' -- exec "你的任务"
```

在 SWE-Milestone 上从冻结边界做到提交，并用官方评测器打分：

```bash
ctxpress eval plan my-evaluation.json --output runs/my-plan.json
ctxpress eval run runs/my-plan.json --directory runs/my-run --background
```

配置结构见 [evaluation.md](evaluation.md)。模型和 Codex 二进制目录要明确填写；真实实验设置确认后再启动。

## 宿主兼容与机制检查

新增方法还应检查摘要/删除后的工具目录、宿主状态和媒体保留，以及追加历史、一次性恢复长历史和宿主修订历史。渲染器会拒绝违反这些契约的请求；方法不能绕过保护直接清空 `sim.ctx`。`ctxpress check-method` 为有网络隔离保证的方法提供合成 fixture，外部服务需要另写显式测试。合成触发和真实模型效果分开报告。

ACM 是复用共用模型服务与会话归档的实例，适配范围见 [ACM 文档](acm.md)。
