"""Hand-written seed for ToolFields: which extra fields a tool output carries.

receipt(changes, ctx) -> str | None
    changes: [(path, kind, hunks)] for files whose working-tree diff changed during this step; kind is "modified",
    "new" or "deleted"; hunks are the new diff hunks (lists of lines, header first). None: add no receipt.
repeat(cmd, out, earlier, ctx) -> str | None
    earlier: (step, out) of the last time this same command ran. A string replaces this output.
GUIDANCE: text added to the agent's instructions.
"""
HUNK_LINES = 24
TOTAL_LINES = 90
MIN_REPEAT_TOKENS = 30

GUIDANCE = ("Each command output may end with a [workspace] field. It is computed by the tool from the files on "
            "disk after your command ran: every file your step changed, with the changed lines and their new line "
            "numbers. Trust it: you do not need to print a file again to check that an edit applied. An output "
            "marked [unchanged] is byte-identical to the last time you ran that command.")


def receipt(changes, ctx):
    if not changes:
        return None
    lines, more = [], 0
    for path, kind, hunks in changes:
        if kind != "modified":
            lines.append(f"{path}: {kind}")
            continue
        lines.append(f"{path}: modified")
        for h in hunks:
            body = h[:HUNK_LINES]
            if len(lines) + len(body) > TOTAL_LINES:
                more += sum(1 for x in h if x[:1] in "+-")
                continue
            lines += body
            if len(h) > HUNK_LINES:
                lines.append(f"  ... {len(h) - HUNK_LINES} more lines in this hunk")
    if more:
        lines.append(f"(+{more} more changed lines not shown; `git diff` prints them)")
    return "[workspace: your step changed these files]\n" + "\n".join(lines)


LEDGER_TOOL = dict(
    name="task_ledger",
    description="Your ledger of the task's requirements. Add or update items: id, requirement, the files that "
                "implement it, and after you check it, the check you ran and its result (pass/fail). The tool hashes "
                "those files and reports, for every item, whether it passed and whether any of its files changed "
                "since that check. Call with no items to see the current status.",
    inputSchema=dict(type="object", properties=dict(items=dict(type="array", items=dict(
        type="object", properties=dict(
            id=dict(type="string"), requirement=dict(type="string"),
            files=dict(type="array", items=dict(type="string"), description="paths relative to the repository root"),
            check=dict(type="string", description="the command or test that verifies it"),
            result=dict(type="string", enum=["pass", "fail"]), note=dict(type="string")),
        required=["id"])))))

LEDGER_GUIDANCE = ("Keep the task's requirements in mcp__ctxpress.task_ledger: record each requirement and the files "
                   "that implement it as you go, and record the check you ran and its result once you verify it. The "
                   "tool tells you which items passed and whether their files changed since. When every item is "
                   "verified as passing and unchanged, the task is complete: reading the code or rerunning the checks "
                   "again cannot change that, so finish.")


def ledger_report(rows):
    if not rows:
        return "ledger is empty: add the task's requirements as items"
    done = [r for r in rows if r["result"] == "pass" and not r["changed_since_check"]]
    lines = []
    for r in rows:
        if r["result"] is None:
            state = "not verified yet"
        elif r["changed_since_check"]:
            state = f"{r['result']} at its check, but changed since: {', '.join(r['changed_since_check'])}"
        else:
            state = f"{r['result']}, files unchanged since the check"
        lines.append(f"- {r['id']}: {r['requirement'][:120]} [{state}]")
    head = (f"ALL {len(rows)} items verified as passing, none changed since: the task is complete."
            if len(done) == len(rows) else f"{len(done)}/{len(rows)} items verified as passing and unchanged.")
    return head + "\n" + "\n".join(lines)



BOUNDARY_HUNK_LINES = 30
BOUNDARY_GUIDANCE = ("After your context is compacted, the next command output ends with a [your changes so far] "
                     "field: every file you have changed since the task started, with the changed lines, computed from "
                     "the files on disk. Use it instead of printing those files again.")


BOUNDARY_TOKENS = 2500        # the whole field; it is carried in every later request
BOUNDARY_FILES = 15            # files listed by name (largest changes first); the rest are counted


def boundary(changes, ctx):
    """Changes since the task started, shown once after a compaction, within a token budget: the files with the
    largest changes by name with their changed lines, the many small ones only counted."""
    if not changes:
        return None
    size = lambda h: sum(1 for x in h if x[:1] in "+-")
    rows = sorted(changes, key=lambda c: (c[1] == "modified", -sum(size(h) for h in c[2])))
    rows = [c for c in rows if c[1] != "modified"] + sorted((c for c in rows if c[1] == "modified"),
                                                            key=lambda c: -sum(size(h) for h in c[2]))
    named, rest = rows[:BOUNDARY_FILES], rows[BOUNDARY_FILES:]
    stat = [f"{p}: {k}" + (f" (+{sum(1 for h in hs for x in h if x[:1] == '+')} "
                           f"-{sum(1 for h in hs for x in h if x[:1] == '-')})" if k == "modified" else "")
            for p, k, hs in named]
    if rest:
        small = sum(sum(size(h) for h in hs) for _, _, hs in rest)
        stat.append(f"... and {len(rest)} more files with {small} changed lines in all (`git diff --stat` lists them)")
    out = "[your changes so far, from the files on disk]\n" + "\n".join(stat)
    cut = 0
    for path, kind, hunks in named:
        if kind != "modified":
            continue
        block = [f"--- {path}"]
        for h in hunks:
            body = [x[:200] for x in h[:BOUNDARY_HUNK_LINES]]
            if ctx["tokens"](out + "\n".join(block + body)) > BOUNDARY_TOKENS:
                cut += size(h)
                continue
            block += body
        if len(block) > 1:
            out += "\n" + "\n".join(block)
    if cut:
        out += f"\n(+{cut} more changed lines not shown; `git diff -- <file>` prints them)"
    return out


def repeat(cmd, out, earlier, ctx):
    step, before = earlier
    if out == before and ctx["tokens"](out) >= MIN_REPEAT_TOKENS:
        return "[unchanged: byte-identical to the last output of this same command]"
    return None
