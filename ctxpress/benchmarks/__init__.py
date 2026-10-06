"""Built-in benchmark adapters, independent of context-management methods."""
from ctxpress.benchmarks.milestone.adapter import SWEMilestone
from ctxpress.benchmarks.swe.adapter import SWEBench, SWEBenchVerified, SWEBenchLite
from ctxpress.benchmarks.harbor.terminal_bench import TerminalBench
from ctxpress.benchmarks.harbor.terminal_science import TerminalScience
from ctxpress.benchmarks.deepswe.adapter import DeepSWE
from ctxpress.benchmarks.polybench.adapter import PolyBench
from ctxpress.benchmarks.pro.adapter import SWEPro
from ctxpress.benchmarks.bigcode.adapter import BigCodeBench

DEFAULT = "swe-milestone"
REGISTRY = {DEFAULT: SWEMilestone, 'swe-bench':SWEBench, 'swe-bench-verified':SWEBenchVerified,
            'swe-bench-lite':SWEBenchLite, 'terminal-bench':TerminalBench,
            'terminal-bench-science':TerminalScience, 'deep-swe':DeepSWE, 'swe-polybench':PolyBench,
            'swe-bench-pro':SWEPro,'bigcodebench':BigCodeBench}

FAMILIES = {'swe-milestone': ['swe-milestone'],
            'swe-bench': ['swe-bench', 'swe-bench-verified', 'swe-bench-lite'],
            'terminal-bench': ['terminal-bench'], 'terminal-bench-science': ['terminal-bench-science'],
            'deep-swe': ['deep-swe'], 'swe-bench-pro': ['swe-bench-pro'],
            'swe-polybench': ['swe-polybench'], 'bigcodebench': ['bigcodebench']}


def get(name=DEFAULT):
    if not isinstance(name, str) or name not in REGISTRY:
        raise ValueError(f"unsupported benchmark {name!r}; available: {', '.join(REGISTRY)}")
    return REGISTRY[name]()


def describe(start_mode=None):
    rows = []
    for name in sorted(REGISTRY):
        adapter = get(name)
        modes = dict(task_start=adapter.task_start_description())
        if not adapter.describe()['from_task_start']:
            modes['checkpoint'] = adapter.describe()
        if start_mode is None:
            row = dict(adapter.describe(), modes=modes)
        elif start_mode in modes:
            row = dict(modes[start_mode])
        else:
            continue
        row['family'] = next(key for key, names in FAMILIES.items() if name in names)
        rows.append(row)
    return rows
