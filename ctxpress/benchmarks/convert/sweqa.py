"""SWE-QA questions as Harbor tasks: the agent studies the repository at its pinned commit and writes an answer.

Source: a checkout of peng-weihan/SWE-QA-Bench (Benchmark/<project>.jsonl and repo_commit.txt with clone URLs and
commits). The image is built from the pinned commit. The official score is an LLM judge (five dimensions, 1-5);
the verifier here only collects the answer with its reference for that judge (`answered` reward) and never claims a
quality score. Field names are read from the release (question / answer); an unknown layout fails loudly.
"""
from __future__ import annotations
import json, re
from pathlib import Path

from ctxpress.benchmarks.convert.common import new_output, write_manifest, write_task

QUESTION_KEYS = ("question", "query")
ANSWER_KEYS = ("answer", "reference_answer", "ground_truth")

INSTRUCTION = """The repository {repo} is checked out at /workspace/repo (commit {commit}).
Answer the following question about this codebase. Study the code as needed; do not modify it.
Write your final answer in Markdown to /workspace/answer.md.

Question: {question}
"""

VERIFIER = """#!/bin/bash
mkdir -p /logs/verifier
if [ -s /workspace/answer.md ]; then
  cp /workspace/answer.md /logs/verifier/answer.md
  cp /tests/reference.json /logs/verifier/reference.json
  echo '{"answered": 1}' > /logs/verifier/reward.json
else
  echo '{"answered": 0}' > /logs/verifier/reward.json
fi
"""


def repositories(path):
    """repo_commit.txt: each line has a clone URL and a 40-hex commit."""
    found = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        url = next((token for token in line.split() if token.startswith(("http://", "https://", "git@"))), None)
        commit = next((token for token in line.split() if re.fullmatch(r"[0-9a-f]{40}", token)), None)
        if url and commit:
            name = url.rstrip("/").removesuffix(".git").split("/")[-1].lower()
            found[name] = (url, commit)
    if not found:
        raise ValueError("repo_commit.txt has no clone URL + commit lines")
    return found


def pick(row, keys, what):
    for key in keys:
        if isinstance(row.get(key), str) and row[key].strip():
            return row[key]
    raise ValueError(f"SWE-QA item without a {what} field ({', '.join(keys)}): keys {sorted(row)}")


def convert(source, output, *, image, revision, projects=None, agent_timeout=3600):
    source = Path(source).expanduser().resolve()
    repos = repositories(source / "repo_commit.txt")
    output = new_output(output)
    count = 0
    for path in sorted((source / "Benchmark").glob("*.jsonl")):
        project = path.stem.lower()
        if projects and project not in projects:
            continue
        if project not in repos:
            raise ValueError("no pinned commit for project " + project)
        url, commit = repos[project]
        for number, line in enumerate(l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()):
            row = json.loads(line)
            question, answer = pick(row, QUESTION_KEYS, "question"), pick(row, ANSWER_KEYS, "answer")
            write_task(output, f"swe-qa-{project}-{number:04d}",
                       instruction=INSTRUCTION.format(repo=project, commit=commit, question=question),
                       config=dict(agent=dict(timeout_sec=agent_timeout), verifier=dict(timeout_sec=120), environment={}),
                       environment={"Dockerfile": f"FROM {image}\nRUN git clone {url} /workspace/repo && "
                                                  f"git -C /workspace/repo checkout {commit}\n"},
                       tests={"test.sh": VERIFIER, "reference.json": json.dumps(dict(question=question, answer=answer,
                                                                                    project=project, item=row), ensure_ascii=False)})
            count += 1
    if not count:
        raise ValueError("no SWE-QA questions found")
    write_manifest(output, "swe-qa", revision, source="peng-weihan/SWE-QA-Bench", projects=sorted(projects or repos),
                   converter="ctxpress.benchmarks.convert.sweqa",
                   grading_note="official LLM-judge scoring runs separately on the collected answers")
    return output
