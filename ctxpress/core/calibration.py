"""Portable cost-model statistics, containing no tool text or executable objects.

Fitting stays offline. A live method only loads frozen tables and makes decisions.
"""
from __future__ import annotations
import copy
import json
import math
import os
import statistics

BUCKETS = (1, 2, 3, 5, 8, 13, 21, 34, 55, 89)


def bucket(age):
    return next((b for b in BUCKETS if age <= b), 144)


def remaining_from_lengths(lengths):
    lengths = tuple(lengths)
    def remaining(request):
        rest = [length - request for length in lengths if length > request]
        return statistics.mean(rest) if rest else 20
    return remaining


class FrozenCurves:
    def __init__(self, table, raw=None):
        from ctxpress.core.engine import item_type
        self.tab, self.raw, self.type_fn = table, raw or table, item_type

    def pm(self, item, age, use_types=True):
        table = self.tab.get(self.type_fn(item)) if use_types else None
        table = table or self.tab["*"]
        return tuple(table.get(str(bucket(age)), table[max(table, key=lambda key: int(key))]))


def _number(value, low=0, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("calibration statistics must be finite numbers")
    if value < low or (high is not None and value > high):
        raise ValueError("calibration statistic outside its allowed range")
    return value


def validate(profile):
    if not isinstance(profile, dict) or profile.get("schema") != "ctxpress.cost-model" or profile.get("version") != 1:
        raise ValueError("unsupported cost-model calibration format")
    if profile.get("type_fn") != "item_type":
        raise ValueError("unsupported calibration content classifier")
    lengths = profile.get("lengths")
    if not isinstance(lengths, list) or not lengths or any(type(n) is not int or n < 2 for n in lengths):
        raise ValueError("calibration needs training sessions with at least two requests")
    for key in ("curves", "raw_curves"):
        tables = profile.get(key)
        if not isinstance(tables, dict) or not tables.get("*"):
            raise ValueError("calibration is missing pooled reuse curves")
        for kind, table in tables.items():
            if not isinstance(kind, str) or not isinstance(table, dict) or not table:
                raise ValueError("invalid calibration reuse table")
            for age, stats in table.items():
                if age not in {str(b) for b in (*BUCKETS, 144)} or not isinstance(stats, (list, tuple)) or len(stats) != 2:
                    raise ValueError("invalid calibration reuse bucket")
                _number(stats[0], high=1)
                _number(stats[1])
    budget = profile.get("truncate_budget")
    if type(budget) is not int or budget < 1:
        raise ValueError("calibration truncation budget must be positive")
    coverage = profile.get("coverage")
    if not isinstance(coverage, list):
        raise ValueError("invalid calibration coverage table")
    seen = set()
    for row in coverage:
        if not isinstance(row, dict) or row.get("form") not in ("truncated", "structured") or not isinstance(row.get("type"), str):
            raise ValueError("invalid calibration coverage row")
        _number(row.get("rate"), high=1)
        if type(row.get("count")) is not int or row["count"] < 1:
            raise ValueError("calibration coverage count must be positive")
        key = row["form"], row["type"]
        if key in seen:
            raise ValueError("duplicate calibration coverage row")
        seen.add(key)
    return profile


def load(path):
    with open(os.path.expanduser(os.fspath(path)), encoding="utf-8") as fh:
        return validate(json.load(fh))


def save(path, profile):
    from ctxpress.core.artifacts import atomic_json
    validate(profile)
    path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    atomic_json(path, profile)
    return path


def pack(curves, lengths, coverage, budget, provenance=None):
    from ctxpress.core.engine import item_type
    if curves.type_fn is not item_type:
        raise ValueError("portable calibrations currently require the standard item_type classifier")
    profile = dict(schema="ctxpress.cost-model", version=1, type_fn="item_type", curves=copy.deepcopy(curves.tab),
                   raw_curves=copy.deepcopy(curves.raw), lengths=list(lengths), truncate_budget=budget,
                   coverage=[dict(form=form, type=kind, rate=rate, count=count)
                             for (form, kind), (rate, count) in sorted(coverage.items())],
                   provenance=provenance or {})
    return validate(profile)
