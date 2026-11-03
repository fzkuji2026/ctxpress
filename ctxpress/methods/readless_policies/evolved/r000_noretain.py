"""Retention seed (P3/joint): no tool change and no retention rule, i.e. our method alone; a retention-only or joint
lineage starts here and adds retain() / RETAIN."""
from __future__ import annotations
from ctxpress.evolve.runtime import cli

TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
GUIDANCE = ""
RETAIN = {}
TOOLS = {}


def view(cmd, out, ctx):
    return None


def retain(info, ctx):
    return None


if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
