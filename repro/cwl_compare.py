"""CWL: ours vs the authors' eviction filter and delimiter tool (Kiz8-Team/pi-cwl @ cd2ea3f), run by Node.

There are no released sessions annotated with the delimiter tool, so seeded random sessions are generated:
exploration and action episodes with dependencies (some open at the end, some rejected calls: starting while one
is active, ending an exploration without a description, unknown or non-exploration dependencies, duplicates),
thinking, text, read / grep / find / ls / glob / bash / edit / write calls with results of random size, and user
messages, sometimes inside episodes. Node runs the authors' `filterContext` (core/context-filter.ts) and the
`delimiter` tool's `execute` (core/tools/delimiter.ts, with core/chunk.ts) on the growing message list after every
round, as pi's transformContext hook does before each model call. Ours runs the same rounds through LiveContext with
CWL, the delimiter results coming from our tool (`CWL.call_tool`, as `ctxpress mcp` answers). Per round we compare
every message block: user text, thinking, text, tool calls (id, name) and tool results (id, text).

Their files are used unmodified except import paths (.js -> .ts for the TypeScript files Node loads); stubs replace
the UI (`@mariozechner/pi-tui`, render helpers) and schema (`@sinclair/typebox`) packages; `estimateTokens` is copied
verbatim from core/compaction/compaction.ts into a module of its own (the rest of that file needs the model client).

    python repro/cwl_compare.py [--sessions 200] [--seed 0]
"""
from __future__ import annotations
import argparse, json, os, random, re, shutil, subprocess, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA = os.environ.get("CTXPRESS_DATA", os.path.join(ROOT, "data"))
sys.path.insert(0, os.path.join(ROOT, "ctxpress"))
from ctxpress.live.context import LiveContext
from ctxpress.methods import CWL

SRC = os.path.join(DATA, "repro", "pi-cwl", "packages", "coding-agent", "src", "core")
DRIVER = r"""
import { readFileSync } from "node:fs";
import { filterContext } from "./core/context-filter.ts";
import { createDelimiterToolDefinition } from "./core/tools/delimiter.ts";
const input = JSON.parse(readFileSync(0, "utf8"));
const out = [];
for (const s of input.sessions) {
  const messages = [{ role: "user", content: s.task }];
  const tool = createDelimiterToolDefinition(() => messages);
  const views = [];
  for (const ev of s.events) {
    if (ev.user !== undefined) messages.push({ role: "user", content: ev.user });
    else {
      messages.push({ role: "assistant", content: ev.blocks });
      for (const c of ev.blocks.filter((b) => b.type === "toolCall")) {
        if (c.name === "delimiter") {
          try {
            const r = await tool.execute(c.id, c.arguments, undefined, undefined, {});
            messages.push({ role: "toolResult", toolCallId: c.id, toolName: "delimiter", content: r.content, details: r.details, isError: false });
          } catch (e) {
            messages.push({ role: "toolResult", toolCallId: c.id, toolName: "delimiter", content: [{ type: "text", text: e.message }], isError: true });
          }
        } else messages.push({ role: "toolResult", toolCallId: c.id, toolName: c.name, content: [{ type: "text", text: ev.results[c.id] }], isError: false });
      }
    }
    // under the threshold filterContext returns the live array itself: keep a copy of this round's view
    views.push([...filterContext(messages, 1000000, { type: "tokens", value: s.threshold }).messages]);
  }
  out.push(views);
}
process.stdout.write(JSON.stringify(out));
"""


def prepare(work):
    """Their sources with .ts import paths, the verbatim estimateTokens, and stubs, in `work`."""
    shutil.rmtree(work, ignore_errors=True)
    for rel in ("context-filter.ts", "chunk.ts", "tools/delimiter.ts"):
        text = open(os.path.join(SRC, rel), encoding="utf-8").read()
        text = text.replace('"./compaction/compaction.js"', '"./compaction-estimate.ts"').replace('"../chunk.js"', '"../chunk.ts"')
        os.makedirs(os.path.dirname(os.path.join(work, "core", rel)), exist_ok=True)
        open(os.path.join(work, "core", rel), "w", encoding="utf-8").write(text)
    comp = open(os.path.join(SRC, "compaction", "compaction.ts"), encoding="utf-8").read()
    start = comp.index("export function estimateTokens")
    depth, i = 0, comp.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(comp[i], 0)
        i += 1
        if depth == 0:
            break
    open(os.path.join(work, "core", "compaction-estimate.ts"), "w", encoding="utf-8").write(
        'import type { AgentMessage } from "@mariozechner/pi-agent-core";\n'
        'import type { AssistantMessage } from "@mariozechner/pi-ai";\n' + comp[start:i] + "\n")
    stubs = {"core/tools/render-utils.js": "export function renderBoundaryLine() { return ''; }\n",
             "core/tools/tool-definition-wrapper.js": "export function wrapToolDefinition(d) { return d; }\n",
             "node_modules/@mariozechner/pi-tui/index.js": "export class Text {}\nexport function truncateToWidth(s) { return s; }\n",
             "node_modules/@sinclair/typebox/index.js":
                 "const o = (x, k) => ({ ...k, ...x });\nexport const Type = { Object: (p, k) => o({ type: 'object', properties: p }, k),"
                 " Union: (a, k) => o({ anyOf: a }, k), Literal: (v) => ({ const: v }), Optional: (x) => x,"
                 " String: (k) => o({ type: 'string' }, k), Array: (x, k) => o({ type: 'array', items: x }, k) };\n",
             "driver.mjs": DRIVER}
    for pkg in ("@mariozechner/pi-tui", "@sinclair/typebox"):
        stubs[f"node_modules/{pkg}/package.json"] = json.dumps({"name": pkg, "type": "module", "main": "index.js"})
    stubs["package.json"] = json.dumps({"type": "module"})
    for rel, text in stubs.items():
        os.makedirs(os.path.dirname(os.path.join(work, rel)) or work, exist_ok=True)
        open(os.path.join(work, rel), "w", encoding="utf-8").write(text)


def session(rng):
    """A random annotated session (abstract rounds); invalid delimiter calls are mixed in on purpose."""
    # non-ASCII on purpose: JavaScript counts UTF-16 units (an emoji is 2), Python code points
    words = "alpha beta gamma delta parser cache auth route model view config test build index 中文 naïve 😀 🚀👍".split()
    text = lambda lo, hi: " ".join(rng.choice(words) for _ in range(rng.randint(lo, hi)))
    events, n, cid = [], 0, 0
    expl, names, active = [], [], None

    def call(name, args, result=None):
        nonlocal cid
        cid += 1
        return dict(type="toolCall", id=f"t{cid}", name=name, arguments=args), result

    for _ in range(rng.randint(25, 110)):
        if rng.random() < 0.06:
            events.append(dict(user=text(5, 60)))
            continue
        blocks, results = [], {}
        if rng.random() < 0.6:
            blocks.append(dict(type="thinking", thinking=text(5, 300)))
        if rng.random() < 0.4:
            blocks.append(dict(type="text", text=text(3, 80)))
        r = rng.random()
        if r < 0.22:                                         # a delimiter call, sometimes invalid
            if active is None or rng.random() < 0.08:
                kind = "act" if expl and rng.random() < 0.45 else "expl"
                n += 1
                name = rng.choice(names) if names and rng.random() < 0.04 else f"{kind}-{n}"
                args = dict(action="start", name=name, type=kind)
                if kind == "act":
                    deps = rng.sample(expl, rng.randint(1, min(3, len(expl))))
                    if rng.random() < 0.05:
                        deps.append(rng.choice(["missing", name] + names))
                    if rng.random() > 0.04:
                        args["dependencies"] = deps
                if active is None and name not in names:
                    names.append(name)
                    if args.get("dependencies") is not None or kind == "expl":
                        active = (name, kind)
                c, _ = call("delimiter", args)
            else:
                args = dict(action="end")
                if active[1] == "expl" and rng.random() > 0.05:
                    args["description"] = text(3, 20)
                elif active[1] == "act" and rng.random() < 0.05:
                    args["description"] = "unwanted"
                c, _ = call("delimiter", args)
            blocks.append(c)
        else:
            for _ in range(rng.choice([1, 1, 1, 2])):
                tool = rng.choice(["read", "read", "grep", "find", "ls", "glob", "bash", "bash", "edit", "write"])
                args = {"path": f"src/{rng.choice(words)}.py"} if tool in ("read", "edit", "write") else {"command": text(1, 6)}
                c, _ = call(tool, args)
                blocks.append(c)
                results[c["id"]] = text(10, rng.choice([80, 400, 1500]))
        events.append(dict(blocks=blocks, results=results))
        # keep our own idea of the active chunk close to the tool's (the tool decides; we only steer the mix)
        last = blocks[-1]
        if last["name"] == "delimiter" and last["arguments"]["action"] == "end" and active:
            ok = (active[1] == "expl") == bool(last["arguments"].get("description"))
            if ok:
                if active[1] == "expl":
                    expl.append(active[0])
                active = None
    return dict(task=text(20, 120), threshold=rng.choice([1500, 3000, 6000, 12000]), events=events)


def canon_theirs(view):
    out = []
    for m in view:
        if m["role"] == "user":
            out.append(("user", m["content"]))
        elif m["role"] == "assistant":
            for b in m["content"]:
                out.append(("thinking", b["thinking"]) if b["type"] == "thinking" else ("text", b["text"]) if b["type"] == "text"
                           else ("call", b["id"], b["name"]))
        else:
            out.append(("result", m["toolCallId"], "".join(x.get("text", "") for x in m["content"])))
    return out


def canon_ours(ctx):
    out = []
    for v, s in zip(ctx.view(), ctx.pinned + ctx.ctx):
        if v["seg"] == "reason":
            out.append(("thinking", v["text"]))
        elif v["seg"] == "msg":
            out.append(("user", v["text"]) if v["role"] == "user" else ("text", v["text"]))
        elif v["seg"] == "call":
            out.append(("call", s["call_id"], s["name"]))
        elif v["seg"] == "out":
            out.append(("result", s["call_id"], v["text"]))
    return out


def ours(s):
    tool, ctx = CWL(budget=s["threshold"]), LiveContext(CWL(budget=s["threshold"]))
    ctx.add_message("user", s["task"])
    views = []
    for ev in s["events"]:
        if "user" in ev:
            ctx.add_message("user", ev["user"])
        else:
            for b in ev["blocks"]:
                if b["type"] == "thinking":
                    ctx.add_other("reasoning", len(b["thinking"]) // 4, text=b["thinking"])
                elif b["type"] == "text":
                    ctx.add_message("assistant", b["text"])
            calls = [b for b in ev["blocks"] if b["type"] == "toolCall"]
            for b in calls:
                ctx.add_call(b["id"], json.dumps(b["arguments"]), name=b["name"],
                             args=json.dumps(b["arguments"]))  # as a host may send them: spaced, ASCII-escaped
            for b in calls:
                text = tool.call_tool("delimiter", b["arguments"])[0] if b["name"] == "delimiter" else ev["results"][b["id"]]
                ctx.add_output(b["id"], text)
        ctx.before_request()
        views.append(canon_ours(ctx))
    return views


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=200)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--work", default=os.path.join(ROOT, "ctxpress", "runs", "repro", "cwl-node"))
    ap.add_argument("--out", default=os.path.join(ROOT, "ctxpress", "runs", "repro", "cwl.json"))
    a = ap.parse_args()
    prepare(a.work)
    rng = random.Random(a.seed)
    sessions = [session(rng) for _ in range(a.sessions)]
    tot = dict(sessions=len(sessions), seed=a.seed, rounds=0, same=0, removed_blocks=0, diffs=[])
    for k, s in enumerate(sessions):
        if k % 25 == 0:                                   # every view of every round: Node's output in batches
            proc = subprocess.run(["node", "--experimental-transform-types", "--no-warnings", "driver.mjs"], cwd=a.work,
                                  input=json.dumps(dict(sessions=sessions[k:k + 25])), capture_output=True, text=True,
                                  encoding="utf-8")
            if proc.returncode:
                sys.exit(proc.stderr[-3000:])
            batch = json.loads(proc.stdout)
        tv = batch[k % 25]
        for r, (o, t) in enumerate(zip(ours(s), tv)):
            t = canon_theirs(t)
            tot["rounds"] += 1
            tot["same"] += o == t
            if o != t and len(tot["diffs"]) < 20:
                i = next((i for i, (x, y) in enumerate(zip(o, t)) if x != y), min(len(o), len(t)))
                tot["diffs"].append(dict(session=k, round=r, at=i, ours=str(o[i:i + 1])[:200], theirs=str(t[i:i + 1])[:200]))
        full = sum(len(e.get("blocks", [])) + len(e.get("results", {})) + ("user" in e) for e in s["events"]) + 1
        tot["removed_blocks"] += full - len(canon_theirs(tv[-1]))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(tot, open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(json.dumps(dict(tot, diffs=tot["diffs"][:3], n_diffs=len(tot["diffs"])), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
