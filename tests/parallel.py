"""Run the suite in parallel processes, balanced by recorded test durations.

    python tests/parallel.py            # default check
    python tests/parallel.py --all      # also the extended tests (see conftest.EXTENDED_TESTS)
    python tests/parallel.py -j 4 tests/test_rewrite.py   # any extra arguments select tests as in pytest

`tests/durations.json` holds the seconds of tests that took at least one second; others count as 0.2 s.
Refresh it with `--record` after adding slow tests.
"""
import argparse, json, os, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DURATIONS = ROOT / 'tests/durations.json'


def collect(extra, everything):
    out = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'no:cacheprovider',
                          *(['--all'] if everything else []), *(extra or ['tests'])],
                         cwd=ROOT, capture_output=True, text=True)
    nodes = [line for line in out.stdout.splitlines() if '::' in line and not line.startswith(' ')]
    if out.returncode not in (0, 5) or not nodes:
        sys.exit(out.stdout[-3000:] + out.stderr[-3000:])
    return nodes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-j', type=int, default=min(8, os.cpu_count() or 1))
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--record', action='store_true', help='rewrite tests/durations.json from this run')
    args, extra = ap.parse_known_args()
    nodes = collect(extra, args.all)
    known = json.loads(DURATIONS.read_text()) if DURATIONS.exists() else {}
    cost = lambda node: known.get(node, 0.2)
    shards = [[] for _ in range(max(1, min(args.j, len(nodes))))]
    load = [0.0] * len(shards)
    for node in sorted(nodes, key=cost, reverse=True):   # longest first onto the least loaded shard
        k = load.index(min(load)); shards[k].append(node); load[k] += cost(node)
    folder = Path(tempfile.mkdtemp(prefix='ctxpress-tests-'))
    started, procs = time.time(), []
    for k, shard in enumerate(shards):
        (folder / f'{k}.args').write_text('\n'.join(shard) + '\n')
        command = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--all', f'@{folder / f"{k}.args"}']
        if args.record:
            command += ['--durations=0', '--durations-min=1']
        procs.append((subprocess.Popen(command, cwd=ROOT, stdout=open(folder / f'{k}.out', 'w'),
                                       stderr=subprocess.STDOUT), folder / f'{k}.out'))
    failed = False
    for proc, log in procs:
        proc.wait()
        text = log.read_text()
        print(text.strip().splitlines()[-1] if text.strip() else f'{log.name}: no output')
        if proc.returncode not in (0, 5):
            failed = True
            print(''.join(line + '\n' for line in text.splitlines() if line.startswith(('FAILED', 'ERROR'))) or text[-3000:])
    if args.record:
        measured = {}
        for _, log in procs:
            for line in log.read_text().splitlines():
                parts = line.split(None, 2)
                if len(parts) == 3 and parts[0].endswith('s') and parts[1] in ('call', 'setup', 'teardown'):
                    measured[parts[2]] = round(measured.get(parts[2], 0) + float(parts[0][:-1]), 1)
        DURATIONS.write_text(json.dumps(dict(sorted({**known, **measured}.items())), indent=0) + '\n')
    print(f'{len(nodes)} tests in {len(shards)} processes, {time.time() - started:.0f}s; logs in {folder}')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
