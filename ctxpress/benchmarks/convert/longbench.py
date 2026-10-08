"""LongBench v2 items as Harbor tasks: the official 0-shot prompt, with the long text in a file the agent reads.

Source: the official data file (zai-org/LongBench-v2, a JSON list with _id, question, choice_A..choice_D, answer,
context, domain, difficulty, length). The prompt is THUDM/LongBench prompts/0shot.txt with $DOC$ replaced by a
pointer to /workspace/context.txt; the answer is only in tests/ and is extracted from the agent's response with
pred.py's extract_answer. Agentic context-management papers evaluate it this way (ARC 2607.25066: hard subset,
311 items), since the text must reach the agent through tool observations for compaction to act on it.
"""
from __future__ import annotations
import json
from pathlib import Path

from ctxpress.benchmarks.convert.common import new_output, write_manifest, write_task

FIELDS = ("_id", "question", "choice_A", "choice_B", "choice_C", "choice_D", "answer", "context")

# prompts/0shot.txt; the only change is where the text is.
INSTRUCTION = """Please read the following text and answer the question below.

<text>
The text is in the file /workspace/context.txt ({chars} characters). Read it from there.
</text>

What is the correct answer to this question: {question}
Choices:
(A) {choice_A}
(B) {choice_B}
(C) {choice_C}
(D) {choice_D}

Format your response as follows: "The correct answer is (insert answer here)".
Write that response to /workspace/answer.txt.
"""

# pred.py extract_answer, applied to the response file.
VERIFIER = r"""import re
from pathlib import Path

def extract_answer(response):
    response = response.replace('*', '')
    match = re.search(r'The correct answer is \(([A-D])\)', response)
    if match:
        return match.group(1)
    else:
        match = re.search(r'The correct answer is ([A-D])', response)
        if match:
            return match.group(1)
        else:
            return None

out = Path("/logs/verifier"); out.mkdir(parents=True, exist_ok=True)
path = Path("/workspace/answer.txt")
pred = extract_answer(path.read_text(errors="replace")) if path.is_file() else None
expected = Path("/tests/expected.txt").read_text().strip()
(out / "reward.txt").write_text("1\n" if pred == expected else "0\n")
(out / "prediction.txt").write_text(str(pred) + "\n")
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
        fields = {k: str(row[k]).strip() for k in FIELDS[1:6]}               # pred.py strips each field
        write_task(output, "longbench-v2-" + row["_id"], instruction=INSTRUCTION.format(chars=len(row["context"]), **fields),
                   config=dict(agent=dict(timeout_sec=agent_timeout), verifier=dict(timeout_sec=120), environment={}),
                   environment={"Dockerfile": f"FROM {image}\nCOPY context.txt /workspace/context.txt\n", "context.txt": row["context"]},
                   tests={"test.sh": "#!/bin/bash\npython3 /tests/longbench_verify.py\n", "longbench_verify.py": VERIFIER,
                          "expected.txt": row["answer"] + "\n"})
        count += 1
    if not count:
        raise ValueError("no LongBench v2 items matched the selection")
    write_manifest(output, "longbench-v2", revision, source="zai-org/LongBench-v2", difficulty=difficulty, length=length,
                   converter="ctxpress.benchmarks.convert.longbench", prompt="THUDM/LongBench prompts/0shot.txt",
                   answer_extraction="THUDM/LongBench pred.py extract_answer",
                   protocol_note="the long text is a file the agent reads, not inline in the prompt")
    return output
