"""Detached scheduler fixture that starts only offline synthetic job processes."""
import subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ctxpress.harness.evaluation import schedule

def launch(directory, job_id, attempt):
    return subprocess.Popen([sys.executable, str(Path(__file__).with_name('fake_eval_job.py')), str(directory), job_id, str(attempt), '0.4'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

schedule(sys.argv[1], launch=launch, interval=0.02)
