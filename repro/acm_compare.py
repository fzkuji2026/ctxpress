"""Compare ACM archive boundaries/checkpoint retention with pinned local author code.

Both sides receive deterministic fake summaries. We compare normalized tool
pairs and archived content, not prompts, token hints, real summaries or the 9B
policy. Author code is used only as the test oracle, never as a runtime backend.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import ACM
from repro.sources import verify_source

SUMMARY = "Synthetic memory: findings, pending work and sources retained."
EMPTY_ERRORS = ("Error: nothing to compress.",
                "Error: nothing to compress. There are no messages between your previous manage_context call and this one.")


def normalize(rows):
    for row in rows:
        if row[0] == "out" and row[2] in EMPTY_ERRORS:
            row[2] = "EMPTY_RANGE"
    return rows


def functions(original):
    verify_source('acm', original)
    env = dict(copy=copy, os=os, json=json, logger=logging.getLogger("acm-comparison"),
               call_summarizer=lambda *args, **kwargs: (SUMMARY, True))
    sources = [(original / "src/history.py", {"HistoryManager"}),
               (original / "src/runner.py", {"compress_messages", "_handle_manage_context"})]
    hashes = {}
    for path, names in sources:
        raw = path.read_bytes()
        hashes[str(path.relative_to(original))] = hashlib.sha256(raw).hexdigest()
        nodes = [n for n in ast.parse(raw.decode()).body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names]
        if len(nodes) != len(names):
            raise ValueError("author source does not contain expected functions")
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + nodes,
                            type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(path), "exec"), env)
    return env, hashes


def canonical_author(messages):
    result = []
    for m in messages:
        if m.get("tool_calls"):
            for c in m["tool_calls"]:
                result.append(["call", c["id"], c["function"]["name"], json.loads(c["function"]["arguments"])])
        elif m["role"] == "tool":
            result.append(["out", m["tool_call_id"], m["content"]])
        else:
            result.append(["msg", m["role"], m.get("content")])
    return normalize(result)


def canonical_ours(items, archive=False):
    result = []
    for x in items:
        kind = x.get("kind") if archive else x.get("type")
        if kind in ("call", "function_call"):
            result.append(["call", x["call_id"], x["name"], json.loads(x.get("arguments") or "{}")])
        elif kind in ("out", "function_call_output"):
            result.append(["out", x["call_id"], x["content"] if archive else x["output"]])
        elif x.get("role") != "developer":  # adapter's method instructions, absent in author runner
            result.append(["msg", x.get("role"), x.get("content")])
    return normalize(result)


def compare(original, sessions=20, rounds=25):
    env, hashes = functions(original)
    cases, matches, archives, archive_matches = 0, 0, 0, 0
    client = SimpleNamespace(count_tokens=lambda *args, **kwargs: 1,
                             build_tool_message=lambda text, tool_call_id=None: dict(role="tool", content=text, tool_call_id=tool_call_id))
    service = SimpleNamespace(summarize_prompt=lambda *args, **kwargs: SUMMARY)
    with tempfile.TemporaryDirectory(prefix="ctxpress-acm-compare-") as directory:
        root = Path(directory)
        for seed in range(sessions):
            rng = random.Random(seed)
            hm = env["HistoryManager"]()
            hm.add_message("user", "TASK")
            boundary, sid = 1, 0
            author_store = root / str(seed) / "author"
            rw = Rewriter(ACM, summarizer=service, store_dir=str(root / str(seed) / "ours"))
            items = [dict(role="user", content="TASK")]
            for i in range(rounds):
                cid = f"c{i}"
                name = "manage_context" if i % 7 == 0 or rng.random() < .25 else "read"
                args = {} if name == "manage_context" else {"path": f"file-{i}.py"}
                call = dict(id=cid, type="function", function=dict(name=name, arguments=json.dumps(args)))
                hm.add_message_dict(dict(role="assistant", content=None, tool_calls=[call]))
                output = f"evidence-{seed}-{i} 中文 😀"
                if name == "manage_context":
                    previous = sid
                    output, boundary, sid, _ = env["_handle_manage_context"](
                        client, hm, "TASK", args, [], str(author_store), None, "task", "answer", "test", i,
                        "fake", "synthetic", [], i, boundary, sid, tool_call_id=cid)
                else:
                    hm.add_message_dict(client.build_tool_message(output, tool_call_id=cid))
                items.extend([dict(type="function_call", call_id=cid, name=name, arguments=json.dumps(args)),
                              dict(type="function_call_output", call_id=cid, output="recorded" if name == "manage_context" else output)])
                actual, _ = rw.rewrite_body(dict(input=items), "s")
                a, b = canonical_author(hm.messages), canonical_ours(actual["input"])
                cases += 1
                matches += a == b
                if name == "manage_context" and sid > previous:
                    author_raw = json.loads((author_store / f"summary_{sid}.json").read_text())
                    ours_path = rw.sessions["s"].ctx.method.archives[sid][0]
                    ours_raw = json.loads(ours_path.read_text())
                    archives += 1
                    archive_matches += canonical_author(author_raw) == canonical_ours(ours_raw, archive=True)
    return dict(scope="synthetic ACM sequential compression; normalized tool-pair history and archives",
                reference_revision="f06f90e728af8580a4515812425c1620144145a2", source_sha256=hashes,
                cases=cases, matches=matches, archives=archives, archive_matches=archive_matches,
                passed=cases > 0 and cases == matches and archives == archive_matches,
                limitations=["Fake identical summary text on both sides; prompts and summary quality not compared.",
                    "Role/envelope normalization and empty-range error normalization are explicit.",
                    "Query-memory, restart behavior, parallel calls, token hints and trained policy are outside this comparison."])


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
