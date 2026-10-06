"""Terminal-Bench-Science original tasks; a manifest prevents dataset relabeling."""
from ctxpress.benchmarks.harbor.tasks import HarborTasks


class TerminalScience(HarborTasks):
    NAME = 'terminal-bench-science'
    TITLE = 'Terminal-Bench-Science'
    REQUIRE_MANIFEST = True

    def describe(self):
        return dict(super().describe(), suite='explicit local Terminal-Bench-Science Harbor tasks; NVIDIA GPUs require pinned device UUIDs',
            task_scope='versioned scientific research workflows; dataset provenance must be declared')
