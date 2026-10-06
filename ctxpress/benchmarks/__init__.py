"""Built-in benchmark adapters, independent of context-management methods."""
from .swe_milestone import SWEMilestone
from .swe_bench import SWEBench, SWEBenchVerified, SWEBenchLite
from .terminal_bench import TerminalBench
from .terminal_science import TerminalScience
from .deep_swe import DeepSWE
from .poly_bench import PolyBench
from .swe_pro import SWEPro
from .bigcode_bench import BigCodeBench

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
