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


# How a method meets the shared window (class attribute `context_role`):
#   full  - the method manages the whole history and replaces the host's native compaction;
#   local - the method only edits part of the history (new or old tool outputs); the host's native
#           compaction stays on at the shared window, as in an ordinary deployment.
ROLES = ('full', 'local')


def role(method):
    value = getattr(method, 'context_role', 'full')
    if value not in ROLES:
        raise ValueError(f'context_role must be one of {ROLES}')
    return value


def attach(method, profile):
    from ctxpress.methods.native import NoCompaction
    method.context_profile = validate(profile)
    method.requires_host_view = True
    method.unbounded = True  # disables Simulator/LiveContext's generic summary fallback only
    if role(method) == 'local':
        method.allow_native_compaction = True
        method.codex_config = dict(method.codex_config, model_context_window=profile['max_context_tokens'])
    else:
        method.allow_native_compaction = False
        method.codex_config = dict(method.codex_config, **NoCompaction.codex_config,
                                  model_context_window=profile['max_context_tokens'])
    return method


def instructions(method):
    base = method.instructions
    profile = getattr(method, 'context_profile', None)
    if not profile:
        return base
    host = ("The host's native compaction remains active at the limit." if role(method) == 'local' else
            "There is no native-summary fallback at the limit.")
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


def check_body(body, profile):
    from ctxpress.core.hostsize import billable_chars
    tokens = math.ceil(billable_chars(body) / 4)
    if tokens > profile['max_context_tokens']:
        raise ContextLimitError(tokens, profile)
    return dict(**profile, input_tokens_estimate=tokens,
                trigger_reached=tokens >= profile['compression_trigger_tokens'],
                counting='shared rendered request chars/4 with media estimate; not exact provider tokens')


OUTPUT_TYPES = ('function_call_output', 'custom_tool_call_output')
FALLBACK_RULE = 'oldest-tool-outputs-v1'
OMITTED = '[ctxpress: earlier tool output omitted to fit the {} token experimental window]'
QUANTUM_TOKENS = 8192  # the cut moves in steps, so a growing history keeps its cached prefix


def output_text(item):
    value = item.get('output')
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(part.get('text', '') for part in value if isinstance(part, dict))
    return ''


def fit_body(body, profile):
    """The one overflow rule, the same for every method and for the host's own compaction request.

    Applied to the request about to be sent, after the method; the method's own state is unchanged.
    When the estimate exceeds the window, the oldest tool outputs are replaced by a short notice until
    it fits, the omitted amount rounded up to QUANTUM_TOKENS. User messages, reasoning, tool calls and
    the newest tool output are kept; the newest output is shortened (head and tail kept) only when
    nothing else is left. No model is called. Returns (body, record); record is None if unchanged.
    """
    from ctxpress.core.hostsize import billable_chars
    limit = profile['max_context_tokens'] * 4
    total = billable_chars(body)
    items = body.get('input') if isinstance(body, dict) else None
    if total <= limit or not isinstance(items, list):
        return body, None
    note = OMITTED.format(profile['max_context_tokens'])
    outputs = [i for i, item in enumerate(items) if isinstance(item, dict) and item.get('type') in OUTPUT_TYPES]
    savings, saved = [], 0
    for i in outputs[:-1]:
        saved += max(billable_chars(items[i]) - billable_chars(dict(items[i], output=note)), 0)
        savings.append(saved)
    needed = total - limit
    target = -(-needed // (QUANTUM_TOKENS * 4)) * QUANTUM_TOKENS * 4
    count = next((k + 1 for k, s in enumerate(savings) if s >= target), None)
    if count is None:
        count = next((k + 1 for k, s in enumerate(savings) if s >= needed), len(savings))
    items = list(items)
    for i in outputs[:count]:
        items[i] = dict(items[i], output=note)
    body = dict(body, input=items)
    total = billable_chars(body)
    truncated = False
    if total > limit and outputs:
        i = outputs[-1]
        text = output_text(items[i])
        keep = len(text)
        while total > limit and keep > 0:
            keep = max(0, keep - (total - limit) - 64)
            marker = f'\n[ctxpress: {len(text) - keep} characters omitted to fit the window]\n'
            items[i] = dict(items[i], output=text[:keep // 2] + marker + text[len(text) - (keep - keep // 2):])
            body = dict(body, input=items)
            total = billable_chars(body)
        truncated = True
    return body, dict(rule=FALLBACK_RULE, tokens_before=math.ceil((needed + limit) / 4),
                      tokens_after=math.ceil(total / 4), outputs_omitted=count, newest_output_truncated=truncated)


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
