"""Explicit experiment constraints, separate from a model's advertised capacity.

Opt-in only: author defaults and frozen historical entries are unchanged.
Token counts use the shared rendered-prompt estimate, not provider guarantees.
"""
from copy import deepcopy
import math

PROFILE = dict(version=1, max_context_tokens=128000, compression_trigger_tokens=102400)


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
    measure = getattr(sim, 'measure_history', None)
    return math.ceil(measure(sim.ctx)) if measure else sim.size()


def attach(method, profile):
    from ctxpress.methods.native import NoCompaction
    method.context_profile = validate(profile)
    method.requires_host_view = True
    # Do not silently introduce a second compression method at the shared cap.
    method.unbounded = True  # disables Simulator/LiveContext's generic summary fallback only
    method.allow_native_compaction = False
    method.codex_config = dict(method.codex_config, **NoCompaction.codex_config,
                              model_context_window=profile['max_context_tokens'])
    return method


def instructions(method):
    base = method.instructions
    profile = getattr(method, 'context_profile', None)
    if not profile:
        return base
    guidance = (f"Experimental input-context limit: {profile['max_context_tokens']} tokens; "
                f"compression threshold: {profile['compression_trigger_tokens']} tokens. "
                "These are experimental constraints, not the model's physical capacity. "
                "Use this method's context operations as the threshold approaches, preserving the task, "
                "evidence and current work. Rolling and per-output compression may act earlier. "
                "There is no native-summary fallback at the limit.")
    return '\n\n'.join(s for s in (base, guidance) if s)


class ContextLimitError(ValueError):
    def __init__(self, tokens, profile):
        self.tokens, self.profile = tokens, dict(profile)
        super().__init__(f"Experimental context limit exceeded: estimated {tokens} input tokens > "
                         f"{profile['max_context_tokens']}; request not forwarded")


def check_body(body, profile):
    from ctxpress.core.hostsize import billable_chars
    tokens = math.ceil(billable_chars(body) / 4)
    if tokens > profile['max_context_tokens']:
        raise ContextLimitError(tokens, profile)
    return dict(**profile, input_tokens_estimate=tokens,
                trigger_reached=tokens >= profile['compression_trigger_tokens'],
                counting='shared rendered request chars/4 with media estimate; not exact provider tokens',
                native_fallback=False)


def check_auxiliary(client, payload, purpose):
    """Auxiliary compressors cannot silently use a larger experimental window."""
    profile = getattr(client, 'context_profile', None)
    if not profile:
        return
    try:
        return check_body(payload, profile)
    except ContextLimitError as error:
        if client.record:
            import time
            client.record(dict(type='experiment_context_limit', caller='summary',
                               purpose=purpose, t=time.time(), upstream_sent=False,
                               input_tokens_estimate=error.tokens, context_profile=profile,
                               reason=str(error)))
        raise
