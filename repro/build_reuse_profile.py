"""Write a reuse-memory profile (ctxpress/methods/reuse_memory.py) for one session from finished sessions.

Only sessions that started before --before are used, so a profile never contains the session it is for or anything
after it. Attributes name the session the profile is for (task, task type, language; any attribute the index rows
carry can be added).

    python repro/build_reuse_profile.py --index runs/repro/rollout_index.jsonl --before 2026-10-12T10-00-00 \\
        --task navidrome_navidrome_v0.57.0_v0.58.0 --out runs/prepared/v9/reuse-profile-navidrome.json
"""
from __future__ import annotations
import argparse, hashlib, json, os, sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ctxpress.methods.reuse_memory import profile  # noqa: E402
from memory_reuse_check import TASK_TYPE, language, points, started  # noqa: E402


def attrs_of(row):
    return dict(task=row["task"], task_type=TASK_TYPE.get(row["bench"], row["bench"]),
                lang=language(row["task"] or "", row["bench"]))


def job(row):
    try:
        return row, points(row["path"])["points"]
    except Exception as error:
        return row, error


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True)
    ap.add_argument("--before", required=True, help="only sessions started before this (YYYY-MM-DDTHH-MM-SS)")
    ap.add_argument("--task", required=True)
    ap.add_argument("--bench", default="swe-milestone")
    ap.add_argument("--min-bytes", type=int, default=200_000)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    target = attrs_of(dict(task=a.task, bench=a.bench))
    rows, seen = [], set()
    for line in open(a.index, encoding="utf-8"):
        r = json.loads(line)
        if (r["bench"] not in TASK_TYPE or r["bytes"] < a.min_bytes or not os.path.exists(r["path"])
                or not started(r["path"]) or started(r["path"]) >= a.before):
            continue
        digest = hashlib.sha256(open(r["path"], "rb").read()).hexdigest()
        if digest not in seen:
            seen.add(digest)
            rows.append(r)
    data = []
    with ProcessPoolExecutor(a.workers) as pool:
        for row, pts in pool.map(job, rows, chunksize=1):
            if isinstance(pts, Exception):
                print("skip", row["path"][-80:], repr(pts), flush=True)
                continue
            data.append((attrs_of(row), pts))
    prof = profile(data, target)
    prof["built_from"] = dict(index=a.index, before=a.before, sessions=len(data),
                              same_task=sum(d[0]["task"] == a.task for d in data))
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    json.dump(prof, open(a.out, "w"), indent=0)
    print(json.dumps(dict(prof["built_from"], attrs=target, cells=len(prof["tables"]),
                          bytes=os.path.getsize(a.out))))


if __name__ == "__main__":
    main()
