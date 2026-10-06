"""Base class of a context-management method.

A method is only a rule. The engine calls, for each request r of the replayed session:
  on_ingest(sim, item)   when a new segment enters the context (entry-time handling: truncation etc.)
  step(sim, r)           before the request is sent: change the context with sim.to_placeholder / truncate /
                         structure / delete / summarize / summarize_segment
  overhead(sim, r)       the method's own decision cost this request (LLM / small-model scoring), in input-token
                         units; 0 for fixed rules
  on_fault(sim, item)    the agent had to re-fetch `item`
Class attributes
  memory     None | 'id' | 'label': removed content goes to a dedicated store; its placeholder carries an id
             (and a label of what it was); recovery is a retrieval instead of re-running the call
  hint       after a session summary, tell the agent to re-read the requirements and the files it is editing
  unbounded  True only for "no compaction" (the shared window fallback is off)
  framework  where the method sits in the framework (experiment.zh.html §9): one value per layer / part
  agent_tools  tools the method gives the agent ({name, description, inputSchema}); the host exposes them (Codex:
             `ctxpress mcp`), calls go to call_tool(name, args) -> (text, is_error) on a method object of that tool
             process, and the method sees the calls and their outputs in later requests like any other tool's
"""
from __future__ import annotations

K = 1000
REPLACEABLE = ("full", "truncated", "structured")      # forms a placeholder can still replace


def replaceable(s):
    return s.get("form", "full") in REPLACEABLE


class Method:
    name = "method"
    memory = None
    hint = False
    unbounded = False
    allow_native_compaction = True
    framework = {}
    source = ""
    instructions = ""
    requires_summary = False
    codex_config = {}
    parameters = None
    budget_spec = None
    forwards_budget = False
    max_overflow_retries = 0
    agent_tools = ()

    def call_tool(self, name, args):
        raise KeyError(f"unknown tool {name}")

    def reset(self, sim):
        pass

    def validate_live(self):
        """Check dependencies before the host starts; replay approximations stay separate."""
        pass

    def on_ingest(self, sim, item):
        pass

    def step(self, sim, r):
        pass

    def overhead(self, sim, r):
        return 0.0

    def on_fault(self, sim, item):
        pass

    def on_overflow(self, sim, request):
        """Adjust the current view after an explicit upstream length rejection.

        Return True only when ready to retry. Transport and error recognition
        belong to the host; no new history items or model turns are ingested.
        """
        return False

    def __repr__(self):
        return self.name


FRAMEWORK_KEYS = ["L1", "L2", "L3", "cross", "memory", "decider"]
FRAMEWORK_TITLES = {"L1": "第 1 层：单条工具输出", "L2": "第 2 层：一段工作", "L3": "第 3 层：整个会话",
                    "cross": "跨层（固定保留、找回、提示）", "memory": "专用记忆", "decider": "决策器"}
