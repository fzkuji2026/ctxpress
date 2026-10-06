"""Offline subprocess fixture for scheduler durability, never a benchmark runner."""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ctxpress.harness.evaluation import database, set_result

directory, job_id, attempt, delay = sys.argv[1:5]
if len(sys.argv) > 5:
    release = Path(sys.argv[5])
    deadline = time.monotonic() + 60
    while not release.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('scheduler fixture was not released')
        time.sleep(0.01)
time.sleep(float(delay))
usage = dict(api_input_tokens=120, api_cached_tokens=50, api_output_tokens=10, summary_calls=1)
result = dict(test_only=True, stop="submitted", grade=dict(resolved=True, infra_invalid=False), usage=usage,
              requests=1, seconds=0.1, rewrites=[dict(request=1, usage=dict(input_tokens=100, cached_tokens=50, output_tokens=5)),
                                             dict(type="summary", usage=dict(input_tokens=20, cached_tokens=0, output_tokens=5))])
set_result(directory, job_id, int(attempt), result=result)
