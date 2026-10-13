"""CoMem k-step-off compression through the shared, metered model service.

Author prompt and asynchronous snapshot/append mechanism; native host messages
replace R2E-Gym serialization. No prefill optimization or policy training.
An optional token trigger gates only new summaries; archive updates and pending
summary completion still run below it. None preserves the author's scheduling.
"""
import copy
from concurrent.futures import ThreadPoolExecutor

from ctxpress.methods.base import Method
from ctxpress.methods.experiment_context import context_tokens
from ctxpress.methods.paper_utils import complete_items, positive_int
from ctxpress.methods._paper_sources.comem_prompt import compressor_prompt_v5


class CoMem(Method):
    source = 'arXiv 2605.30842'
    name = 'CoMem (asynchronous host adapter)'
    requires_summary = True
    isolate_history_epochs = True

    def __init__(self, interval=1, lag=1, summary_model=None, trigger_tokens=None):
        self.interval = positive_int('interval', interval)
        self.lag = positive_int('lag', lag)
        self.summary_model = summary_model
        self.trigger_tokens = None if trigger_tokens is None else positive_int('trigger_tokens', trigger_tokens)
        self.executor = None
        self.pending = None
        self.archive = {}
        self.retired = False

    def __deepcopy__(self, memo):
        # A speculative host request must not share an in-flight context edit.
        new = type(self)(self.interval, self.lag, self.summary_model, trigger_tokens=self.trigger_tokens)
        memo[id(self)] = new
        new.archive = copy.deepcopy(self.archive, memo)
        for name in ('context_profile', 'requires_host_view', 'instructions', 'codex_config',
                     'unbounded', 'allow_native_compaction'):
            if name in self.__dict__:
                setattr(new, name, copy.deepcopy(self.__dict__[name], memo))
        return new

    def reset(self, sim):
        self.close()
        self.executor = self.pending = None
        self.archive = {}
        self.retired = False

    def retire(self):
        self.retired = True

    def close(self):
        if self.executor is not None:
            # Drain actual billable calls, including discarded summaries, before
            # run teardown. Do not cancel a request and silently lose its usage.
            self.executor.shutdown(wait=True)
            self.executor = None
        self.pending = None

    def step(self, sim, r):
        if self.retired:
            return
        for item in sim.sent():
            if not item.get('comem_summary'):
                self.archive[item['id']] = dict(item, text=sim.current_text(item) or item.get('text', ''))
        if self.pending and r >= self.pending[0] + self.lag:
            _, ids, future = self.pending
            self.pending = None
            answer = future.result()  # k-step deadline, same wait semantics as author loop
            current = [s for s in sim.ctx if s['id'] in ids]
            selected = complete_items(sim, current)
            if answer and {s['id'] for s in selected} == ids:
                sim.summarize_segment(selected, text=answer, billed=False, model_written=True,
                                      role='assistant', comem_summary=True)
        if self.pending or r <= 0 or r % self.interval:
            return
        if self.trigger_tokens is not None and context_tokens(sim) < self.trigger_tokens:
            return
        selected = complete_items(sim, sim.ctx)
        if not any(not s.get('comem_summary') for s in selected):
            return
        # The compressor sees full accumulated source history, not recursively
        # just its last summary. Newer turns remain intact when a result lands.
        history = '\n\n'.join(f"{s.get('role', s['seg']).upper()}:\n{s.get('text', '')}"
                              for s in self.archive.values())
        prompt = compressor_prompt_v5.format(conversation=history)
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ctxpress-comem')
        # Rewriter temporarily installs a per-request summary client; capture it
        # now, before it is restored or replaced by the next host request.
        captured = copy.copy(sim)
        captured.summarizer = sim.summarizer
        future = self.executor.submit(captured.model_text, '', prompt,
                                      purpose='history', model=self.summary_model)
        self.pending = (r, {s['id'] for s in selected}, future)
