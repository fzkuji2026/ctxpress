"""Declared retention budgets, without conflating them with trigger thresholds or ages."""
from __future__ import annotations
from dataclasses import dataclass
import copy


@dataclass(frozen=True)
class BudgetSpec:
    parameter: str
    unit: str
    scope: str
    default: int
    minimum: int = 0

    def validate(self, value):
        if type(value) is not int or value < self.minimum:
            raise ValueError(f"budget ({self.scope}, {self.unit}) must be an integer >= {self.minimum}")
        return value

    def resolve(self, budget=None, legacy=None):
        for value in (budget, legacy):
            if value is not None:
                self.validate(value)
        if budget is not None and legacy is not None and budget != legacy:
            raise ValueError(f"budget conflicts with {self.parameter}")
        return self.default if budget is None and legacy is None else (legacy if budget is None else budget)


def with_budget(entry, budget):
    """Copy a declarative method entry and add a budget without silently overriding it."""
    result = copy.deepcopy(entry)
    args = result.setdefault("args", {})
    if args is None:
        args = result["args"] = {}
    if "budget" in args and (type(args["budget"]) is not int or args["budget"] != budget):
        raise ValueError("budget conflicts with the configured budget")
    args["budget"] = budget
    return result


def resolve_wrapper_budget(cls, args):
    """Transparent wrappers forward only to their explicitly selected inner method.

    EntryTruncation has its own ingress budget; Composed is ambiguous. Neither
    receives this forwarding behavior.
    """
    args = dict(args)
    if "budget" not in args:
        return args
    if cls.forwards_budget:
        inner = args.get("inner")
        if not isinstance(inner, dict):
            raise ValueError("set budget when constructing the inner Method instance")
        args["inner"] = with_budget(inner, args.pop("budget"))
    elif cls.budget_spec is None:
        raise ValueError(f"{cls.__name__} has no retention budget; configure its method-specific parameters")
    else:
        cls.budget_spec.validate(args["budget"])
    return args
