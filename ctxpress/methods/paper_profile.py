"""Explicit settings for the paper comparison at one experimental window (not author defaults).

Every token setting is derived from the window W: the compression trigger T = 0.8 W (the shared
context profile) and the per-output / branch headroom H = W - T. Counts, relevance probabilities and
per-item character thresholds are not windows and stay at the author values; methods whose author rule
fires on a fixed turn count (the Complexity Trap family) fire at T instead.
"""
from copy import deepcopy
from ctxpress.methods.experiment_context import profile

WINDOW = 128000                               # the default window (the v3 cohort)
WINDOWS = (32000, 64000, 128000, 256000)      # published configurations, configs/paper-context-<k>k.methods.json


def version(window=WINDOW):
    return f'paper-context-{window // 1000}k-20261016-v4'


VERSION = version()


def settings(window=WINDOW):
    """Method arguments for one window. Do not turn a paper's rolling filter or autonomous operation into
    another algorithm: only the quantities that encode a context size follow the window."""
    shared = profile(window)
    t = shared['compression_trigger_tokens']
    h = window - t
    return {
        'HostDefault': {},
        'ComplexityTrap': dict(n=10, pin_spec=False, polling=1, trigger_tokens=t),
        'ComplexityTrapSummary': dict(n=21, m=10, trigger_tokens=t),
        'ComplexityTrapHybrid': dict(n=43, m=10, w=10, polling=1, trigger_tokens=t),
        'AgentDiet': dict(a=2, b=1, threshold=500, min_saving=400, max_ratio=0.8,
                          reflect_model='gpt-6-luna', bypass_filter=False),
        'ACONSource': dict(t_hist=t, trigger_tokens=t, t_obs=h, keep_turns=2, summary_model=None),
        'ReSumSource': dict(context_k=window // 1024, trigger_tokens=t, interval=None, summary_model=None),
        'AgentFoldSource': {},
        'SWEPruner': dict(threshold=0.5, min_chars=500, chunk_overlap_tokens=50),
        'TokenPilotLifecycle': dict(entry_budget=h, batch_turns=8, min_chars=256, estimator_model=None),
        'ACM': dict(summary_model=None),
        'CWL': dict(budget=t, full_context=True),
        'DTOC': dict(strategy=True),
        'ContextFoldingTools': dict(max_branches=None, branch_max_turns=None,
                                    branch_timeout=None, branch_tokens=h),
        'FoldActSource': dict(max_model_len=window, selection_tokens=t, max_obs_length=None),
        'TACO': dict(uncovered_threshold=3000, freeze_rules=False,
                     task_category='software-engineering', summary_model=None),
        'CoMem': dict(trigger_tokens=t, interval=1, lag=1, summary_model=None),
        'LongSeeker': {},
        'ZipRL': {},
    }


SETTINGS = settings()


def notes(window=WINDOW):
    t = profile(window)['compression_trigger_tokens']
    h = window - t
    return {
        'HostDefault': f'Control: the host alone; its native compaction at the {window} window.',
        'ComplexityTrap': f'Author rolling mask (latest 10 outputs) fires only when the complete input reaches {t}; masked outputs stay masked.',
        'ComplexityTrapSummary': f'When the complete input reaches {t}, all unsummarized turns except the latest 10 become one checkpoint (author 21+10 turn schedule replaced by the window trigger).',
        'ComplexityTrapHybrid': f'Checkpoint and latest-10 masking both fire at {t}, summary first as in the author order.',
        'AgentDiet': '500 is a per-step eligibility threshold, 400 minimum saving, 0.8 acceptance ratio; these are not context caps.',
        'ACONSource': f'History uses the complete-input {t} trigger. Per-observation threshold is {h} (window headroom); retain 2 recent turns.',
        'ReSumSource': f'Explicit {t} complete-input trigger (context_k {window // 1024}) replaces the implicit 32k-derived trigger; periodic 40-turn trigger disabled.',
        'AgentFoldSource': 'Agent chooses fold ranges and summary using its tool/visible tags; shared threshold is guidance, not a forced model call.',
        'SWEPruner': '0.5 relevance score, 500 characters and 50-token overlap retained; focus question and real service remain required.',
        'TokenPilotLifecycle': f'Per-output cap increased from 2000 to {h}; lifecycle estimation every 8 requests retained, 256 is characters.',
        'ACM': 'Agent-selected segments and archival retrieval; same window guidance, no forced replacement algorithm.',
        'CWL': f'{t} complete-input estimate including fixed instructions replaces 80000 conversation-only estimate; dependencies can still restore history.',
        'DTOC': 'Agent controls hide/show; shared window guidance; strategy instructions enabled.',
        'ContextFoldingTools': f'Remove lifetime branch count and branch time/turn caps; {h} branch-growth allowance uses shared headroom, complete-pair protection retained.',
        'FoldActSource': f'{window} model-length setting, explicit {t} selection budget; remove 4096 observation clipping and its forced last-three-turn side effect.',
        'TACO': '3000 uncovered characters triggers rule induction, not a token cap; rule feedback and dynamic planning retained.',
        'CoMem': f'Only schedule a new async summary at {t}; retain one-step lag, settle pending calls and preserve full source archive.',
        'LongSeeker': 'Agent chooses among five operations; shared window guidance, no forced summary mechanism.',
        'ZipRL': 'Agent chooses compression level 1–5 and summary; shared window guidance, no artificial length replacement.',
    }


NOTES = notes()


def arguments(name, window=WINDOW):
    return dict(deepcopy(settings(window)[name]), context_profile=profile(window))


def describe(window=WINDOW):
    from ctxpress.methods import REGISTRY
    from ctxpress.methods.experiment_context import role
    window_notes, names = notes(window), settings(window)
    return dict(version=version(window), context=profile(window),
                counting='complete rendered agent input; shared chars/4 and media estimate, not exact provider tokens',
                scope=('Shared agent input limit. Every method keeps the host native compaction at the '
                       'window as the only fallback; ctxpress never cuts or refuses an agent request, and a '
                       'request over the window is forwarded and recorded (over_limit). Auxiliary-model inputs '
                       'over the window fail and are recorded. Per-call generation limits are unchanged.'),
                fallback='host native compaction',
                external_request_or_time_caps=False,
                methods=[dict(method=name, role=role(REGISTRY[name]), args=arguments(name, window),
                              note=window_notes[name]) for name in names])
