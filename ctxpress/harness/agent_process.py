"""Own a Linux agent process group so cancelled Docker exec cannot leave it running."""
from __future__ import annotations
import argparse, json, os, signal, subprocess, sys, time
from pathlib import Path
from ctxpress.harness import eval_plan
from ctxpress.core import processes


def stop(path):
    path = Path(path)
    if not path.exists():
        return
    record = json.loads(path.read_text(encoding='utf-8'))
    pid, expected = record.get('pid'), record.get('identity')
    if type(pid) is not int or pid <= 1 or not isinstance(expected, str) or not expected.startswith('proc:'):
        raise ValueError('invalid managed agent process identity')
    current = processes.identity(pid)
    if current is not None and current != expected:
        raise ValueError('managed agent PID changed; refusing to signal another process')
    # The group may retain children after its leader exits. Its ID is reserved
    # while any member lives, so signal the recorded group even for a dead leader.
    for action in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, action)
        except ProcessLookupError:
            break
        if action == signal.SIGTERM:
            time.sleep(0.2)
    path.unlink(missing_ok=True)


def run(path, arguments):
    path = Path(path)
    if not sys.platform.startswith('linux') or not arguments or path.exists() or path.is_symlink():
        raise ValueError('agent requires Linux, a command and an unused process record')
    process = subprocess.Popen(arguments, start_new_session=True)
    try:
        expected = processes.identity(process.pid)
        if expected is None:
            # A very short command can exit before its record is written. Its
            # unreaped PID still belongs to this Popen; remove any descendants.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return process.wait()
        eval_plan.atomic_json(path, dict(pid=process.pid, identity=expected))
        return process.wait()
    finally:
        if path.exists():
            stop(path)
        elif process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid-file', required=True)
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.stop:
        stop(args.pid_file)
        return 0
    arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    return run(args.pid_file, arguments)


if __name__ == '__main__':
    raise SystemExit(main())
