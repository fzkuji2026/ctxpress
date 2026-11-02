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


def repeat(cmd, out, earlier, ctx):
    step, before = earlier
    if out == before and ctx["tokens"](out) >= MIN_REPEAT_TOKENS:
        return "[unchanged: byte-identical to the last output of this same command]"
    return None
