"""Compare SWE-Pruner's output handling with the downloaded author's code.

This stage checks the adapter, not the model's accuracy or published solve rate.
The author's client and _apply_pruner are compiled from their source files; only
their HTTP response and data containers are substituted. No model/network calls.
Use --records for JSONL rows containing text, query and response from a real model.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods.swepruner import SWEPruner


def author_functions(original):
    src = original / "downstream_eval/multi_turn/swebench/mini-swe-agent--with-pruning/src/minisweagent"
    paths = [src / "agents/default.py", src / "utils/pruner.py"]
    wanted = ["_apply_pruner", "prune"]
    env = {"PrunerRequest": SimpleNamespace,
           "PruneResponse": lambda **kwargs: SimpleNamespace(**({"error_msg": None} | kwargs)),
           "logger": SimpleNamespace(debug=lambda *args: None)}
    hashes = {}
    for path, name in zip(paths, wanted):
        raw = path.read_bytes()
        hashes[str(path.relative_to(original))] = hashlib.sha256(raw).hexdigest()
        tree = ast.parse(raw.decode("utf-8"))
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        # Future annotations avoid importing the author's agent/LLM dependencies.
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(path), "exec"), env)
    return env["_apply_pruner"], env["prune"], hashes


def cases():
    for n in (0, 1, 500, 501, 1300):
        for query in (None, "where is the timeout handled?"):
            for mode in ("filtered", "all", "error", "exception"):
                yield dict(text=("x\n" * (n // 2) + "x" * (n % 2)), query=query, mode=mode)


def compare_case(case, apply, client_prune):
    calls = [[], []]
    response = case.get("response") or dict(score=0.5, pruned_code="kept line\n", token_scores=[],
                                           kept_frags=[1], origin_token_cnt=100, left_token_cnt=10,
                                           model_input_token_cnt=120, error_msg=None)
    response = dict(response)
    if case.get("mode") == "all":
        response["left_token_cnt"] = response["origin_token_cnt"]
    if case.get("mode") == "error":
        response["error_msg"] = "model unavailable"

    def service(index, payload):
        calls[index].append(payload)
        if case.get("mode") == "exception":
            raise RuntimeError("model unavailable")
        return dict(response)

    def post(url, json, timeout):
        result = service(0, json)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: result)

    config = SimpleNamespace(url="http://unused", threshold=0.5, timeout=60, retries=1,
                             min_chars=500, chunk_overlap_tokens=50)
    client = SimpleNamespace(config=config, session=SimpleNamespace(post=post))
    # PrunerRequest.model_dump in the original is equivalent to these fields.
    def original_prune(req):
        req.model_dump = lambda: {k: v for k, v in vars(req).items() if k != "model_dump"}
        return client_prune(client, req)
    client.prune = original_prune
    output = {"output": case["text"]}
    apply(SimpleNamespace(pruner_client=client), {"context_focus_question": case.get("query")}, output)

    def ours(query, code, threshold, overlap):
        return service(1, dict(query=query, code=code, threshold=threshold,
                               always_keep_first_frags=False, chunk_overlap_tokens=overlap))

    command = "cat source.py"
    if case.get("query"):
        command += " # context_focus_question: " + case["query"]
    rw = Rewriter(lambda: SWEPruner(pruner=ours))
    body, _ = rw.rewrite_body({"input": [
        {"type": "message", "role": "user", "content": "task"},
        {"type": "function_call", "call_id": "a", "name": "exec_command", "arguments": json.dumps({"cmd": command})},
        {"type": "function_call_output", "call_id": "a", "output": case["text"]},
    ]}, "s")
    got = next(x["output"] for x in body["input"] if x["type"] == "function_call_output")
    return dict(same=got == output["output"], calls_same=calls[0] == calls[1],
                model_calls=len(calls[1]), mode=case.get("mode", "recorded"), chars=len(case["text"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--original", type=Path, default=ROOT.parent / "data/repro/swe-pruner")
    ap.add_argument("--records", type=Path, help="JSONL: text, query, response (model outputs)")
    ap.add_argument("--output", type=Path)
    a = ap.parse_args(argv)
    apply, prune, hashes = author_functions(a.original)
    inputs = (json.loads(line) for line in a.records.read_text(encoding="utf-8").splitlines()) if a.records else cases()
    rows = [compare_case(c, apply, prune) for c in inputs]
    report = dict(scope="author output handling and live Responses adapter; model responses supplied",
                  cases=len(rows), same=sum(r["same"] for r in rows),
                  calls_same=sum(r["calls_same"] for r in rows), source_sha256=hashes,
                  differences=[r for r in rows if not r["same"] or not r["calls_same"]])
    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return int(bool(report["differences"]))


if __name__ == "__main__":
    raise SystemExit(main())
