"""Terminal-Bench dataset identity on the shared official Harbor task format."""
from ctxpress.benchmarks.harbor.tasks import HarborTasks


class TerminalBench(HarborTasks):
    NAME = 'terminal-bench'
    TITLE = 'Terminal-Bench'
