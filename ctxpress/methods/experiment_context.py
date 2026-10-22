"""Explicit experiment constraints, separate from a model's advertised capacity.

Opt-in only: author defaults and frozen historical entries are unchanged.
Token counts use the shared rendered-prompt estimate, not provider guarantees.
"""
from copy import deepcopy
import math

TRIGGER_FRACTION = 0.8  # every method's compression threshold is 80% of the experimental window


def profile(window):
    """The shared context profile for one experimental window (decimal tokens)."""
    if type(window) is not int or window <= 0:
        raise ValueError('window must be a positive integer')
    return dict(version=1, max_context_tokens=window, compression_trigger_tokens=round(window * TRIGGER_FRACTION))


PROFILE = profile(128000)


def validate(profile):
    if not isinstance(profile, dict) or set(profile) != set(PROFILE):
        raise ValueError('context_profile requires version, max_context_tokens and compression_trigger_tokens')
    if type(profile['version']) is not int or profile['version'] != 1:
        raise ValueError('unsupported context_profile version')
    maximum, trigger = profile['max_context_tokens'], profile['compression_trigger_tokens']
    if any(type(v) is not int or v <= 0 for v in (maximum, trigger)) or trigger >= maximum:
        raise ValueError('context_profile requires 0 < compression trigger < context maximum')
    return deepcopy(profile)


def context_tokens(sim):
    """Estimated input tokens of the next request (live: the session's size model, ctxpress.core.sizes)."""
    return math.ceil(sim.request_tokens())


def payload_tokens(payload, model=None):
    """Estimated input tokens of a request body that is not the session's next turn (a summary or compaction
    request): o200k_base tokens of every item and of the instructions and tools, plus `model`'s fitted overhead
    per item (a fresh model's prior without one). Chat Completions bodies count their messages' text."""
    from ctxpress.core import sizes, tokens
    model = model or sizes.SizeModel()
    if isinstance(payload.get('input'), list):
        x, counted = sizes.features([sizes.parts(i) for i in payload['input']])
        return round(model.model(x, counted + sizes.fixed_tokens(payload)))
    rows = [("message", tokens.count(m.get('content') if isinstance(m.get('content'), str) else
                                     sizes._text(m.get('content'))), 0) for m in payload.get('messages') or []]
    x, counted = sizes.features(rows)
    return round(model.model(x, counted + sizes.fixed_tokens(payload)))


# What a method manages (class attribute `context_role`), reported only; the run setup is the same:
#   full  - the whole history (its own trigger, below the host's);
#   local - part of the history (new or old tool outputs).
# Every method keeps the host's native compaction at the shared window: it is the only fallback.
ROLES = ('full', 'local')


def role(method):
    value = getattr(method, 'context_role', 'full')
    if value not in ROLES:
        raise ValueError(f'context_role must be one of {ROLES}')
    return value


def attach(method, profile):
    method.context_profile = validate(profile)
    method.requires_host_view = True
    method.unbounded = True  # disables Simulator/LiveContext's generic summary fallback only
    method.allow_native_compaction = True
    method.codex_config = dict(method.codex_config, model_context_window=profile['max_context_tokens'])
    return method


def instructions(method):
    base = method.instructions
    profile = getattr(method, 'context_profile', None)
    if not profile:
        return base
    host = "The host's native compaction remains active at the limit."
    guidance = (f"Experimental input-context limit: {profile['max_context_tokens']} tokens; "
                f"compression threshold: {profile['compression_trigger_tokens']} tokens. "
                "These are experimental constraints, not the model's physical capacity. "
                "Use this method's context operations as the threshold approaches, preserving the task, "
                "evidence and current work. Rolling and per-output compression may act earlier. " + host)
    return '\n\n'.join(s for s in (base, guidance) if s)


class ContextLimitError(ValueError):
    def __init__(self, tokens, profile):
        self.tokens, self.profile = tokens, dict(profile)
        super().__init__(f"Experimental context limit exceeded: estimated {tokens} input tokens > "
                         f"{profile['max_context_tokens']}; request not forwarded")


def check_body(tokens, profile):
    """Measure the request about to be sent (`tokens`: its estimated input tokens, the session's size model).
    Nothing is cut or refused: a request over the window is forwarded and recorded (`over_limit`), since the
    host's native compaction is the only fallback."""
    if tokens is None:
        return dict(**profile, input_tokens_estimate=None, trigger_reached=None, over_limit=None,
                    counting='no size estimate')
    return dict(**profile, input_tokens_estimate=tokens,
                trigger_reached=tokens >= profile['compression_trigger_tokens'],
                over_limit=tokens > profile['max_context_tokens'],
                counting='o200k_base tokens of each item plus overhead fitted on billed input tokens (ctxpress.core.sizes)')


def check_auxiliary(client, payload, purpose):
    """Auxiliary compressors cannot silently use a larger experimental window."""
    profile = getattr(client, 'context_profile', None)
    if not profile:
        return
    measured = check_body(payload_tokens(payload), profile)
    if measured['over_limit']:
        error = ContextLimitError(measured['input_tokens_estimate'], profile)
        if client.record:
            import time
            client.record(dict(type='experiment_context_limit', caller='summary',
                               purpose=purpose, t=time.time(), upstream_sent=False,
                               input_tokens_estimate=error.tokens, context_profile=profile,
                               reason=str(error)))
        raise error
    return measured
