"""Tool fields: the host's tools return more of what the agent would otherwise read again to be sure.

The method runs where the agent's commands run (the ctxpress proxy lives in the task container), so it can look at
the workspace. Before each request it adds fields to the tool outputs that arrived since the last one:

  [workspace]   the files this step changed, with the changed lines and their new line numbers, taken from the
                working tree's diff against the previous step (an edit receipt: no need to print the file again)
  [unchanged]   replaces an output identical to the last output of the same command (a polled log, a re-read)

What the fields contain and how they read is a plain policy module (toolfields_policies.seed) so that it can be
rewritten by hand or by an automatic search; GUIDANCE is added to the agent's instructions. Outputs already in the
history when the method starts (a resumed session) are registered but never changed.
"""
from __future__ import annotations
import hashlib, os, subprocess
from ctxpress.core import tokens
from ctxpress.core.trace import shell_command, unescape
from ctxpress.methods.base import Method
from ctxpress.methods.readless import host_commands, inner_commands, join_code_mode, load_policy, norm_cmd, split_code_mode

DEFAULT_POLICY = "ctxpress.methods.toolfields_policies.seed"
NL = b"\n"


def _git(workdir, *args):
    r = subprocess.run(["git", "-C", workdir, *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200])
    return r.stdout


def snapshot(workdir):
    """{path: (kind, frozenset of hunks)} of the working tree against HEAD; a hunk is a tuple of its lines."""
    out = {}
    path, hunk, hunks = None, None, []

    def close():
        if path is not None:
            if hunk:
                hunks.append(tuple(hunk))
            out[path] = ("modified", tuple(hunks))
    for line in _git(workdir, "diff", "--no-color", "--no-ext-diff", "-U1", "HEAD").split("\n"):
        if line.startswith("diff --git "):
            close()
            path, hunk, hunks = line.split(" b/", 1)[-1], None, []
        elif line.startswith("@@") and path is not None:
            if hunk:
                hunks.append(tuple(hunk))
            hunk = ["@@ new lines " + line.split("+", 1)[-1].split(" @@")[0] + " @@"]
        elif hunk is not None and line[:1] in (" ", "+", "-") and not line.startswith(("+++", "---")):
            hunk.append(line)
    close()
    for p in _git(workdir, "ls-files", "--others", "--exclude-standard").split("\n"):
        if p:
            try:
                with open(os.path.join(workdir, p), "rb") as f:
                    data = f.read(2_000_000)
            except OSError:
                continue
            n = data.count(NL)
            out[p] = ("new", ((hashlib.sha1(data).hexdigest(), f"{n} lines"),))
    return out


def changes(before, after):
    """[(path, kind, new hunks)] for files whose diff differs between two snapshots (hunks matched by content)."""
    rows = []
    for p in sorted(set(before) | set(after)):
        a, b = before.get(p), after.get(p)
        if a == b:
            continue
        if b is None:
            rows.append((p, "reverted to HEAD" if a[0] == "modified" else "deleted", []))
        elif b[0] == "new":
            rows.append((p, "new file, " + b[1][0][1], []))
        else:
            old = {h[1:] for h in (a[1] if a and a[0] == "modified" else ())}
            rows.append((p, "modified", [list(h) for h in b[1] if h[1:] not in old]))
    return rows


class ToolFields(Method):
    name = "Tool fields"
    framework = dict(L1="工具输出附加字段：本步改动的文件和改动行（编辑回执）；同一命令输出不变时标注 unchanged",
                     L2="无", L3="无", cross="无", memory="无", decider="字段策略（可手写或自动搜索）")
    source = "本文"

    def __init__(self, policy=DEFAULT_POLICY, receipts=True, repeats=True, workdir=None):
        self.policy_spec = policy
        load_policy(policy)
        self.receipts, self.repeats = receipts, repeats
        self.workdir = workdir
        self.instructions = load_policy(policy).GUIDANCE
        self.reset(None)

    def reset(self, sim):
        self.started = False
        self.pending = []          # output items ingested since the last request
        self.last = {}             # normalised command -> (step, output)
        self.steps = 0
        self.tree = None

    def _wd(self):
        return self.workdir or os.environ.get("CTXPRESS_WORKDIR") or os.getcwd()

    def on_ingest(self, sim, item):
        if item.get("seg") == "out" and not item.get("protected") and not item.get("has_media"):
            self.pending.append(item)

    def step(self, sim, r):
        items, self.pending = self.pending, []
        policy = load_policy(self.policy_spec)
        if self.receipts:
            try:
                tree = snapshot(self._wd())
            except Exception:
                sim.M["toolfields_git_failed"] += 1
                tree = None
        first = not self.started
        self.started = True
        rows = changes(self.tree, tree) if self.receipts and tree is not None and self.tree is not None else []
        if self.receipts and tree is not None:
            self.tree = tree
        for k, item in enumerate(items):
            self.steps += 1
            text = item.get("text") or ""
            new = self._repeats(sim, item, text, policy, register_only=first)
            if first:
                continue
            if rows and k == len(items) - 1:      # the tree changed during these commands: receipt on the last output
                field = policy.receipt(rows, dict(tokens=tokens.count))
                if field:
                    new = (new if new is not None else text) + "\n" + field
                    sim.M["toolfields_receipt"] += 1
                    sim.M["toolfields_receipt_tokens"] += tokens.count(field)
            if new is not None and new != text:
                sim.keep_text(item, new, form="structured")

    def _repeats(self, sim, item, text, policy, register_only):
        """The output with repeated chunks replaced (None: unchanged); registers every command's output."""
        call = unescape(item.get("call_text") or "")
        ctx = dict(tokens=tokens.count)
        parts = split_code_mode(text)
        if parts is not None and "tools." in call:
            header, chunks = parts
            cmds = host_commands(getattr(sim, "calls", {}).get(item.get("call_id")) or {}) or inner_commands(call)
            if len(cmds) != len(chunks):
                return None
            changed = False
            for c, cmd in zip(chunks, cmds):
                if cmd and isinstance(c.get("output"), str):
                    rep = self._one(sim, cmd, c["output"], policy, ctx, register_only)
                    if rep is not None:
                        c["output"] = rep; changed = True
            return join_code_mode(header, chunks) if changed else None
        return self._one(sim, shell_command(call), text, policy, ctx, register_only)

    def _one(self, sim, cmd, out, policy, ctx, register_only):
        key = norm_cmd(cmd)
        earlier = self.last.get(key)
        self.last[key] = (self.steps, out)
        if register_only or not self.repeats or earlier is None:
            return None
        rep = policy.repeat(cmd, out, earlier, ctx)
        if rep is not None:
            sim.M["toolfields_repeat"] += 1
            sim.M["toolfields_repeat_saved_tokens"] += max(0, tokens.count(out) - tokens.count(rep))
        return rep
