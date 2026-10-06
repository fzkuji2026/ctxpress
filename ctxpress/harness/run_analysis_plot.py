"""Figures for a unified run analysis (ctxpress.harness.run_analysis): one set per benchmark family.

- cost-<family>: mean API cost per job split into uncached input, cache read, cache write and output.
- cache-<family>: mean cache-miss cost per job by cause (method edit, host edit, provider).
- context-<family>: per method, median API input and cache read by request index, against the reference's input.
Only the family's usable jobs (run_analysis.usable_for) are drawn; nothing is imputed.
"""
from __future__ import annotations
import statistics
from pathlib import Path

# Reference categorical order (blue, orange, aqua, yellow) and neutral ink; validated for adjacent stacks.
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
NEUTRAL, INK, MUTED, GRID, SURFACE = "#8f8e88", "#0b0b0b", "#52514e", "#e6e5e0", "#fcfcfb"


def _style(plt):
    plt.rcParams.update({"figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "axes.edgecolor": GRID,
                         "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK,
                         "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.axisbelow": True,
                         "axes.spines.top": False, "axes.spines.right": False, "font.size": 9, "legend.frameon": False})


def _save(fig, output, name):
    for ext in ("png", "svg"):
        fig.savefig(output / f"{name}.{ext}", dpi=200, bbox_inches="tight")


def _methods(family, usable):
    return [m["method"] for m in family["methods"] if any(usable(j) and j["method"] == m["method"] for j in family["jobs"])]


def _mean(values):
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else 0.0


def plot(analysis, tables, output, usable_for):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _style(plt)
    output = Path(output)
    written = []
    for family in analysis["families"]:
        name = family["benchmark"]
        usable = usable_for(family)
        methods = _methods(family, usable)
        if not methods:
            continue
        jobs = {m: [j for j in family["jobs"] if j["method"] == m and usable(j)] for m in methods}
        y = list(range(len(methods)))[::-1]

        # Cost composition (stacked, one scale).
        fig, ax = plt.subplots(figsize=(7.2, 0.32 * len(methods) + 1.2))
        left = [0.0] * len(methods)
        labels = {"uncached": "uncached input", "cache_read": "cache read", "cache_write": "cache write", "output": "output"}
        for color, part in zip(SERIES, labels):
            values = [_mean(j["cost"]["parts"][part] for j in jobs[m]) for m in methods]
            ax.barh(y, values, left=left, height=0.62, color=color, edgecolor=SURFACE, linewidth=1, label=labels[part])
            left = [a + b for a, b in zip(left, values)]
        for yi, total, m in zip(y, left, methods):
            ax.text(total, yi, f"  ${total:.3f}  (n={len(jobs[m])})", va="center", fontsize=8, color=MUTED)
        ax.set_yticks(y, methods); ax.set_xlabel("mean API cost per job (USD, declared rates)")
        ax.set_xlim(0, max(left) * 1.3); ax.grid(axis="y", visible=False)
        ax.legend(ncol=4, loc="lower center", bbox_to_anchor=(0.5, 1.0), fontsize=8)
        ax.set_title(f"{name}: where the cost goes", loc="left", fontsize=10, pad=22)
        _save(fig, output, f"cost-{name}"); plt.close(fig); written.append(f"cost-{name}")

        # Cache-miss cost attribution.
        fig, ax = plt.subplots(figsize=(7.2, 0.32 * len(methods) + 1.2))
        left = [0.0] * len(methods)
        names = {"provider": "provider (no history edit)", "method": "method edited history", "host": "host edited history"}
        for color, cause in zip((SERIES[0], SERIES[1], SERIES[2]), ("provider", "method", "host")):
            values = [_mean(j["cache"][f"miss_cost_{cause}"] for j in jobs[m]) for m in methods]
            ax.barh(y, values, left=left, height=0.62, color=color, edgecolor=SURFACE, linewidth=1, label=names[cause])
            left = [a + b for a, b in zip(left, values)]
        for yi, total in zip(y, left):
            ax.text(total, yi, f"  ${total:.3f}", va="center", fontsize=8, color=MUTED)
        ax.set_yticks(y, methods); ax.set_xlabel("mean cache-miss cost per job (USD): missed prefix × (input − cached rate)")
        ax.set_xlim(0, (max(left) * 1.25) or 1); ax.grid(axis="y", visible=False)
        ax.legend(ncol=3, loc="lower center", bbox_to_anchor=(0.5, 1.0), fontsize=8)
        ax.set_title(f"{name}: cost of cache misses", loc="left", fontsize=10, pad=22)
        _save(fig, output, f"cache-{name}"); plt.close(fig); written.append(f"cache-{name}")

        # Context length by request index (small multiples, shared scale).
        series = {}
        for m in methods:
            rows = [[r for r in tables[name][j["job_id"]] if r["ok"]] for j in jobs[m]]
            length = max(len(r) for r in rows)
            series[m] = [(statistics.median([r[i]["input"] for r in rows if len(r) > i]),
                          statistics.median([r[i]["cache_read"] for r in rows if len(r) > i]),
                          sum(len(r) > i for r in rows)) for i in range(length)]
        reference = family.get("reference")
        cols = 4; rows_n = -(-len(methods) // cols)
        fig, axes = plt.subplots(rows_n, cols, figsize=(9, 2.0 * rows_n + 0.6), sharex=True, sharey=True, squeeze=False)
        top = max(v[0] for s in series.values() for v in s)
        for ax, m in zip(axes.flat, methods):
            points = series[m]
            x = range(1, len(points) + 1)
            if reference in series and m != reference:
                ref = series[reference]
                ax.plot(range(1, len(ref) + 1), [v[0] for v in ref], color=NEUTRAL, linewidth=1.2, label=f"{reference} input")
            ax.fill_between(x, [v[1] for v in points], color=SERIES[2], alpha=0.35, linewidth=0, label="cache read")
            ax.plot(x, [v[0] for v in points], color=SERIES[0], linewidth=2, label="API input")
            ax.set_title(m, fontsize=8.5, loc="left")
            ax.set_ylim(0, top * 1.05)
        for ax in list(axes.flat)[len(methods):]:
            ax.set_visible(False)
        handles, labels_ = axes.flat[1 if len(methods) > 1 else 0].get_legend_handles_labels()
        fig.legend(handles, labels_, ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.0), fontsize=8)
        fig.supxlabel("main request index", fontsize=8.5, color=MUTED)
        fig.supylabel("tokens per request (median over jobs)", fontsize=8.5, color=MUTED)
        fig.suptitle(f"{name}: context length and cache reads", x=0.01, y=1.04, ha="left", fontsize=10)
        fig.tight_layout(rect=(0, 0, 1, 0.97))
        _save(fig, output, f"context-{name}"); plt.close(fig); written.append(f"context-{name}")
    return written
