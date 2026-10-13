"""ReSum author inference mechanism adapted to the shared tool-history interface.

Prompt construction, response parsing and trigger follow DeepResearch f72f75d.
Host tokenization, turn boundaries, protected messages and model serving differ.
This does not reproduce ReSumTool weights or ReSum-GRPO training.
"""
import re

from ctxpress.methods.base import Method
from ctxpress.methods.resum_prompts import QUERY_SUMMARY_PROMPT, QUERY_SUMMARY_PROMPT_LAST


def summary_prompt(question, messages, previous=None):
    template = QUERY_SUMMARY_PROMPT_LAST if previous else QUERY_SUMMARY_PROMPT
    result = template.replace("{{{question}}}", question).replace(
        "{{{recent_history_messages}}}", "\n".join(str(m) for m in messages))
    return result.replace("{{{last_summary}}}", previous) if previous else result


def parse_summary(content):
    if not content:
        return ""
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    try:
        content = content.split("<summary>")[1].split("</summary>")[0]
    except IndexError:
        pass
    return "<summary>" + content + "</summary>"


def should_summarize(tokens, round_number, context_k, interval, calls_available=True, enabled=True,
                     trigger_tokens=None):
    """Use the author threshold unless overridden; interval=None disables periodic triggers."""
    threshold = (context_k * 1024 - 1000) * 0.9 if trigger_tokens is None else trigger_tokens
    return bool(((enabled and tokens >= threshold)
                 or (interval is not None and round_number % interval == 0)) and calls_available)


def continuation(question, summary):
    return ("Question: " + question + "\nBelow is a summary of the previous conversation. "
            "This summary condenses key information from earlier steps, so please consider it carefully. "
            "Assess whether the summary provides enough information to answer the question and use it as "
            "the basis for further reasoning and information gathering to answer the question.\n"
            + "Summary: " + summary + "\n")


class ReSumSource(Method):
    source = "arXiv 2509.13313"
    name = "ReSum (author inference mechanism)"
    requires_summary = True
    framework = dict(L1="无", L2="无", L3="作者阈值/周期触发，上一摘要与新增历史续接",
                     cross="保护宿主指令和用户消息", memory="无", decider="阈值或周期")

    def __init__(self, context_k=32, interval=40, summary_model=None, trigger_tokens=None):
        if type(context_k) is not int or context_k < 1:
            raise ValueError("context_k must be a positive integer")
        if interval is not None and (type(interval) is not int or interval < 1):
            raise ValueError("interval must be a positive integer or None")
        if trigger_tokens is not None and (type(trigger_tokens) is not int or trigger_tokens < 1):
            raise ValueError("trigger_tokens must be a positive integer or None")
        self.context_k, self.interval, self.summary_model = context_k, interval, summary_model
        self.trigger_tokens = trigger_tokens

    def step(self, sim, r):
        # The host invokes us before requests, unlike the author's post-tool loop.
        if r <= 0:
            return
        if self.trigger_tokens is None:
            tokens = sim.size()
        else:
            # Explicit experiment triggers include the complete rendered input.
            from ctxpress.methods.experiment_context import context_tokens
            tokens = context_tokens(sim)
        if not should_summarize(tokens, r, self.context_k, self.interval,
                                trigger_tokens=self.trigger_tokens):
            return
        previous = [s for s in sim.ctx if s.get('resum_summary')]
        selected = [s for s in sim.ctx if not s.get('protected') and not s.get('has_media')
                    and s.get('role') not in ('user', 'system', 'developer') and not s.get('resum_summary')]
        ids = {s['id'] for s in selected}
        crossing = {s.get('call_id') for s in sim.ctx if s['id'] not in ids and s.get('call_id')}
        calls = {s.get('call_id') for s in selected if s['seg'] == 'call'}
        outputs = {s.get('call_id') for s in selected if s['seg'] == 'out'}
        blocked = crossing | (calls ^ outputs)
        selected = [s for s in selected if not s.get('call_id') or s['call_id'] not in blocked]
        if not selected:
            return
        question = '\n'.join(s.get('text', '') for s in sim.sent() if s.get('role') == 'user'
                             and not s.get('resum_summary'))
        messages = [dict(role=s.get('role', s['seg']), content=sim.current_text(s) or s.get('text', ''))
                    for s in selected]
        last = previous[-1].get('resum_payload') if previous else None
        answer = sim.model_text('', summary_prompt(question, messages, last),
                                purpose='history', model=self.summary_model)
        if not answer:
            return
        summary = parse_summary(answer)
        sim.summarize_segment(previous + selected, text=continuation(question, summary),
                              billed=False, model_written=True, role='assistant',
                              resum_summary=True, resum_payload=summary)
