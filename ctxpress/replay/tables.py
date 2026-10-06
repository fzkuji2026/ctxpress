"""The framework table (experiment.zh.html table 23) generated from the methods' own declarations, so the
documentation and the code cannot drift apart."""
from __future__ import annotations
from ctxpress.methods import FRAMEWORK_KEYS, FRAMEWORK_TITLES
import ctxpress.methods as M

DEFAULTS = [M.CodexAutoCompact(), M.ClaudeCode(), M.CliffCompaction(), M.SlidingWindow(), M.ComplexityTrap(),
            M.KeepLastTokens(), M.Pichay(), M.ClawVM(), M.TokenPilot(), M.SWEPruner(), M.ARC(), M.AgentFold(),
            M.ACON(), M.ReSum(), M.WorkingView()]


def framework_table(methods=None, html=False):
    methods = methods or DEFAULTS
    cols = ["方法", "来源"] + [FRAMEWORK_TITLES[k] for k in FRAMEWORK_KEYS]
    rows = [[m.name, m.source] + [m.framework.get(k, "无") for k in FRAMEWORK_KEYS] for m in methods]
    rows.append(["本文：成本模型", "本文", "成本决定：每类内容在占位符 / 截断 / 结构化压缩中选", "可选：成本决定何时折叠已完成的段",
                 "成本决定何时整体摘要", "需求文档、正在用的自动保留；可选压缩时提示重读", "可选：占位符带编号 / 编号和标签", "成本模型 + 性能约束（λ）"])
    if html:
        h = "<table>\n<thead><tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr></thead>\n<tbody>\n"
        h += "".join("<tr>" + "".join(f"<td>{v}</td>" for v in r) + "</tr>\n" for r in rows)
        return h + "</tbody>\n</table>"
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)
