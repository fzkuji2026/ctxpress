"""KernelBench problems as Harbor tasks, graded by KernelBench's own eval_kernel_against_ref.

Source: a checkout of ScalingIntelligence/KernelBench (problems in KernelBench/level<N>/<id>_<name>.py, each a
PyTorch `Model` with get_inputs / get_init_inputs). The image must contain CUDA, PyTorch and the KernelBench package
(`pip install -e` of the same checkout). Two protocols:

official   one kernel: the agent writes `ModelNew` to /workspace/model_new.py; the verifier runs the official
           correctness and timing check and reports compiled, correct, speedup (reference / custom runtime from the
           same official call) and fast_1.
continual  CliffCompaction's (2609.26779, appendix D.2): Level 3, the agent keeps optimizing and every candidate
           it evaluates is archived; the verifier re-evaluates all of them (5 correctness inputs, 10 timed runs,
           atol = rtol = 1e-2 for CUDA, 5e-2 for Triton; CUDA kernels may not fall back on PyTorch compute
           operators, by the official static checker) and scores the best speedup, clamped to 10x, or 0.1 when
           no candidate is correct. The benchmark score is the geometric mean of `score` over the problems.
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

CONTINUAL = """You are given a PyTorch reference architecture in /workspace/reference.py (also shown below).
Optimize it for maximum speedup over the PyTorch eager reference by writing `ModelNew`, a class with the same
constructor and forward signature, implemented with {kernels}. It must produce the same outputs as `Model` for the
inputs from get_inputs() / get_init_inputs().

Evaluate every candidate with `python3 /workspace/evaluate_kernel.py <file>`: it runs KernelBench's official check
(compilation, correctness on {trials} random inputs, runtime against the reference) and archives the candidate.
Your score is the best correct candidate among those you evaluated, so keep proposing new optimizations and
evaluating them; do not stop after the first working kernel.

Reference architecture (level {level}, problem {problem}):

```python
{source}
```
"""

KERNELS = dict(cuda="raw CUDA kernels (for example via torch.utils.cpp_extension.load_inline); PyTorch compute "
                    "operators may not be used in the forward pass",
               triton="Triton kernels")

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

# Shared by the agent's evaluation helper and the verifier, each from its own copy.
PROTOCOL = '''"""KernelBench official check with the continual-protocol settings."""
import hashlib, json, time
from pathlib import Path

BACKEND, TOLERANCE, CORRECT, PERF, NO_TORCH_OPS = {backend!r}, {tolerance!r}, {correct}, {perf}, {no_torch_ops}


def evaluate(reference, source):
    import kernelbench.eval as official
    from kernelbench.kernel_static_checker import STRICT_CHECKS, WARNING_CHECKS, validate_kernel_static
    forbidden = list(STRICT_CHECKS) + (["torch_computation_ops"] if NO_TORCH_OPS else [])
    valid, errors, warnings = validate_kernel_static(source, backend=BACKEND, precision="fp32", forbidden=forbidden,
                                                     warnings=[w for w in WARNING_CHECKS if w not in forbidden])
    if not valid:
        return dict(compiled=False, correct=False, speedup=0.0, static_errors=errors)
    official.get_tolerance_for_precision = lambda precision: TOLERANCE      # the paper's atol = rtol
    result = official.eval_kernel_against_ref(reference, source, num_correct_trials=CORRECT, num_perf_trials=PERF,
                                              measure_performance=True, backend=BACKEND)
    speedup = result.ref_runtime / result.runtime if result.correctness and result.runtime > 0 and result.ref_runtime > 0 else 0.0
    return dict(compiled=bool(result.compiled), correct=bool(result.correctness), speedup=speedup,
                runtime_us=result.runtime, ref_runtime_us=result.ref_runtime, metadata=str(result.metadata)[:2000])


def archive(source, folder):
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(source.encode()).hexdigest()[:16]
    path = folder / f"{{time.time_ns()}}-{{digest}}.py"
    path.write_text(source)
    return path
'''

HELPER = """import json, sys
from pathlib import Path
sys.path.insert(0, "/workspace/.kernelbench")
from kb_protocol import archive, evaluate

source = Path(sys.argv[1]).read_text()
archive(source, "/workspace/candidates")
print(json.dumps(evaluate(Path("/workspace/reference.py").read_text(), source), indent=1))
"""

CONTINUAL_VERIFIER = """import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, "/tests")
from kb_protocol import evaluate

out = Path("/logs/verifier"); out.mkdir(parents=True, exist_ok=True)
reference = Path("/tests/reference.py").read_text()
paths = sorted(Path("/workspace/candidates").glob("*.py")) + [Path("/workspace/model_new.py")]
seen, results = set(), []
for path in paths:
    if not path.is_file():
        continue
    source = path.read_text(errors="replace")
    digest = hashlib.sha256(source.encode()).hexdigest()
    if digest in seen:
        continue
    seen.add(digest)
    try:
        result = evaluate(reference, source)
    except Exception as error:
        result = dict(compiled=False, correct=False, speedup=0.0, error=repr(error)[:500])
    results.append(dict(file=path.name, sha256=digest, **result))
correct = [r["speedup"] for r in results if r["correct"]]
best = max(correct) if correct else 0.0
reward = dict(compiled=int(any(r["compiled"] for r in results)), correct=int(bool(correct)), speedup=best,
              fast_1=int(best > 1), score=min(best, 10.0) if correct else 0.1, candidates=len(results))
(out / "reward.json").write_text(json.dumps(reward))
(out / "kernelbench-candidates.json").write_text(json.dumps(results, default=str))
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


def convert(source, output, *, image, revision, protocol="official", levels=None, backend="cuda",
            correct_trials=5, perf_trials=None, agent_timeout=None, verifier_timeout=None):
    if protocol not in ("official", "continual"):
        raise ValueError("protocol is official or continual")
    if backend not in KERNELS:
        raise ValueError("backend is cuda or triton")
    continual = protocol == "continual"
    levels = tuple(levels or ((3,) if continual else (1, 2, 3)))
    perf_trials = perf_trials or (10 if continual else 100)
    agent_timeout = agent_timeout or (28800 if continual else 3600)
    verifier_timeout = verifier_timeout or (14400 if continual else 1800)
    tolerance = 1e-2 if backend == "cuda" else 5e-2
    output = new_output(output)
    shared = PROTOCOL.format(backend=backend, tolerance=tolerance, correct=int(correct_trials), perf=int(perf_trials),
                             no_torch_ops=backend == "cuda")
    official = VERIFIER.replace("CORRECT", str(int(correct_trials))).replace("PERF", str(int(perf_trials)))
    for level, number, path in problems(source, levels):
        text = path.read_text(encoding="utf-8")
        values = dict(trials=correct_trials, level=level, problem=path.stem, source=text, kernels=KERNELS[backend])
        environment = {"Dockerfile": f"FROM {image}\nCOPY reference.py /workspace/reference.py\n", "reference.py": path}
        tests = {"reference.py": path}
        if continual:
            environment["Dockerfile"] += ("COPY kb_protocol.py /workspace/.kernelbench/kb_protocol.py\n"
                                          "COPY evaluate_kernel.py /workspace/evaluate_kernel.py\n")
            environment.update({"kb_protocol.py": shared, "evaluate_kernel.py": HELPER})
            tests.update({"test.sh": "#!/bin/bash\nset -e\npython3 /tests/kernelbench_verify.py\n",
                          "kernelbench_verify.py": CONTINUAL_VERIFIER, "kb_protocol.py": shared})
        else:
            tests.update({"test.sh": "#!/bin/bash\nset -e\npython3 /tests/kernelbench_verify.py\n",
                          "kernelbench_verify.py": official})
        write_task(output, f"kernelbench-l{level}-{number:03d}-{path.stem.split('_', 1)[1]}",
                   instruction=(CONTINUAL if continual else INSTRUCTION).format(**values),
                   config=dict(agent=dict(timeout_sec=agent_timeout), verifier=dict(timeout_sec=verifier_timeout),
                               environment=dict(gpus=1)),
                   environment=environment, tests=tests)
    write_manifest(output, "kernelbench", revision, source="ScalingIntelligence/KernelBench", levels=list(levels),
                   protocol=protocol, backend=backend if continual else None,
                   tolerance=tolerance if continual else "official default", correct_trials=correct_trials,
                   perf_trials=perf_trials, converter="ctxpress.benchmarks.convert.kernelbench", image=image,
                   aggregate="geometric mean of score" if continual else None)
    return output
