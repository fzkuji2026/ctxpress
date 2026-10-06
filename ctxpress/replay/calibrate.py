"""Fit portable statistics on explicitly selected historical sessions, without model calls."""
from __future__ import annotations
import hashlib
import json
from ctxpress.methods.cost_model import CostModel, ALL_TYPES


def fit(traces, path, truncate_budget=2000, smooth=True):
    traces = list(traces)
    if not traces or any(len(trace.get("reqs", [])) < 2 for trace in traces):
        raise ValueError("fit needs historical sessions with at least two requests each")
    method = CostModel(traces, smooth=smooth, truncate_budget=truncate_budget,
                       ops={kind: ["placeholder", "truncate", "structure"] for kind in ALL_TYPES})
    # Hash inputs for provenance but never put commands, outputs or credentials into the artifact.
    method.provenance.update(input_sha256=[hashlib.sha256(json.dumps(trace, sort_keys=True, ensure_ascii=False,
                                             separators=(",", ":")).encode("utf-8")).hexdigest() for trace in traces],
                             scope="offline statistics; no task-quality guarantee or fitted lambda")
    output = method.save_profile(path)
    return dict(profile=output, sessions=len(traces), requests=sum(method._train_lengths),
                coverage_cells=len(method.cov_rates))
