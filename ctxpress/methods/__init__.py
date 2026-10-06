"""All methods. Build one from a config entry with `build({'class': ..., 'args': {...}}, train=...)`; entries nest,
so wrappers take other entries as arguments:
    {"class": "WithMemory", "args": {"inner": {"class": "ComplexityTrap", "args": {"n": 10}}}}
    {"class": "Composed", "args": {"methods": [{"class": "EntryTruncation", ...}, {"class": "Pichay"}]}}
`METHODS` lists every method with its paper, whether it needs a model of its own, whether it runs for real
(through ctxpress.live.proxy) and how it differs from the original implementation (`ctxpress list`)."""
from ctxpress.methods.base import Method, FRAMEWORK_KEYS, FRAMEWORK_TITLES
from ctxpress.methods.budget import BudgetSpec, with_budget, resolve_wrapper_budget
from ctxpress.methods.native import NoCompaction, CodexAutoCompact, ClaudeCode, SlidingWindow
from ctxpress.methods.masking import ComplexityTrap, KeepLastTokens
from ctxpress.methods.cliff import CliffCompaction
from ctxpress.methods.pichay import Pichay
from ctxpress.methods.clawvm import ClawVM
from ctxpress.methods.tokenpilot import TokenPilot
from ctxpress.methods.swepruner import SWEPruner
from ctxpress.methods.arc import ARC
from ctxpress.methods.summaries import AgentFold, ACON, ReSum, ComplexityTrapSummary, ComplexityTrapHybrid
from ctxpress.methods.agentdiet import AgentDiet
from ctxpress.methods.cwl import CWL
from ctxpress.methods.dtoc import DTOC
from ctxpress.methods.acm import ACM
from ctxpress.methods.workingview import WorkingView
from ctxpress.methods.legacy import ClearThenSummarize, PichayApprox, ClawVMApprox
from ctxpress.methods.cost_model import CostModel, ALL_TYPES
from ctxpress.methods.scored import ScoredMethod
from ctxpress.methods.wrappers import Composed, EntryTruncation, PinRequirements, WithMemory, Trigger
from ctxpress.methods.auto_cost import AutoCostModel

REGISTRY = {c.__name__: c for c in [NoCompaction, CodexAutoCompact, ClaudeCode, CliffCompaction, ClearThenSummarize, SlidingWindow,
                                    ComplexityTrap, KeepLastTokens, Pichay, PichayApprox, ClawVM, ClawVMApprox, TokenPilot, SWEPruner, ARC,
                                    ComplexityTrapSummary, ComplexityTrapHybrid, AgentDiet, CWL, DTOC, ACM, AgentFold, ACON, ReSum, WorkingView, CostModel, ScoredMethod,
                                    Composed, EntryTruncation, PinRequirements, WithMemory, Trigger, AutoCostModel]}
NEEDS_TRAINING = {"CostModel"}

# name: (paper / source, needs a model of its own, runs for real through the proxy, differences from the original)
METHODS = {
    "ACM": ("arXiv 2607.23809", True, True, "工具机制适配：Agent 主动分段摘要、磁盘归档和模型检索；须 store_dir；自定义提示词与 Responses 边界；未加载作者 9B 训练策略、未复现 BrowseComp-Plus；宿主修订历史会重建状态"),
    "NoCompaction": ("对照", False, True, "无"),
    "CodexAutoCompact": ("Codex CLI", True, True, "真实运行时就是 Codex 自带的压缩（--compact-limit），摘要由 Codex 的模型写；重放中摘要长度取 8k"),
    "ClaudeCode": ("Claude Code", True, True, "清理旧工具输出的部分与原机制一致；之后的 /compact 摘要交给宿主的压缩"),
    "CliffCompaction": ("arXiv 2609.26779", False, True, "触发、机械摘要、前缀替换、升级阶梯与图片计价已在录制范围内逐请求对照；新增 Responses HTTP400 超长重试及摘要缩短，8组合成阶梯与作者引擎逐步一致；错误识别更保守，未提供 strict/shadow 模式或其他 API 方言"),
    "ClearThenSummarize": ("本研究（此前误作 CliffCompaction）", True, True, "超过阈值清空全部工具输出，再超过 0.6 倍阈值时整体摘要；摘要交给宿主的压缩"),
    "SlidingWindow": ("经典做法", False, True, "删除整对调用和输出；固定保留指令和任务"),
    "ComplexityTrap": ("arXiv 2508.21433", False, True, "在作者发布的 500 条 SWE-agent 轨迹上，每一步发给模型的内容与原版逐条一致（107 万条观察，repro/complexity_trap_compare.py）；占位文字与原版相同"),
    "ComplexityTrapSummary": ("arXiv 2508.21433", True, True, "作者 SWE-agent 的 SummarizeEveryNTurns（静态检查点）：轮次划分、触发、摘要提示词原文、检查点格式与发送顺序按作者代码移植；摘要用当前 Agent 的模型；一轮按一条工具输出计，Codex 的并行调用各算一轮；后续用户消息不进入摘要；无摘要服务时保留历史，原版改为省略"),
    "ComplexityTrapHybrid": ("arXiv 2508.21433", True, True, "LLM-Summary（N = 43，M = 10）后接 Complexity Trap 遮蔽（W = 10），顺序同作者的 history_processors；摘要读未遮蔽的原文；差别同 ComplexityTrapSummary"),
    "AgentDiet": ("arXiv 2509.23586", True, True, "反思模块（traj_analyzer.py）按作者代码移植：窗口 a/b、阈值、步骤序列化、作者提示词、采纳条件、整步替换文本及已压缩步骤在之后提示中的原样展开；Responses 没有预填与停止符，改为在用户提示末尾要求同样的输出格式；token 用框架近似计数（原版 tiktoken gpt-4o）；一步按一轮模型输出及其工具结果计，用户消息不进入步骤；反思模型默认用 Agent 的模型，可用 reflect_model 指定（原文主实验为 GPT-5 mini）"),
    "CWL": ("arXiv 2606.11213", False, True, "淘汰策略（context-filter.ts filterContext）按作者代码移植：片段划分、依赖约束、按开始位置选最早候选、行动片段与探索片段的逐级剥离及每级之后的阈值检查、整段移除时保留用户消息；delimiter 工具的说明、参数和校验与原版一致，经 ctxpress mcp 提供；token 按作者 estimateTokens（每条消息 ceil(字符/4)，不含系统提示）；Codex shell 按命令类别对应原版的 search / bash / read；Code Mode 完整同步执行清单按组成操作分类，混合输出在最晚适用级删除，未知/截断/异步复合输出仍按整段处理；原版在探索片段中拒绝改文件，Codex 的 apply_patch 由宿主执行，代理无法拦截，未移植"),
    "DTOC": ("arXiv 2609.26121", False, True, "按作者 OpenCode 分支移植：tool_key 编号与登记时机（组装请求时登记，manage_context 只看到上一次组装的编号）、可见与隐藏两种 JSON 外壳、manage_context 的说明、参数与结果文本、累计节省 token；编号与可见状态在代理中维护，结果文本由代理按原版写入，工具进程只确认；时间戳取代理收到输出的时间（原版为工具结束时间）；/dtoc 指令的策略说明默认作为方法说明给出（原版仅在用户切换时发送），可用 strategy=false 关闭；原版报错的工具调用不加外壳，Codex 输出无报错状态，只对参数无效的 manage_context 调用如此处理"),
    "KeepLastTokens": ("本研究 §4 的对照组", False, True, "无"),
    "Pichay": ("arXiv 2603.09023", False, True, "分页器（按轮龄换出、缺页、内容未变则固定）逐行移植；Codex 请求先映射成 Anthropic 消息（读单个文件的命令视作 Read），再与作者的 MessageStore + 分页器逐请求比对（repro/pichay_compare.py）；模型主动释放、召回工具未移植"),
    "PichayApprox": ("本研究此前的近似", False, True, "按闲置步数换出（§8 用）"),
    "ClawVM": ("arXiv 2604.10352", False, True, "选择核心（两阶段选表示、效用公式、新近度）逐行移植，放进作者的 Tier-2 模拟器后 144 行结果完全一致（repro/clawvm_compare.py）；原文只在抽象页上评测，真实输出的页大小、需求由本项目定义；生命周期写回未实现"),
    "ClawVMApprox": ("本研究此前的近似", False, True, "按类型最低保真度的规则近似（§8 用）"),
    "TokenPilot": ("arXiv 2606.17016", True, True, "无公开代码，未与原实现比对；入口截断和按闲置步数清理可真实运行；原文用 LLM 估计剩余价值，这里用闲置步数代替"),
    "SWEPruner": ("arXiv 2601.16746", True, True, "接作者的剪枝服务（0.6B 模型）后与原版流程一致：Agent 给命令附关注问题，输出超过 500 字符时剪枝，回写格式相同；Codex 没有放问题的字段，改由方法说明要求在命令末尾加注释；无模型（重放）时用定义行近似"),
    "ARC": ("arXiv 2607.25066", False, True, "无公开代码，未与原实现比对；原文按编号取回；这里原文写进挂载目录，占位符给出路径，Agent 用普通读文件取回"),
    "AgentFold": ("arXiv 2510.24699", True, True, "可调用当前 Agent 的模型写段摘要和深度摘要；按规则分段触发，可配置摘要指南，未复现原文训练或模型自主折叠决策"),
    "ACON": ("arXiv 2510.00615", True, True, "可调用当前 Agent 的模型压缩观察和历史；两种操作可分别配置指南，默认通用续接提示词，未复现原文优化流程、指南或效果"),
    "ReSum": ("arXiv 2509.13313", True, True, "可按周期调用当前 Agent 的模型摘要；提示词依据 v3 附录 C 改写，只整理有依据的信息，不强制计划或缺口清单；保留周期触发近似，未复现原文提示词全文、续接模板、训练或效果"),
    "WorkingView": ("本项目此前的方法", True, False, "原实现在 adapters/codex（Rust）里；这里是规则近似"),
    "CostModel": ("本文", False, True, "可加载 ctxpress fit 生成的固定再用曲线接入真实代理；λ、价格、找回与重新探索参数仍需明确配置，真实任务效果尚未评测"),
    "AutoCostModel": ("本文", False, True, "加载 ctxpress tune 冻结的统计、参数及留一会话选出的 λ；无合格候选时退回原生 Codex 压缩；筛选是模拟证据，真实任务约束尚未验证"),
    "ScoredMethod": ("基类（参照 kvpress 的 ScorerPress）", False, True, "无"),
    "Composed": ("包装器", False, True, "无"), "EntryTruncation": ("包装器", False, True, "无"),
    "PinRequirements": ("包装器", False, True, "无"), "WithMemory": ("包装器", False, True, "无"),
    "Trigger": ("包装器", False, True, "无"),
}
WRAPPER_ARGS = {"inner", "methods"}


def build(entry, train=None, **extra):
    """entry: {'class': name, 'args': {...}, 'label': optional display name}. `train` = traces the method may
    estimate its parameters on (never the evaluated session)."""
    from ctxpress.core import metrics
    cls = REGISTRY[entry["class"]]
    args = dict(entry.get("args") or {}); args.update(extra)
    args = resolve_wrapper_budget(cls, args)
    if isinstance(args.get("harm"), str):
        args["harm"] = metrics.HARM[args["harm"]]
    if "inner" in args and isinstance(args["inner"], dict):
        args["inner"] = build(args["inner"], train=train)
    if "methods" in args:
        args["methods"] = [build(m, train=train) if isinstance(m, dict) else m for m in args["methods"]]
    if entry["class"] in NEEDS_TRAINING:
        if args.pop("item_model", False):
            from ctxpress.core.itemmodel import ReuseModel
            args["item_model"] = ReuseModel(train)
        m = cls(train, **args)
    else:
        m = cls(**args)
    if entry.get("label"):
        m.name = entry["label"]
    return m


def method_table(markdown=True):
    rows = []
    for name, cls in REGISTRY.items():
        spec = cls.budget_spec
        budget = f"{spec.scope}: {spec.unit} (default {spec.default})" if spec else (
            "inner method" if cls.forwards_budget else "—")
        rows.append((name, *METHODS.get(name, ("", False, False, "")), budget))
    head = "| 方法 | 来源 | 需要额外模型 | 能真实运行 | 与原实现的差别 | budget 范围与单位 |\n|---|---|---|---|---|---|"
    return head + "\n" + "\n".join(f"| {n} | {p} | {'是' if m else '否'} | {'是' if r else '否'} | {d} | {b} |" for n, p, m, r, d, b in rows)
