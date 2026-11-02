"""What an evolved policy module runs as a command: `python3 -m <module> NAME ARG ...` -> TOOLS[NAME](args)."""
from __future__ import annotations
import sys


def cli(tools, argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in tools:
        print("usage: python3 -m <module> {" + ",".join(sorted(tools)) + "} ARG ...")
        return 2
    try:
        out = tools[argv[0]](argv[1:])
    except (OSError, ValueError, KeyError, IndexError) as e:
        print(f"{argv[0]}: {type(e).__name__}: {e}")
        return 1
    if out:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    return 0
