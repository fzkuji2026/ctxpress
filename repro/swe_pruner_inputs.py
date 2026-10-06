"""Recover a conservative subset of SWE-Pruner's published inputs.

Only simple cat/nl/sed reads before any possibly mutating command are accepted.
Commands in downloaded trajectories are parsed, never executed. Source files are
fetched at SWE-bench's base commit. Unsupported commands and later reads are
counted, rather than silently treated as equivalent inputs.
Requires permission for dataset and source downloads before running.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import urllib.parse
import urllib.request

COMMAND = re.compile(r"```bash\s*\n(.*?)\n```", re.S)
QUERY = re.compile(r"<context_focus_question>\s*(.*?)\s*</context_focus_question>", re.S)
OUTPUT = re.compile(r"<output>\n(.*?)</output>", re.S)
PREFIX = "Filtered some unrelevant parts judged by your context_focus_question, good try! Filtered Output:\n"
READ_ONLY = {"ls", "pwd", "find", "grep", "rg", "head", "tail", "wc", "cat", "nl"}


def read_spec(command):
    """A finite whitelist; shell expansions, pipelines and redirection are excluded."""
    if any(c in command for c in ("$", "`", ">", "<", "|", ";", "\n", "&&", "||")):
        return None
    try:
        args = shlex.split(command)
    except ValueError:
        return None
    if len(args) == 2 and args[0] == "cat":
        return args[-1], "cat", None
    if len(args) == 3 and args[:2] in (["cat", "-n"], ["nl", "-ba"]):
        return args[-1], "number", None
    if len(args) == 2 and args[0] == "nl":
        return args[-1], "nonblank", None
    if len(args) == 4 and args[:2] == ["sed", "-n"]:
        match = re.fullmatch(r"(\d+)(?:,(\d+|\$))?p", args[2])
        if match:
            return args[-1], "range", match.groups()
    return None


def known_read_only(command):
    if any(c in command for c in ("$", "`", ">", "<", "|", ";", "\n", "&&", "||")):
        return False
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    # find -exec/-delete and shell-invoking grep flags are not simple reads.
    return bool(args and args[0] in READ_ONLY and not any(x in args for x in ("-exec", "-execdir", "-delete"))) or read_spec(command) is not None


def format_source(text, mode, limits):
    lines = text.splitlines(keepends=True)
    if mode == "cat":
        return text
    if mode == "range":
        lo, hi = limits
        return "".join(lines[int(lo) - 1:len(lines) if hi == "$" else int(hi or lo)])
    out, number = [], 0
    for line in lines:
        if mode == "nonblank" and line.rstrip("\n") == "":
            out.append("       \n")
        else:
            number += 1
            out.append(f"{number:6}\t{line}")
    return "".join(out)


def get(url, path):
    if not path.exists():
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return path.read_bytes()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--original", type=Path, default=Path(__file__).resolve().parents[2] / "data/repro/swe-pruner")
    ap.add_argument("--limit", type=int, help="cap successful recovered pairs; default all")
    a = ap.parse_args(argv)
    dest = a.original / "verified"
    tasks = {}
    for offset in range(0, 500, 100):
        url = f"https://datasets-server.huggingface.co/rows?dataset=princeton-nlp/SWE-bench_Verified&config=default&split=test&offset={offset}&length=100"
        data = json.loads(get(url, dest / f"rows-{offset}.json"))
        tasks.update((r["row"]["instance_id"], r["row"]) for r in data["rows"])
    counts, pairs = Counter(), []
    for path in sorted((a.original / "traj").rglob("*.traj.json")):
        trajectory = json.loads(path.read_text(encoding="utf-8"))
        task = tasks.get(trajectory.get("instance_id"))
        if not task:
            counts["missing_task"] += 1
            continue
        pristine = True
        messages = trajectory["messages"]
        for index, message in enumerate(messages):
            if message["role"] != "assistant":
                continue
            match = COMMAND.search(message["content"])
            if not match:
                pristine = False
                continue
            command = match[1].strip()
            query = QUERY.search(message["content"])
            spec = read_spec(command)
            if not pristine or not spec or not query:
                counts["later_or_unsupported"] += 1
                pristine = pristine and known_read_only(command)
                continue
            if index + 1 >= len(messages) or messages[index + 1]["role"] != "user":
                counts["no_output"] += 1
                continue
            output = OUTPUT.search(messages[index + 1]["content"])
            if not output or not output[1].startswith(PREFIX):
                counts["not_filtered"] += 1
                continue
            file, mode, limits = spec
            file = file.removeprefix("/testbed/").removeprefix("./")
            if PurePosixPath(file).is_absolute() or ".." in PurePosixPath(file).parts or file.startswith("-"):
                counts["unsafe_path"] += 1
                continue
            url = f"https://raw.githubusercontent.com/{task['repo']}/{task['base_commit']}/{urllib.parse.quote(file)}"
            source = dest / "sources" / task["repo"] / task["base_commit"] / file
            try:
                raw = get(url, source)
                text = raw.decode("utf-8")
            except (OSError, UnicodeError) as e:
                counts["fetch_error"] += 1
                print(json.dumps(dict(file=file, error=str(e))), flush=True)
                continue
            config = trajectory["info"]["config"]["agent"]["pruner"]
            pairs.append(dict(instance_id=task["instance_id"], repo=task["repo"], base_commit=task["base_commit"],
                              file=file, command=command, text=format_source(text, mode, limits), query=query[1].strip(),
                              expected=output[1], trajectory=str(path.relative_to(a.original)), message=index,
                              source_sha256=hashlib.sha256(raw).hexdigest(),
                              threshold=config["threshold"], chunk_overlap_tokens=config["chunk_overlap_tokens"],
                              expected_stats=messages[index + 1].get("pruned_stats")))
            counts["recovered"] += 1
            if len(pairs) % 10 == 0:
                print(json.dumps(dict(recovered=len(pairs), last=task["instance_id"])), flush=True)
            if a.limit and len(pairs) >= a.limit:
                break
        if a.limit and len(pairs) >= a.limit:
            break
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "inputs.jsonl").write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in pairs), encoding="utf-8")
    report = dict(scope="base-commit simple reads before any possibly mutating command", counts=dict(counts),
                  recovered=len(pairs), limit=a.limit)
    (dest / "input_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
