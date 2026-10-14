"""Frozen, explicit settings for the 128k paper comparison (not author defaults)."""
from copy import deepcopy
from ctxpress.methods.experiment_context import PROFILE

VERSION = 'paper-context-128k-20261014-v2'

# Counts, relevance probabilities and character thresholds are not token windows.
# Do not turn a paper's rolling filter or autonomous operation into another algorithm.
SETTINGS = {
    'HostDefault': {},
    'NoCompaction': {},
    'ComplexityTrap': dict(n=10, pin_spec=False, polling=1),
    'ComplexityTrapSummary': dict(n=21, m=10),
    'ComplexityTrapHybrid': dict(n=43, m=10, w=10, polling=1),
    'AgentDiet': dict(a=2, b=1, threshold=500, min_saving=400, max_ratio=0.8,
                     reflect_model='gpt-6-luna', bypass_filter=False),
    'ACONSource': dict(t_hist=102400, trigger_tokens=102400, t_obs=25600,
                       keep_turns=2, summary_model=None),
    'ReSumSource': dict(context_k=125, trigger_tokens=102400, interval=None, summary_model=None),
    'AgentFoldSource': {},
    'SWEPruner': dict(threshold=0.5, min_chars=500, chunk_overlap_tokens=50),
    'TokenPilotLifecycle': dict(entry_budget=25600, batch_turns=8, min_chars=256, estimator_model=None),
    'ACM': dict(summary_model=None),
    'CWL': dict(budget=102400, full_context=True),
    'DTOC': dict(strategy=True),
    'ContextFoldingTools': dict(max_branches=None, branch_max_turns=None,
                                branch_timeout=None, branch_tokens=25600),
    'FoldActSource': dict(max_model_len=128000, selection_tokens=102400, max_obs_length=None),
    'TACO': dict(uncovered_threshold=3000, freeze_rules=False,
                task_category='software-engineering', summary_model=None),
    'CoMem': dict(trigger_tokens=102400, interval=1, lag=1, summary_model=None),
    'LongSeeker': {},
    'ZipRL': {},
}

NOTES = {
    'HostDefault': 'Control: the host alone; its native compaction at the 128000 window.',
    'NoCompaction': 'Control: no method and no native compaction; only the shared overflow rule.',
    'ComplexityTrap': 'Rolling masking retains 10 latest outputs; no global length trigger in this algorithm.',
    'ComplexityTrapSummary': 'Author 21+10-turn checkpoint schedule retained; 102400 is not a substitute for turn counts.',
    'ComplexityTrapHybrid': 'Author 43+10-turn summary and latest-10 masking retained.',
    'AgentDiet': '500 is a per-step eligibility threshold, 400 minimum saving, 0.8 acceptance ratio; these are not context caps.',
    'ACONSource': 'History uses the complete-input 102400 trigger. Per-observation threshold is 25600 (window headroom); retain 2 recent turns.',
    'ReSumSource': 'Explicit 102400 complete-input trigger replaces the implicit 32k-derived trigger; periodic 40-turn trigger disabled.',
    'AgentFoldSource': 'Agent chooses fold ranges and summary using its tool/visible tags; shared threshold is guidance, not a forced model call.',
    'SWEPruner': '0.5 relevance score, 500 characters and 50-token overlap retained; focus question and real service remain required.',
    'TokenPilotLifecycle': 'Per-output cap increased from 2000 to 25600; lifecycle estimation every 8 requests retained, 256 is characters.',
    'ACM': 'Agent-selected segments and archival retrieval; same window guidance, no forced replacement algorithm.',
    'CWL': '102400 complete-input estimate including fixed instructions replaces 80000 conversation-only estimate; dependencies can still restore history.',
    'DTOC': 'Agent controls hide/show; shared window guidance; strategy instructions enabled.',
    'ContextFoldingTools': 'Remove lifetime branch count and branch time/turn caps; 25600 branch-growth allowance uses shared headroom, complete-pair protection retained.',
    'FoldActSource': '128000 model-length setting, explicit 102400 selection budget; remove 4096 observation clipping and its forced last-three-turn side effect.',
    'TACO': '3000 uncovered characters triggers rule induction, not a token cap; rule feedback and dynamic planning retained.',
    'CoMem': 'Only schedule a new async summary at 102400; retain one-step lag, settle pending calls and preserve full source archive.',
    'LongSeeker': 'Agent chooses among five operations; shared window guidance, no forced summary mechanism.',
    'ZipRL': 'Agent chooses compression level 1–5 and summary; shared window guidance, no artificial length replacement.',
}


def arguments(name):
    return dict(deepcopy(SETTINGS[name]), context_profile=deepcopy(PROFILE))


def describe():
    from ctxpress.methods import REGISTRY
    from ctxpress.methods.experiment_context import FALLBACK_RULE, QUANTUM_TOKENS, role
    return dict(version=VERSION, context=deepcopy(PROFILE),
                counting='complete rendered agent input; shared chars/4 and media estimate, not exact provider tokens',
                scope=('Shared agent and host-compaction input limit. A full method replaces the host native '
                       'compaction; a local method keeps it. Auxiliary-model inputs are not truncated to fit. '
                       'Per-call generation limits are unchanged.'),
                overflow=dict(rule=FALLBACK_RULE, quantum_tokens=QUANTUM_TOKENS,
                              description='Oldest tool outputs replaced by a notice until the request fits; '
                                          'no model call; every use recorded.'),
                external_request_or_time_caps=False,
                methods=[dict(method=name, role=role(REGISTRY[name]), args=arguments(name), note=NOTES[name])
                         for name in SETTINGS])
