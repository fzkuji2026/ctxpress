"""Convert an official benchmark release on disk into Harbor tasks.

    python -m ctxpress.benchmarks.convert kernelbench --source KernelBench/ --output tasks/ --image <cuda-image> --revision <tag>
    python -m ctxpress.benchmarks.convert longbench-v2 --source data.json --output tasks/ --image <image> --revision <rev>
    python -m ctxpress.benchmarks.convert swe-qa --source SWE-QA-Bench/ --output tasks/ --image <image> --revision <commit>
    python -m ctxpress.benchmarks.convert officebench --source OfficeBench/ --output tasks/ --image <officebench-image> --revision <commit>
    python -m ctxpress.benchmarks.convert recovery-bench --tasks terminal-bench-2/ --traces runs/initial-.../
        --checkout recovery-bench/ --output tasks/ --revision <commit> [--message-mode full|summary|none]

The output directory is then the `environment.data` of an evaluation plan for the same benchmark name.
"""
from __future__ import annotations
import argparse


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m ctxpress.benchmarks.convert")
    sub = ap.add_subparsers(dest="benchmark", required=True)
    p = sub.add_parser("recovery-bench")
    for flag in ("--tasks", "--traces", "--checkout", "--output", "--revision"):
        p.add_argument(flag, required=True)
    p.add_argument("--message-mode", default="full", choices=["full", "summary", "none"])
    for name in ("kernelbench", "longbench-v2", "swe-qa", "officebench"):
        p = sub.add_parser(name)
        p.add_argument("--source", required=True, help="official release already on disk")
        p.add_argument("--output", required=True, help="new or empty directory for the Harbor tasks")
        p.add_argument("--image", required=True, help="prepared base image for every task's environment")
        p.add_argument("--revision", required=True, help="official release identifier (tag, commit or dataset revision)")
        if name == "kernelbench":
            p.add_argument("--levels", type=int, nargs="+", default=[1, 2, 3])
        if name == "longbench-v2":
            p.add_argument("--difficulty", choices=["easy", "hard"]); p.add_argument("--length", choices=["short", "medium", "long"])
        if name == "swe-qa":
            p.add_argument("--projects", nargs="+")
    a = ap.parse_args(argv)
    if a.benchmark == "recovery-bench":
        from ctxpress.benchmarks.convert.recovery import convert
        print(convert(a.tasks, a.traces, a.checkout, a.output, revision=a.revision, message_mode=a.message_mode))
        return 0
    if a.benchmark == "officebench":
        from ctxpress.benchmarks.convert.officebench import convert
        print(convert(a.source, a.output, image=a.image, revision=a.revision))
        return 0
    if a.benchmark == "kernelbench":
        from ctxpress.benchmarks.convert.kernelbench import convert
        out = convert(a.source, a.output, image=a.image, revision=a.revision, levels=tuple(a.levels))
    elif a.benchmark == "longbench-v2":
        from ctxpress.benchmarks.convert.longbench import convert
        out = convert(a.source, a.output, image=a.image, revision=a.revision, difficulty=a.difficulty, length=a.length)
    else:
        from ctxpress.benchmarks.convert.sweqa import convert
        out = convert(a.source, a.output, image=a.image, revision=a.revision,
                      projects=[p.lower() for p in a.projects] if a.projects else None)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
