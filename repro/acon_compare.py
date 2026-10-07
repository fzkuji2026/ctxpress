"""Offline comparison of ACON's optimizer protocol against local author source.

No model is called. Checks prompt variables/templates, output parsing and token
threshold decisions; does NOT claim trained compressor or task-score parity.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ctxpress.methods import acon_prompts
from ctxpress.methods.acon_source import history_args, observation_args, parse_output, needs_summary, render
from repro.sources import verify_source


def extract(path, cls, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parent = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    node = next(n for n in parent.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    env = {}
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), env)
    return env[name]


def compare(original):
    verify_source('acon', original)
    from jinja2 import Environment
    src = original / "src/productive_agents/ctxopt"
    prompt = original / "experiments/appworld/prompts/context_opt"
    paths = [src / name for name in ("base.py", "history_optimizer.py", "obs_optimizer.py")]
    parse = extract(paths[0], "BaseContextOptimizer", "parse_output")
    hist = extract(paths[1], "HistoryOptimizer", "_build_history_prompt")
    obs = extract(paths[2], "ObservationOptimizer", "_build_optimization_prompt")
    hist_needed = extract(paths[1], "HistoryOptimizer", "check_summarization_needed")
    obs_needed = extract(paths[2], "ObservationOptimizer", "check_summarization_needed")
    author = SimpleNamespace(debug_mode=False, history_template="history", user_template="observation",
                             render_template=lambda name, **kw: json.dumps(kw, ensure_ascii=False), count_tokens=lambda t: len(t) // 4)
    cases = []
    for text in ("", "brief", "中文 😀\n" * 40):
        for previous in (None, "", "previous {{ task }}"):
            cases.append(hist(author, "TASK", text, previous)[1] == history_args("TASK", text, previous))
            for threshold in (-1, 0, len(text) // 4, len(text) // 4 + 1):
                author.history_summarization_threshold = threshold
                author.obs_summarization_threshold = threshold
                cases.append(hist_needed(author, text, previous) == needs_summary(text, threshold, author.count_tokens, previous))
                cases.append(obs_needed(author, text) == needs_summary(text, threshold, author.count_tokens))
        for extra in ({}, {"task": "override", "extra": 2}):
            cases.append(obs(author, "TASK", text, "HISTORY", extra)[1] == observation_args("TASK", text, "HISTORY", extra))
        for marker in ("# History Summary", "# Refined Observation"):
            for response in (text, marker + text, "reasoning\n" + marker + text + "\n" + marker):
                cases.append(parse(author, response, marker) == parse_output(response, marker))
    for name, ours in (("system_prompt", acon_prompts.SYSTEM), ("prompt_user", acon_prompts.OBSERVATION),
                       ("prompt_history_v2", acon_prompts.HISTORY)):
        path = prompt / (name + ".jinja")
        paths.append(path)
        cases.append(path.read_text(encoding="utf-8") == ours)
        for value in ("", "中文 😀\n", "{{ history }} {# text #} {% not code %}\n"):
            args = dict(task=value, prev_summary=value, history=value, observation=value)
            cases.append(Environment().from_string(path.read_text(encoding="utf-8")).render(**args) == render(ours, args))
    return dict(scope="ACON optimizer protocol and AppWorld templates; synthetic inputs; no model or task execution",
                reference_revision="d63f9ae18959dc7215ff62899c94c5e8c56847ae", cases=len(cases), matches=sum(cases),
                passed=all(cases), source_sha256={str(p.relative_to(original)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
                limitations=["Token counter replaced equally on both sides.", "No optimized guideline search or distillation.",
                              "Host history serialization and preservation are adaptations, not compared author behavior."])


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--original", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    a = p.parse_args(argv)
    report = compare(a.original)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report, indent=2))
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
