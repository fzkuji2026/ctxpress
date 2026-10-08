"""LongBench v2 items as Harbor tasks: the agent reads the long context from a file and answers one letter.

Source: the official data file (zai-org/LongBench-v2, a JSON list with _id, question, choice_A..choice_D, answer,
context, domain, difficulty, length). The answer is only in tests/. Grading is the official exact letter match.
This changes the protocol from a single prompt to a file the agent reads with tools; report it as such.
"""
from __future__ import annotations
import json
from pathlib import Path

from ctxpress.benchmarks.convert.common import new_output, write_manifest, write_task

FIELDS = ("_id", "question", "choice_A", "choice_B", "choice_C", "choice_D", "answer", "context")

INSTRUCTION = """Read the document in /workspace/context.txt and answer the multiple-choice question below.
Write only the letter of the correct choice (A, B, C or D) to /workspace/answer.txt.

Question: {question}

A. {choice_A}
B. {choice_B}
C. {choice_C}
D. {choice_D}
"""

VERIFIER = """#!/bin/bash
mkdir -p /logs/verifier
answer=$(tr -d '[:space:]' < /workspace/answer.txt 2>/dev/null | head -c 1 | tr 'abcd' 'ABCD')
expected=$(cat /tests/expected.txt)
if [ "$answer" = "$expected" ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
"""


def items(path):
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("LongBench v2 data must be a nonempty JSON list")
    for row in rows:
        if not isinstance(row, dict) or any(key not in row for key in FIELDS) or row["answer"] not in ("A", "B", "C", "D"):
            raise ValueError("not a LongBench v2 item: " + str(row.get("_id") if isinstance(row, dict) else row)[:80])
    return rows


def convert(source, output, *, image, revision, difficulty=None, length=None, agent_timeout=3600):
    output = new_output(output)
    count = 0
    for row in items(source):
        if difficulty and row.get("difficulty") != difficulty or length and row.get("length") != length:
            continue
        write_task(output, "longbench-v2-" + row["_id"], instruction=INSTRUCTION.format(**row),
                   config=dict(agent=dict(timeout_sec=agent_timeout), verifier=dict(timeout_sec=120), environment={}),
                   environment={"Dockerfile": f"FROM {image}\nCOPY context.txt /workspace/context.txt\n", "context.txt": row["context"]},
                   tests={"test.sh": VERIFIER, "expected.txt": row["answer"] + "\n"})
        count += 1
    if not count:
        raise ValueError("no LongBench v2 items matched the selection")
    write_manifest(output, "longbench-v2", revision, source="zai-org/LongBench-v2", difficulty=difficulty, length=length,
                   converter="ctxpress.benchmarks.convert.longbench", protocol_note="context given as a file, not a single prompt")
    return output
