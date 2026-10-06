"""AgentDiet: ours vs the authors' reflection module and MessageManager (artifact, figshare 30073654).

The authors' `traj_analyzer.py` (unmodified) and `MessageManager` (taken from `agents/expert.py` by name, with
SYS_PROMPT and INIT_USER_PROMPT) run on agent steps from the Complexity Trap's released SWE-agent trajectories
(each step: the model's text and tool call, then the tool's result), calling `maybe_perform_analysis_step` after
every step as `Expert.run` does. Ours runs the same steps through LiveContext with AgentDiet. Both sides get the
same deterministic stand-in for the reflection model, a function of (system prompt, user prompt): two thirds of
the steps get a short rewrite (accepted), one third a long one (rejected). Per step we compare every message the
agent model would receive next. Stubs: the model client, tiktoken (both sides count len // 4 + 4) and lz4.
Our requests cannot prefill the answer, so ours appends an output-format sentence; the stand-in removes it before
hashing, so equal results still mean the authors' prompt text was reproduced.

    python repro/agentdiet_compare.py [--limit 50]
"""
from __future__ import annotations
import argparse, ast, glob, hashlib, importlib.util, json, os, re, sys, types
from collections import defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA = os.environ.get("CTXPRESS_DATA", os.path.join(ROOT, "data"))
sys.path.insert(0, os.path.join(ROOT, "ctxpress"))
from ctxpress.live.context import LiveContext
from ctxpress.methods import AgentDiet

SRC = os.path.join(DATA, "repro", "agentdiet", "artifact", "code", "trae_agent")
RAW = os.path.join(DATA, "repro", "ct-traj", "trajectories", "lindenbauer", "main_experiments",
                   "gemini_2.5_flash-agent-t_0.8-baseline_raw.0-verified-500")
MODEL = "gpt-5-mini-2025-08-07"                 # the paper's reflection model (main experiment)
SUFFIX = "\n\nAnswer exactly as:"


def answer(system, user):
    h = hashlib.sha256((system + "\x00" + user).encode("utf-8")).hexdigest()
    return "z" * len(user) if int(h, 16) % 3 == 0 else f"compressed {h[:16]}"


def load_authors():
    tok = types.ModuleType("tiktoken")
    tok.encoding_for_model = lambda name: types.SimpleNamespace(encode=lambda s: [0] * (len(s) // 4 + 4))
    lz4 = types.ModuleType("lz4"); lz4.frame = types.ModuleType("lz4.frame"); lz4.frame.compress = lambda b: b
    utils = types.ModuleType("utils"); utils.__path__ = []
    poly = types.ModuleType("utils.llm_polytool")

    def get_llm_response(model, msgs, tools, kwargs):
        system = msgs[0]["content"] if isinstance(msgs[0]["content"], str) else msgs[0]["content"][0]["text"]
        return [{"content": answer(system, msgs[1]["content"])}], ["stop"], dict(total_tokens=1, prompt_tokens=1, completion_tokens=1)
    poly.get_llm_response = get_llm_response
    sys.modules.update({"tiktoken": tok, "lz4": lz4, "lz4.frame": lz4.frame, "utils": utils, "utils.llm_polytool": poly})
    os.environ["TRAJ_ANALYSIS"] = json.dumps({"mode": "ours", "model": MODEL})
    spec = importlib.util.spec_from_file_location("traj_analyzer", os.path.join(SRC, "agents", "traj_analyzer.py"))
    ta = importlib.util.module_from_spec(spec); spec.loader.exec_module(ta)
    tree = ast.parse(open(os.path.join(SRC, "agents", "expert.py"), encoding="utf-8").read())
    keep = [n for n in tree.body if (isinstance(n, ast.ClassDef) and n.name == "MessageManager") or
            (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in ("SYS_PROMPT", "INIT_USER_PROMPT") for t in n.targets))]
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "expert.py", "exec"), ns)
    return ta, ns["MessageManager"], ns["INIT_USER_PROMPT"]


class OurReflector:
    def summarize_prompt(self, system, user, purpose="history", model=None):
        assert model == MODEL
        core = user.split(SUFFIX)[0]
        idx = re.search(r"Now, compress the step (-?\d+)\.$", core).group(1)
        return f'Sure. Here is the compressed content of step {idx}: <step id="{idx}">{answer(system, core)}</step>'


def text(c):
    return c if isinstance(c, str) else "".join(x.get("text", "") for x in c)


def theirs_view(mgr):
    out = []
    for m in mgr.format_messages()[2:]:
        if m["role"] == "assistant":
            out.append(("assistant", text(m["content"])))
            for tc in m.get("tool_calls") or []:
                out.append(("call", tc["function"]["name"], tc["function"].get("arguments", "")))
        elif m["role"] == "tool":
            out.append(("tool", text(m["content"])))
    return out


def ours_view(ctx):
    out = []
    for v, s in zip(ctx.view(), ctx.pinned + ctx.ctx):
        if v["seg"] in ("summary",) or (v["seg"] == "msg" and v["role"] == "assistant"):
            out.append(("assistant", v["text"]))
        elif v["seg"] == "call":
            out.append(("call", s.get("name"), s.get("args", "")))
        elif v["seg"] == "out":
            out.append(("tool", v["text"]))
    return out


def check(path, ta, MessageManager, init_prompt):
    hist = json.load(open(path, encoding="utf-8"))["history"]
    task = hist[1]["content"]
    mgr = MessageManager("/testbed", task, None, defaultdict(int), False, 100, [])
    ctx = LiveContext(AgentDiet(reflect_model=MODEL), summarizer=OurReflector())
    ctx.add_message("system", "agent system prompt", fixed=True)
    ctx.add_message("user", init_prompt.format(project_path="/testbed", issue=task))
    res = dict(steps=0, same=0, erased=0, analyses=0, first_diff=None)
    j = 2
    while j + 1 < len(hist) and hist[j]["role"] == "assistant" and hist[j].get("tool_calls"):
        a, t = hist[j], hist[j + 1]
        calls = [{"id": c.get("id", ""), "type": "function", "function": dict(name=c["function"]["name"],
                  arguments=c["function"].get("arguments", ""))} for c in a["tool_calls"]]
        mgr.push_step({"role": "assistant", "content": a["content"], "tool_calls": calls},
                      [{"role": "tool", "content": t["content"], "agent_caller": ("bash", {})}])
        ta.maybe_perform_analysis_step(mgr)
        ctx.add_message("assistant", a["content"])
        for c in calls:
            ctx.add_call(c["id"], a.get("action") or "", name=c["function"]["name"], args=c["function"]["arguments"])
        ctx.add_output(calls[0]["id"], t["content"])
        ctx.before_request()
        theirs, ours = theirs_view(mgr), ours_view(ctx)
        res["steps"] += 1
        if ours == theirs:
            res["same"] += 1
        elif res["first_diff"] is None:
            i = next((i for i, (x, y) in enumerate(zip(ours, theirs)) if x != y), min(len(ours), len(theirs)))
            res["first_diff"] = dict(step=res["steps"] - 1, at=i, ours=str(ours[i:i + 1])[:300], theirs=str(theirs[i:i + 1])[:300])
        j += 2
    res["erased"] = mgr.metrics["erase_tot_count"]; res["analyses"] = mgr.metrics["analysis_count"]
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=os.path.join(ROOT, "ctxpress", "runs", "repro", "agentdiet.json"))
    a = ap.parse_args()
    ta, MessageManager, init_prompt = load_authors()
    tot = dict(instances=0, steps=0, same=0, analyses=0, erased=0, diffs=[])
    for p in sorted(glob.glob(os.path.join(RAW, "*", "*.traj")))[:a.limit]:
        r = check(p, ta, MessageManager, init_prompt)
        tot["instances"] += 1
        for k in ("steps", "same", "analyses", "erased"):
            tot[k] += r[k]
        if r["first_diff"] is not None:
            tot["diffs"].append((os.path.basename(p), r["first_diff"]))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(tot, open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(json.dumps(dict(tot, diffs=tot["diffs"][:3], n_diffs=len(tot["diffs"])), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
