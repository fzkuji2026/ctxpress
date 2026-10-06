"""Deploy offline-selected cost policies through the same method operations."""
from __future__ import annotations
import copy
from ctxpress.core import policy as policies
from ctxpress.methods.cost_model import CostModel
from ctxpress.methods.native import CodexAutoCompact
from ctxpress.methods.wrappers import _Wrap


class AutoCostModel(_Wrap):
    source = '本文'

    def __init__(self, policy):
        self.policy = policies.load(policy) if not isinstance(policy, dict) else policies.validate(copy.deepcopy(policy))
        selected = self.policy['screening']['selected_lambda']
        reference = CodexAutoCompact(**self.policy['reference'].get('args', {}))
        inner = reference if selected is None else CostModel(lam=selected, profile=self.policy['statistics'], **self.policy['method_args'])
        super().__init__(inner)
        self.parameters = policies.parameters(self.policy['parameters'])
        self.codex_config = dict(reference.codex_config)
        self.name = '自动成本策略（原生压缩回退）' if selected is None else f'自动成本策略（λ={selected:g}）'
        self.framework['decider'] = '历史会话留一筛选；无合格 λ 则原生压缩回退；仅模拟约束证据'
