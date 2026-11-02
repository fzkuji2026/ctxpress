"""The proposer: Codex rewrites a parent tool policy into a new candidate.

It works in a scratch directory holding the parent's source (parent.py), the reference read tools it may import
(ctxpress/tools/outline.py, definition.py), the policy format (FORMAT.md, from ctxpress.evolve.policy), and the
evidence: the parent's waste report (how many read tokens the agent never used, by kind, and the reads that wasted
most, with their commands), its scores, and the archive's table of all candidates so far. It writes candidate.py and a
one-line NOTE.txt. A candidate that fails the checks goes back once with the problems listed.

Codex runs on the host with a private temporary CODEX_HOME holding a copy of the file named by
CTXPRESS_CODEX_AUTH_FILE (as an eval container gets one); the copy is deleted when it exits. Nothing reads or prints
the credentials.
"""
from __future__ import annotations
import json, os, shutil, subprocess, tempfile
from ctxpress.evolve import policy

CODEX = os.path.expanduser("~/ctxpress-prepared/codex-0.161.0/bin/codex")
CATALOG = os.path.join(policy.ROOT, "runs", "prepared", "codex-host-metadata", "gpt-6.1-sol.catalog.json")
MODEL, EFFORT = "gpt-6.1-sol", "high"
TIMEOUT = 1800

TASK = """You are improving the read tools of a coding agent (Codex) so that it puts less content into its context while
it works on long software tasks, without doing the tasks worse. Its context is managed by our method
(infinite-context); what you change is the tool policy in parent.py. Read FORMAT.md first: it is the exact module
format and the rules the module must follow (allowed imports, no writes, Python 3.8, the cli footer).

The goal: a lower manageable bill (the input tokens the agent's history costs, every request) at the same score. The
evidence in EVIDENCE.md says where the parent still wastes: read content (file prints, read-tool output) that the agent
never used afterwards (not edited near it, never named again), and which commands produced the most of it. Most of
the agent's reads are chained `sed -n 'A,Bp' FILE` and `cat FILE` inside one command; a whole-file `cat` is the only
output the view() function can reshape; everything else changes only if the agent chooses your tools, so the tool
interface, what each tool returns, and the GUIDANCE wording all matter. Ideas you may use or ignore: return less by
default and let the agent ask for more; return a definition's signature and docstring before its body; let one
command answer several questions at once; make the cheaper path the more obvious one in GUIDANCE; show what a file
contains around the names the agent searched for. Do not hide information the agent needs to finish the task: a score
drop counts against the candidate.

Write the new module to candidate.py (complete, self-contained apart from the allowed imports) and one line to
NOTE.txt saying what you changed and why. You may run `python3 -c` to test pieces of it against the files in ref/ or
your own samples. Do not modify any other file. Stop when candidate.py and NOTE.txt are written."""


def _format_doc():
    return policy.__doc__ + "\n\nThe seed policies (parent.py may be one) are complete examples of the format.\n"


def _evidence(parent, reports, archive_table):
    lines = [f"# Parent {parent['id']}", f"note: {parent.get('note', '')}", ""]
    for key in ("full", "screen"):
        if parent.get(key):
            c = parent[key]
            lines.append(f"## {key} evaluation against our method alone ({c.get('jobs')} jobs on {c.get('tasks')} tasks)")
            lines.append(json.dumps({k: c.get(k) for k in ("saving", "reward_diff", "median", "unused", "unused_base",
                                                           "read_cut", "tool_calls", "calls")}, indent=1))
            lines.append(f"per-task saving: {json.dumps(c.get('per_task'))}")
            lines.append("")
    if reports:
        lines.append("## Where the parent's runs spent their read tokens (sum over runs)")
        agg = {}
        for r in reports:
            for k, g in r["by_kind"].items():
                a = agg.setdefault(k, dict(n=0, tokens=0, unused_tokens=0))
                for x in a:
                    a[x] += g.get(x, 0)
        for k, a in sorted(agg.items(), key=lambda kv: -kv[1]["tokens"]):
            lines.append(f"- {k}: {a['n']} outputs, {a['tokens']} tokens, {a['unused_tokens']} never used afterwards")
        lines.append("")
        lines.append("## The reads that wasted most (command, tokens, tokens never used afterwards)")
        worst = sorted((w for r in reports for w in r["worst"]), key=lambda w: -w["usd_unused"])[:15]
        for w in worst:
            lines.append(f"- {w['tokens']} tokens, {w['unused_tokens']} unused: {w['cmd'][:300]}")
        lines.append("")
    lines += ["## All candidates so far (id, parent, status, fitness = saving of the manageable bill)", archive_table]
    return "\n".join(lines)


def _codex(workdir, prompt, log):
    auth = os.environ.get("CTXPRESS_CODEX_AUTH_FILE")
    if not auth or not os.path.isfile(auth):
        raise RuntimeError("CTXPRESS_CODEX_AUTH_FILE does not name a file")
    cache = os.path.expanduser("~/.cache")                 # Codex refuses its helper binaries under /tmp
    os.makedirs(cache, exist_ok=True)
    home = tempfile.mkdtemp(prefix="ctxpress-proposer-", dir=cache)
    try:
        os.chmod(home, 0o700)
        shutil.copyfile(auth, os.path.join(home, "auth.json"))
        os.chmod(os.path.join(home, "auth.json"), 0o600)
        with open(os.path.join(home, "config.toml"), "w") as f:
            f.write(f'model = "{MODEL}"\nmodel_reasoning_effort = "{EFFORT}"\nweb_search = "disabled"\n'
                    f"model_catalog_json = {json.dumps(CATALOG)}\n")
        env = {k: v for k, v in os.environ.items() if k != "CTXPRESS_CODEX_AUTH_FILE"}
        env["CODEX_HOME"] = home
        with open(log, "w") as out:
            r = subprocess.run([CODEX, "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "--json", prompt],
                               cwd=workdir, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                               timeout=TIMEOUT)
        return r.returncode
    finally:
        shutil.rmtree(home, ignore_errors=True)


def propose(cid, parent, parent_src, reports, archive_table, workroot):
    """(source, note, problems, log path) of a new candidate written by Codex from a parent."""
    wd = os.path.join(workroot, cid)
    os.makedirs(os.path.join(wd, "ref"), exist_ok=True)
    with open(os.path.join(wd, "parent.py"), "w") as f:
        f.write(parent_src)
    for t in ("outline.py", "definition.py"):
        shutil.copyfile(os.path.join(policy.ROOT, "ctxpress", "tools", t), os.path.join(wd, "ref", t))
    with open(os.path.join(wd, "FORMAT.md"), "w") as f:
        f.write(_format_doc())
    with open(os.path.join(wd, "EVIDENCE.md"), "w") as f:
        f.write(_evidence(parent, reports, archive_table))
    problems, src, note = [], None, ""
    for attempt in range(2):
        prompt = TASK if not problems else (
            TASK + "\n\nYour candidate.py failed these checks; fix them and write candidate.py again (NOTE.txt still "
            "describes what the candidate changes against parent.py, not the fix):\n- " + "\n- ".join(problems))
        code = _codex(wd, prompt, os.path.join(wd, f"codex-{attempt}.jsonl"))
        path = os.path.join(wd, "candidate.py")
        if not os.path.exists(path):
            problems = [f"codex exited with {code} and wrote no candidate.py"]
            continue
        with open(path) as f:
            src = f.read()
        if os.path.exists(os.path.join(wd, "NOTE.txt")):
            with open(os.path.join(wd, "NOTE.txt")) as f:
                text = f.read().strip()
            note = text.splitlines()[0][:300] if text else ""
        problems, _ = policy.check(cid, src)
        if not problems:
            break
    return src, note, problems, wd
