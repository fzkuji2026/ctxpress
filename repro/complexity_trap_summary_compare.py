"""The Complexity Trap's LLM-Summary and hybrid: ours vs the authors' SWE-agent history processors.

The authors' `SummarizeEveryNTurns` and `LastNObservations` (sweagent/agent/history_processors.py, unmodified) and
their summary prompt builder (`_construct_user_prompt_for_summary`, taken from sweagent/agent/models.py by name) run
on the raw histories of their released unmanaged ("raw") SWE-agent run, step by step, exactly as the agent calls
them before each model query. Ours runs the same steps through LiveContext. Both sides get the same deterministic
stand-in for the summary model: the summary is a hash of (system prompt, user prompt), so equal summaries mean the
prompts were equal too. Per step we compare the whole request: roles, texts, summaries and placeholders.
Only `models.py` (LiteLLM client) and `utils/log.py` (rich logging) are replaced by stubs to import the module.

    python repro/complexity_trap_summary_compare.py [--limit 50] [--mode hybrid|summary]
"""
from __future__ import annotations
import argparse, ast, glob, hashlib, json, os, sys, types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # this repository, whatever its folder name
DATA = os.environ.get("CTXPRESS_DATA", os.path.join(ROOT, "data"))
sys.path.insert(0, REPO)
from ctxpress.live.context import LiveContext
from ctxpress.methods import ComplexityTrapSummary, ComplexityTrapHybrid
from ctxpress.methods.summaries import CT_SUMMARY_SYSTEM

SRC = os.path.join(DATA, "repro", "the-complexity-trap")
RAW = os.path.join(DATA, "repro", "ct-traj", "trajectories", "lindenbauer", "main_experiments",
                   "gemini_2.5_flash-agent-t_0.8-baseline_raw.0-verified-500")
CONFIGS = {"hybrid": "default_no_demo_checkpoint_same_model_openhands_N=43_M=10_masking_M=10.yaml",
           "summary": "default_no_demo_checkpoint_same_model_openhands_N=21_M=10.yaml"}


def fake_summary(system, user):
    return "SUMMARY " + hashlib.sha256((system + "\x00" + user).encode("utf-8")).hexdigest()[:16]


def load_authors():
    """Import the authors' history_processors with the two heavy modules stubbed (package __init__s skipped)."""
    for name, rel in (("sweagent", "sweagent"), ("sweagent.agent", "sweagent/agent"), ("sweagent.utils", "sweagent/utils")):
        pkg = types.ModuleType(name)
        pkg.__path__ = [os.path.join(SRC, *rel.split("/"))]
        sys.modules[name] = pkg
    models = types.ModuleType("sweagent.agent.models")
    models.AbstractModel = object
    log = types.ModuleType("sweagent.utils.log")
    log.get_logger = lambda *a, **k: types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    sys.modules.update({"sweagent.agent.models": models, "sweagent.utils.log": log})
    import importlib
    hp = importlib.import_module("sweagent.agent.history_processors")
    tree = ast.parse(open(os.path.join(SRC, "sweagent", "agent", "models.py"), encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_construct_user_prompt_for_summary")
    ns = dict(Turns=list)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "models.py", "exec"), ns)
    return hp, ns["_construct_user_prompt_for_summary"]


def config(mode):
    import yaml
    cfg = yaml.safe_load(open(os.path.join(SRC, "config", CONFIGS[mode]), encoding="utf-8"))
    agent = cfg["agent"]
    return agent["templates"]["summary_system_template"], agent["history_processors"]


class AuthorModel:
    """What the authors' SummarizeEveryNTurns needs from the model, with the stand-in summary."""
    def __init__(self, system, build):
        self.system, self.build = system, build
        self.config = types.SimpleNamespace(per_instance_call_limit=0)
        self.stats = types.SimpleNamespace(api_calls=0)

    def query_for_summary(self, context, turns, extract, max_action, max_reasoning):
        from sweagent.types import SummaryMetadata
        user, _ = self.build(None, context, turns, extract, max_action, max_reasoning)
        return SummaryMetadata(summary=fake_summary(self.system, user), context=[])


class OurSummarizer:
    def summarize_prompt(self, system, user, purpose="history"):
        return fake_summary(system, user)


def theirs_view(history):
    out = []
    for e in history:
        tags = e.get("tags") or []
        if any(isinstance(t, dict) and t.get("type") == "summary" for t in tags):
            out.append(("summary", e["content"]))
        elif e["role"] == "assistant":
            out.append(("assistant", e["content"]))
        elif e["role"] in ("tool", "user") and e.get("message_type") == "observation":
            out.append(("obs", e["content"]))
        else:
            out.append((e["role"], e["content"]))
    return out


def ours_view(ctx):
    out = []
    for v in ctx.view():
        if v["seg"] == "summary":
            out.append(("summary", v["text"]))
        elif v["seg"] == "msg" and v["role"] == "assistant":
            out.append(("assistant", v["text"]))
        elif v["seg"] == "out" or (v["seg"] == "msg" and v["role"] == "user"):
            out.append(("obs", v["text"]))
        elif v["seg"] == "msg":
            out.append((v["role"], v["text"]))
    return out


def check(path, mode, hp, build, system, procs):
    t = json.load(open(path, encoding="utf-8"))
    hist = t["history"]
    model = AuthorModel(system, build)
    chain = []
    for p in procs:
        kind = p["type"]
        cls = {"summarize_every_n_turns": hp.SummarizeEveryNTurns, "last_n_observations": hp.LastNObservations}[kind]
        proc = cls(**{k: v for k, v in p.items() if k != "type"})
        if kind == "summarize_every_n_turns":
            proc.set_model(model)
        chain.append(proc)
    s = next(p for p in procs if p["type"] == "summarize_every_n_turns")
    method = ComplexityTrapHybrid(s["n"], s["keep_last_m_turns"], next(p["n"] for p in procs if p["type"] == "last_n_observations")) \
        if mode == "hybrid" else ComplexityTrapSummary(s["n"], s["keep_last_m_turns"])
    ctx = LiveContext(method, summarizer=OurSummarizer())
    res = dict(steps=0, same=0, summaries=0, masked=0, first_diff=None)
    fed = 0
    ends = [len(st["messages"]) for st in t["trajectory"]]   # the raw run's recorded steps (no processors)
    # A model query follows a tool call's observation. The step after the harness's own exit message (an assistant
    # entry without a tool call: "Exit due to cost limit", then "Exited (autosubmitted)") is not one: skipped.
    exit_step = lambda e: e >= 2 and hist[e - 2]["role"] == "assistant" and not hist[e - 2].get("tool_calls")
    res["exit_steps"] = sum(map(exit_step, ends))
    ends = [e for e in ends if not exit_step(e)]
    for end in ends:
        for j in range(fed, end):
            e = hist[j]
            if e["role"] == "system":
                ctx.add_message("system", e["content"], fixed=True)
            elif e["role"] == "assistant":
                cid = ",".join(c.get("id", "") for c in e.get("tool_calls") or []) or f"a{j}"
                ctx.add_message("assistant", e["content"])
                ctx.add_call(cid, e.get("action") or "")
            elif j > 1:
                cid = ",".join(e.get("tool_call_ids") or []) or f"o{j}"
                ctx.add_output(cid, e["content"])
            else:
                ctx.add_message("user", e["content"])
        fed = end
        processed = [dict(x) for x in hist[:end]]
        for proc in chain:
            processed = proc(processed)
        theirs = theirs_view(processed)
        ctx.before_request()
        ours = ours_view(ctx)
        res["steps"] += 1
        res["summaries"] = max(res["summaries"], sum(k == "summary" for k, _ in theirs))
        res["masked"] += sum(k == "obs" and x.startswith("Old environment output:") for k, x in theirs)
        if ours == theirs:
            res["same"] += 1
        elif res["first_diff"] is None:
            i = next((i for i, (a, b) in enumerate(zip(ours, theirs)) if a != b), min(len(ours), len(theirs)))
            res["first_diff"] = dict(step=res["steps"] - 1, at=i, ours=str(ours[i:i + 1])[:300],
                                     theirs=str(theirs[i:i + 1])[:300], n_ours=len(ours), n_theirs=len(theirs))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=sorted(CONFIGS), default="hybrid")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    hp, build = load_authors()
    system, procs = config(a.mode)
    assert system == CT_SUMMARY_SYSTEM, "summary prompt differs from the authors' config"
    trajs = sorted(glob.glob(os.path.join(RAW, "*", "*.traj")))[:a.limit]
    tot = dict(mode=a.mode, config=CONFIGS[a.mode], instances=0, steps=0, same=0, exit_steps_skipped=0, with_summary=0, with_two_or_more=0,
               masked=0, diffs=[])
    for p in trajs:
        r = check(p, a.mode, hp, build, system, procs)
        tot["instances"] += 1
        tot["steps"] += r["steps"]; tot["same"] += r["same"]; tot["masked"] += r["masked"]
        tot["exit_steps_skipped"] += r["exit_steps"]
        tot["with_summary"] += r["summaries"] > 0
        tot["with_two_or_more"] += r["summaries"] > 1
        if r["first_diff"] is not None:
            tot["diffs"].append((os.path.basename(p), r["first_diff"]))
    out = a.out or os.path.join(REPO, "runs", "repro", f"complexity_trap_{a.mode}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(tot, open(out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(json.dumps(dict(tot, diffs=tot["diffs"][:3], n_diffs=len(tot["diffs"])), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
