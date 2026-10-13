"""Codex's own compaction (0.161, remote compaction v2) as a rule on a Responses history.

Ported from codex-rs/core/src/compact_remote_v2.rs (`build_v2_compacted_history`, `is_retained_for_remote_compaction_v2`,
`truncate_retained_messages`) and event_mapping.rs (`parse_turn_item` for user messages). When the context passes
the auto-compact limit (min(configured limit, 90% of the window), openai_models.rs `auto_compact_token_limit`), Codex
sends the history to `/responses/compact`; the new history is

    initial context (instructions and environment, re-injected) + retained messages + the returned compaction item

Retained messages are real user messages (not the contextual fragments Codex itself writes as user messages),
non-final agent messages of at most 10k tokens and client-authored developer messages, newest first within 64k
tokens; everything else (tool calls, their outputs, reasoning, assistant text) goes. The compaction item is written
by the server and is opaque, and it sees the previous one, so an earlier summary is carried into the next.

In infinite-context V5 terms: one operator, at size, whose scope keeps user messages under a token budget and no
recent turns, and whose writer is the host's compaction endpoint. repro/codex_compact_compare.py checks the rule
against the replacement histories Codex recorded.
"""
from __future__ import annotations
import json

RETAINED_MESSAGE_TOKEN_BUDGET = 64_000
MAX_RETAINED_AGENT_MESSAGE_TOKENS = 10_000
WINDOW_SHARE = 0.9

# Text Codex writes into user-role messages itself (contextual_user_message.rs CONTEXTUAL_USER_FRAGMENT_MATCHERS);
# messages containing one are not user messages and are not retained.
CONTEXTUAL_PREFIXES = ("<user_instructions>", "# AGENTS.md instructions", "<environment_context>", "<environments>",
                       "<additional_context>", "<agent_message_board", "<skill", "<user_shell_command>",
                       "<turn_aborted>", "<subagent_notification>", "<internal_model_context>",
                       "<recommended_plugins", "<unified_exec_process_limit", "<apply_patch_exec_command",
                       "<model_mismatch", "<hook_prompt")


def approx_tokens(text):
    """codex_utils_output_truncation::approx_token_count: bytes / 4, rounded up."""
    return (len(text.encode("utf-8")) + 3) // 4


def _texts(item):
    return [c.get("text", "") for c in item.get("content") or [] if c.get("type") in ("input_text", "output_text")]


def is_contextual(item):
    return any(t.lstrip().startswith(CONTEXTUAL_PREFIXES) for t in _texts(item))


def is_user_message(item):
    return (item.get("type", "message") == "message" and item.get("role") == "user" and not is_contextual(item))


def retained(history, metadata=None):
    """The messages Codex keeps when it compacts `history` (Responses items, oldest first), before re-injecting the
    initial context and appending the compaction item. `metadata[i]` may mark client-authored developer messages."""
    metadata = metadata or [None] * len(history)
    keep = []
    for item, md in zip(history, metadata):
        if item.get("type") == "agent_message":
            text = (_texts(item) or [""])[0]
            if (not text.startswith("Message Type: FINAL_ANSWER\n")
                    and approx_tokens(json.dumps(item)) <= MAX_RETAINED_AGENT_MESSAGE_TOKENS):
                keep.append(item)
        elif is_user_message(item):
            keep.append(item)
        elif (item.get("type", "message") == "message" and item.get("role") == "developer"
              and (md or {}).get("client_authored")):
            keep.append(item)
    out, remaining = [], RETAINED_MESSAGE_TOKEN_BUDGET
    for item in reversed(keep):                    # newest first within the budget; the boundary one is cut
        if remaining <= 0:
            break
        tokens = max(1, sum(approx_tokens(t) for t in _texts(item)))
        if tokens <= remaining:
            out.append(item)
            remaining -= tokens
        else:
            text = "\n".join(_texts(item))[: remaining * 4]
            out.append(dict(item, content=[dict(type="input_text", text=text)]))
            remaining = 0
    return list(reversed(out))
