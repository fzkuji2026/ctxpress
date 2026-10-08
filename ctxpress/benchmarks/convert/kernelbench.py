"""KernelBench problems as Harbor tasks, graded by KernelBench's own eval_kernel_against_ref.

Source: a checkout of ScalingIntelligence/KernelBench (problems in KernelBench/level<N>/<id>_<name>.py, each a
PyTorch `Model` with get_inputs / get_init_inputs). The agent writes `ModelNew` to /workspace/model_new.py; the
verifier runs the official correctness and timing check on the GPU and reports compiled, correct, speedup
(reference runtime / custom runtime, both from the same official call) and fast_1. The image must contain CUDA, PyTorch
and the KernelBench package (`pip install -e` of the same checkout).
"""
from __future__ import annotations
import re
from pathlib import Path

from ctxpress.benchmarks.convert.common import new_output, write_manifest, write_task

INSTRUCTION = """You are given a PyTorch reference architecture in /workspace/reference.py (also shown below).
Write a faster implementation as a class named `ModelNew` with the same constructor and forward signature, using
custom GPU kernels (for example CUDA via torch.utils.cpp_extension.load_inline) wherever they help. It must
produce the same outputs as `Model` for the inputs from get_inputs() / get_init_inputs().

Save the complete, self-contained Python source (imports, kernels and ModelNew; get_inputs / get_init_inputs may be
kept) to /workspace/model_new.py. It is graded by KernelBench's official check: compilation, correctness over
{trials} randomized trials and runtime against the reference on this GPU.

Reference architecture (level {level}, problem {problem}):

```python
{source}
```
"""

VERIFIER = """import json
from pathlib import Path

out = Path("/logs/verifier"); out.mkdir(parents=True, exist_ok=True)
reward = dict(compiled=0, correct=0, speedup=0.0, fast_1=0)
detail = {}
solution = Path("/workspace/model_new.py")
if solution.is_file():
    from kernelbench.eval import eval_kernel_against_ref
    result = eval_kernel_against_ref(Path("/tests/reference.py").read_text(), solution.read_text(),
                                     num_correct_trials=CORRECT, num_perf_trials=PERF, measure_performance=True)
    reward["compiled"] = int(bool(result.compiled))
    reward["correct"] = int(bool(result.correctness))
    if result.correctness and result.runtime > 0 and result.ref_runtime > 0:
        reward["speedup"] = result.ref_runtime / result.runtime
        reward["fast_1"] = int(reward["speedup"] > 1)
    detail = dict(runtime_us=result.runtime, ref_runtime_us=result.ref_runtime, metadata=result.metadata)
else:
    detail = dict(error="no /workspace/model_new.py")
(out / "reward.json").write_text(json.dumps(reward))
(out / "kernelbench-result.json").write_text(json.dumps(detail, default=str))
"""


def problems(source, levels):
    root = Path(source).expanduser().resolve() / "KernelBench"
    found = []
    for level in levels:
        folder = root / f"level{level}"
        if not folder.is_dir():
            raise FileNotFoundError(folder)
        for path in sorted(folder.glob("*.py")):
            match = re.match(r"(\d+)_", path.name)
            text = path.read_text(encoding="utf-8")
            if not match or "class Model" not in text or "def get_inputs" not in text:
                raise ValueError("not a KernelBench problem file: " + str(path))
            found.append((level, int(match.group(1)), path))
    if not found:
        raise ValueError("no KernelBench problems found under " + str(root))
    return found


def convert(source, output, *, image, revision, levels=(1, 2, 3), correct_trials=5, perf_trials=100,
            agent_timeout=3600, verifier_timeout=1800):
    output = new_output(output)
    tests_script = VERIFIER.replace("CORRECT", str(int(correct_trials))).replace("PERF", str(int(perf_trials)))
    for level, number, path in problems(source, levels):
        text = path.read_text(encoding="utf-8")
        write_task(output, f"kernelbench-l{level}-{number:03d}-{path.stem.split('_', 1)[1]}",
                   instruction=INSTRUCTION.format(trials=correct_trials, level=level, problem=path.stem, source=text),
                   config=dict(agent=dict(timeout_sec=agent_timeout), verifier=dict(timeout_sec=verifier_timeout),
                               environment=dict(gpus=1)),
                   environment={"Dockerfile": f"FROM {image}\nCOPY reference.py /workspace/reference.py\n", "reference.py": path},
                   tests={"test.sh": "#!/bin/bash\nset -e\npython3 /tests/kernelbench_verify.py\n",
                          "kernelbench_verify.py": tests_script, "reference.py": path})
    write_manifest(output, "kernelbench", revision, source="ScalingIntelligence/KernelBench", levels=list(levels),
                   converter="ctxpress.benchmarks.convert.kernelbench", image=image)
    return output
