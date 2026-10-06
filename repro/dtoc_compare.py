"""DTOC: ours vs the authors' key registry, manage_context tool and tool-result envelope (OpenCode fork
chaturvediabhay24/opencode @ 57bf2e1), run by Node.

There are no released DTOC sessions, so seeded random ones are generated: rounds of one to three parallel tool
calls (read / bash / grep / edit and manage_context with existing, unknown, repeated or already-hidden keys, and
some calls with invalid arguments), with outputs of random size. Node runs the authors' `session/dtoc.ts` registry
and `tool/manage_context.ts` `execute` (unmodified except import paths; `effect`, the Tool module, the layer helper
and the .txt import are stubbed so the module loads without its runtime), then assembles every round as
`toModelMessagesEffect` does: register each completed tool output in order, then the visible envelope or the hidden
placeholder (the two JSON.stringify expressions copied verbatim from session/message-v2.ts). A manage_context call
runs before that round's registration, as the tool executes before the next request is assembled; a call with
invalid arguments fails schema validation, and failed tool parts are neither registered nor wrapped. Ours runs the
same rounds through LiveContext with DTOC. Per round we compare the text of every tool output the model receives.

    python repro/dtoc_compare.py [--sessions 300] [--seed 0]
"""
from __future__ import annotations
import argparse, json, os, random, shutil, subprocess, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA = os.environ.get("CTXPRESS_DATA", os.path.join(ROOT, "data"))
sys.path.insert(0, os.path.join(ROOT, "ctxpress"))
from ctxpress.live.context import LiveContext
from ctxpress.methods import DTOC

SRC = os.path.join(DATA, "repro", "dtoc", "packages", "opencode", "src")
DRIVER = r"""
import { readFileSync } from "node:fs";
import { DTOC } from "./session/dtoc.ts";
import { ManageContextTool } from "./tool/manage_context.ts";
import { run, SERVICE } from "./stub-runtime.js";
const input = JSON.parse(readFileSync(0, "utf8"));
const out = [];
for (const [n, s] of input.sessions.entries()) {
  const dtoc = DTOC.layer;
  const sessionID = `s${n}`;
  dtoc.setDefault(sessionID, true);
  const tool = run(ManageContextTool.init, dtoc);
  const parts = [];
  const views = [];
  for (const round of s.rounds) {
    for (const c of round) {                     // the tools run, then the next request is assembled
      let output = c.output, status = "completed";
      if (c.tool === "manage_context") {
        if (!Array.isArray(c.args.enable) || !Array.isArray(c.args.disable)) { status = "error"; output = "invalid arguments"; }
        else output = run(tool.execute(c.args, { sessionID }), dtoc).output;
      }
      parts.push({ callID: c.id, tool: c.tool, status, output, end: c.end });
    }
    const view = [];
    for (const part of parts) {
      if (part.status !== "completed") { view.push([part.callID, part.output]); continue; }
      const tool_key = dtoc.registerOrGet(sessionID, { callID: part.callID, toolName: part.tool,
        estimatedTokens: Math.ceil(part.output.length / 4), timestamp: part.end });
      const dtocEntry = dtoc.getEntry(sessionID, tool_key);
      const outputText = part.output;
      if (!dtocEntry.visible) {
        const placeholder = JSON.stringify({
                tool_key: dtocEntry.tool_key,
                tool: dtocEntry.toolName,
                estimated_tokens: dtocEntry.estimatedTokens,
                timestamp: dtocEntry.timestamp,
                status: "hidden",
              })
        view.push([part.callID, placeholder]);
      } else {
        const wrappedOutput = dtocEntry
                ? JSON.stringify({
                    tool_key: dtocEntry.tool_key,
                    tool: dtocEntry.toolName,
                    estimated_tokens: dtocEntry.estimatedTokens,
                    tool_result: outputText,
                  })
                : outputText
        view.push([part.callID, wrappedOutput]);
      }
    }
    views.push(view);
  }
  out.push(views);
}
process.stdout.write(JSON.stringify(out));
"""
RUNTIME = r"""
export const SERVICE = Symbol("service");
// Generators stand in for Effect.gen: `yield* DTOC.Service` asks for the service, which `run` supplies.
export function run(effect, service) {
  const it = effect.gen();
  let step = it.next();
  while (!step.done) step = it.next(step.value === SERVICE ? service : undefined);
  return step.value;
}
"""
EFFECT = r"""
import { SERVICE } from "../../stub-runtime.js";
export const Context = { Service: () => (id) => class { static id = id; static of(x) { return x; }
  static *[Symbol.iterator]() { return yield SERVICE; } } };
export const Layer = { succeed: (_service, impl) => impl };
const chain = { annotate() { return chain; } };
export const Schema = { Struct: (x) => x, mutable: (x) => x, Array: () => chain, String: {} };
export const Effect = { gen: (f) => ({ gen: f }) };
"""


def prepare(work):
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(os.path.join(work, "session")); os.makedirs(os.path.join(work, "tool"))
    dtoc = open(os.path.join(SRC, "session", "dtoc.ts"), encoding="utf-8").read().replace('from "./dtoc"', 'from "./dtoc.ts"')
    open(os.path.join(work, "session", "dtoc.ts"), "w", encoding="utf-8").write(dtoc)
    tool = open(os.path.join(SRC, "tool", "manage_context.ts"), encoding="utf-8").read()
    tool = tool.replace('from "./tool"', 'from "./tool-stub.js"').replace('from "../session/dtoc"', 'from "../session/dtoc.ts"')
    tool = tool.replace('from "./manage_context.txt"', 'from "./manage_context-txt.js"')
    open(os.path.join(work, "tool", "manage_context.ts"), "w", encoding="utf-8").write(tool)
    text = open(os.path.join(SRC, "tool", "manage_context.txt"), encoding="utf-8").read()
    files = {"tool/tool-stub.js": "export function define(name, init) { return { name, init }; }\n",
             "tool/manage_context-txt.js": f"export default {json.dumps(text)};\n",
             "stub-runtime.js": RUNTIME, "driver.mjs": DRIVER, "package.json": json.dumps({"type": "module"}),
             "node_modules/effect/index.js": EFFECT,
             "node_modules/effect/package.json": json.dumps({"name": "effect", "type": "module", "main": "index.js"}),
             "node_modules/@opencode-ai/core/effect/layer-node.js": "export const LayerNode = { make: () => null };\n",
             "node_modules/@opencode-ai/core/package.json": json.dumps({"name": "@opencode-ai/core", "type": "module",
                                                                       "exports": {"./effect/layer-node": "./effect/layer-node.js"}})}
    for rel, body in files.items():
        os.makedirs(os.path.dirname(os.path.join(work, rel)) or work, exist_ok=True)
        open(os.path.join(work, rel), "w", encoding="utf-8").write(body)


def session(rng):
    words = "alpha beta gamma delta parser cache auth route model view config test".split()
    text = lambda lo, hi: " ".join(rng.choice(words) for _ in range(rng.randint(lo, hi)))
    rounds, n, t = [], 0, 1_790_000_000_000
    for _ in range(rng.randint(5, 60)):
        calls = []
        for _ in range(rng.choice([1, 1, 2, 3])):
            n += 1; t += rng.randint(100, 5000)
            if rng.random() < 0.25 and n > 2:
                keys = lambda: [f"tk_{rng.randint(1, n + 2):03d}" for _ in range(rng.randint(0, 3))]
                args = {"enable": keys(), "disable": keys()}
                if rng.random() < 0.06:
                    del args[rng.choice(["enable", "disable"])]
                calls.append(dict(id=f"c{n}", tool="manage_context", args=args, end=t))
            else:
                calls.append(dict(id=f"c{n}", tool=rng.choice(["read", "bash", "grep", "edit"]), output=text(1, rng.choice([30, 300, 1500])), end=t))
        rounds.append(calls)
    return dict(rounds=rounds)


def ours(s):
    ctx = LiveContext(DTOC())
    ctx.add_message("user", "task")
    views = []
    for round in s["rounds"]:
        for c in round:
            args = c.get("args", {"path": "f"})
            ctx.add_call(c["id"], json.dumps(args), name=c["tool"], args=json.dumps(args))
        for c in round:
            ctx.add_output(c["id"], c.get("output") or ("invalid arguments" if "manage" in c["tool"] and not
                                                         all(isinstance(c["args"].get(k), list) for k in ("enable", "disable")) else "recorded"))
            ctx.ctx[-1]["t"] = c["end"] / 1000
        ctx.before_request()
        views.append([[s2["call_id"], v["text"]] for v, s2 in zip(ctx.view(), ctx.pinned + ctx.ctx) if v["seg"] == "out"])
    return views


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--work", default=os.path.join(ROOT, "ctxpress", "runs", "repro", "dtoc-node"))
    ap.add_argument("--out", default=os.path.join(ROOT, "ctxpress", "runs", "repro", "dtoc.json"))
    a = ap.parse_args()
    prepare(a.work)
    rng = random.Random(a.seed)
    sessions = [session(rng) for _ in range(a.sessions)]
    proc = subprocess.run(["node", "--experimental-transform-types", "--no-warnings", "driver.mjs"], cwd=a.work,
                          input=json.dumps(dict(sessions=sessions)), capture_output=True, text=True, encoding="utf-8")
    if proc.returncode:
        sys.exit(proc.stderr[-3000:])
    theirs = json.loads(proc.stdout)
    tot = dict(sessions=len(sessions), seed=a.seed, rounds=0, same=0, hidden=0, manage_calls=0, diffs=[])
    for k, (s, tv) in enumerate(zip(sessions, theirs)):
        tot["manage_calls"] += sum(c["tool"] == "manage_context" for r in s["rounds"] for c in r)
        for r, (o, t) in enumerate(zip(ours(s), tv)):
            tot["rounds"] += 1
            tot["same"] += o == t
            if o != t and len(tot["diffs"]) < 20:
                i = next((i for i, (x, y) in enumerate(zip(o, t)) if x != y), min(len(o), len(t)))
                tot["diffs"].append(dict(session=k, round=r, at=i, ours=str(o[i:i + 1])[:300], theirs=str(t[i:i + 1])[:300]))
        tot["hidden"] += sum('"status":"hidden"' in x for _, x in tv[-1])
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(tot, open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(json.dumps(dict(tot, diffs=tot["diffs"][:3], n_diffs=len(tot["diffs"])), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
