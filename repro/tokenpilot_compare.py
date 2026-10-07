"""Compare selection with LightRSI's unmodified TypeScript task-registry analyzer.

Requires a local Node >=22.7; installs nothing. Synthetic typed history blocks,
not trajectories or model judgments. No model or network calls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ctxpress.methods.tokenpilot_lifecycle import eviction_candidates
from repro.sources import verify_source


def cases(seed=17, n=300):
    rng = random.Random(seed)
    for _ in range(n):
        blocks = []
        tasks = ["a", "b", "c"]
        registry = dict(evictableTaskIds=rng.sample(tasks, rng.randrange(4)),
                        turnToTaskIds={str(i): rng.sample(tasks, rng.randrange(4)) for i in range(5)},
                        blockToTaskIds={str(i): rng.sample(tasks, rng.randrange(4)) for i in range(5)})
        for i in range(rng.randrange(1, 15)):
            blocks.append(dict(blockId=str(i), segmentIds=[str(i), str(i + 1)], blockType="tool",
                               charCount=rng.choice([0, 255, 256, 257, 2000]), approxTokens=30,
                               turnAbsIds=[str(i % 5)], taskIds=rng.sample(tasks, rng.randrange(3)),
                               metadata=dict(eviction=dict(skip=rng.random() < .1, archived=rng.random() < .2))))
        yield dict(blocks=blocks, registry=registry, config=dict(enabled=rng.random() > .1,
                    policy=rng.choice(["noop", "archive"]), minBlockChars=rng.choice([0, 256, 300])))


def compare(original, node, workdir, n=300):
    verify_source('tokenpilot', original)
    source = original / "components/packages/features/eviction/src/planning/analyzer.ts"
    workdir.mkdir(parents=True, exist_ok=True)
    samples = list(cases(n=n))
    with tempfile.TemporaryDirectory(dir=workdir) as directory:
        temp = Path(directory)
        shutil.copyfile(source, temp / "analyzer.ts")
        (temp / "driver.mjs").write_text('import fs from "node:fs";\nimport {analyzeEvictionFromTaskRegistry as run} from "./analyzer.ts";\n'
            'const cases=JSON.parse(fs.readFileSync(0,"utf8"));\n'
            'process.stdout.write(JSON.stringify(cases.map(c=>run(c.blocks,c.registry,c.config).instructions.map(i=>i.blockId))));\n', encoding="utf-8")
        result = subprocess.run([node, "--experimental-transform-types", "--no-warnings", "driver.mjs"], cwd=temp,
                                input=json.dumps(samples), text=True, encoding="utf-8", capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr[-2000:])
    actual = json.loads(result.stdout)
    if len(actual) != len(samples):
        raise ValueError("author result count mismatch")
    matches = [ids == eviction_candidates(c["blocks"], c["registry"], enabled=c["config"]["enabled"],
        policy=c["config"]["policy"], min_chars=c["config"]["minBlockChars"]) for c, ids in zip(samples, actual)]
    return dict(scope="synthetic task-registry eviction selection; author TypeScript executed unchanged",
                reference_revision="9f0f19308a30445438779efbf3108a7d965a55c7", cases=n, matches=sum(matches),
                passed=bool(matches) and all(matches), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                limitations=["No model-generated lifecycle judgments, ingestion pass or full runtime comparison.",
                              "ctxpress additionally protects latest outputs and blocks with active co-owners."])


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--original", type=Path, required=True)
    p.add_argument("--node", default="node")
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args(argv)
    report = compare(a.original, a.node, a.output.parent)
    with a.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report, indent=2))
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
