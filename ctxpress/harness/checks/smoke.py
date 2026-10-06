"""Smoke run of every method through the real proxy: a local fake Responses API stands in for the model, a scripted
agent replays a Codex-like session (growing history, long tool outputs, repeated reads), and each run ends with the
unified process statistics (`ctxpress analyze`). It checks that a method starts, rewrites real requests, calls its
summary / reflection / pruning models when it has them, and yields statistics. Usage is synthetic: these numbers
are never task quality or cost evidence.

    ctxpress smoke --output runs/smoke                 # every method
    ctxpress smoke --method DTOC --method CWL --turns 40 --output runs/smoke-two
"""
from __future__ import annotations
import argparse, copy, json, os, re, subprocess, sys, threading, time, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from ctxpress.methods import REGISTRY, build

MODEL = "gpt-6.1-sol"
PRICES = dict(models={MODEL: dict(input=2.0, cached=0.1, output=10.0, unit="USD_per_million_tokens")})
FILES = ["src/server.py", "src/handlers.py", "src/storage.py", "tests/test_server.py", "README.md"]


def smoke_args(name, inputs):
    """Arguments for a short session: thresholds low enough that each method acts within a few dozen requests."""
    inner = {"class": "ComplexityTrap", "args": {"budget": 4}}
    return {
        "ClaudeCode": {"t": 20000}, "CliffCompaction": {"t": 30000}, "ClearThenSummarize": {"t": 20000},
        "SlidingWindow": {"t": 20000}, "ComplexityTrap": {"budget": 4}, "KeepLastTokens": {"budget": 8000},
        "ClawVM": {"budget": 12000}, "ClawVMApprox": {"budget": 3}, "TokenPilot": {"budget": 800}, "ARC": {"budget": 3},
        "SWEPruner": {"url": inputs["pruner"]}, "ComplexityTrapSummary": {"n": 8, "m": 4},
        "ComplexityTrapHybrid": {"n": 12, "m": 4, "w": 4}, "CWL": {"budget": 10000}, "AgentFold": {"budget": 2, "deep": 4},
        "ACON": {"t_hist": 20000, "t_obs": 1000}, "ReSum": {"k": 10}, "WorkingView": {"trigger": 20000, "target": 12000},
        "CostModel": {"profile": inputs["profile"]}, "ScoredMethod": {"budget": 8000},
        "Composed": {"methods": [{"class": "EntryTruncation", "args": {"inner": inner, "budget": 1200}},
                                 {"class": "Pichay"}]},
        "EntryTruncation": {"inner": inner, "budget": 1200}, "PinRequirements": {"inner": inner},
        "WithMemory": {"inner": inner}, "Trigger": {"inner": inner, "threshold": 20000},
        "AutoCostModel": {"policy": inputs["policy"]},
    }.get(name, {})


def policy_inputs(directory):
    """A frozen AutoCostModel policy (and its statistics as a CostModel profile) screened on two synthetic histories."""
    from ctxpress.replay.tune import tune
    from ctxpress.core import policy

    def history(name, length=8):
        before = [dict(seg="call", size=5, kind="read", res=["a.py"], text="cat a.py"),
                  dict(seg="out", size=2500, kind="read", res=["a.py"], outpaths=[], sub="code", text="x\n" * 700)]
        reqs = [dict(before=before, input=0, cached=0, t=1)]
        for i in range(1, length):
            reqs.append(dict(before=[dict(seg="call", size=5, kind="command", res=[], text="true"),
                                     dict(seg="out", size=200, kind="command", res=[], outpaths=[], text="ok\n" * 200)],
                             input=0, cached=0, t=i + 1))
        return dict(name=name, prefix=50, alpha=1.0, reqs=reqs)

    path = Path(directory) / "smoke-policy.json"
    if not path.exists():
        tune([history("synthetic-a"), history("synthetic-b")], path, [0, 100],
             method_args=dict(allow_summary=False, use_reexplore=False, lookahead=4))
    return str(path), policy.load(path)["statistics"]


class FakeAPI:
    """Loopback Responses API (streamed or not) and a SWE-Pruner endpoint. Usage is estimated from the request size,
    with a cached prefix equal to the bytes shared with the previous main request."""

    def __init__(self):
        self.previous, self.calls, self.pruned, self.disabled, self.lock = "", [], 0, set(), threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path.endswith("/prune"):
                    api.pruned += 1
                    lines = body.get("code", "").splitlines()
                    kept = "\n".join(lines[: max(1, len(lines) // 3)])
                    raw = json.dumps(dict(pruned_code=kept, origin_token_cnt=len(lines), left_token_cnt=max(1, len(lines) // 3),
                                          model_input_token_cnt=len(body.get("code", "")) // 4)).encode()
                    self.send_response(200); self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
                    return
                api.respond(self, body)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def respond(self, handler, body):
        text = json.dumps(body.get("input", []), sort_keys=True)
        tools = {t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)}
        main = "shell" in tools
        with self.lock:
            shared = 0
            if main:
                limit = min(len(text), len(self.previous))
                while shared < limit and text[shared] == self.previous[shared]:
                    shared += 1
                self.previous = text
            self.calls.append(dict(main=main, chars=len(text)))
            turn = sum(1 for item in body.get("input", []) if isinstance(item, dict) and item.get("type") == "function_call_output")
        usage = dict(input_tokens=len(text) // 4, output_tokens=40, total_tokens=len(text) // 4 + 40,
                     input_tokens_details=dict(cached_tokens=shared // 4))
        namespace = next((t for t in body.get("tools") or [] if isinstance(t, dict) and t.get("type") == "namespace"
                          and t.get("name") == "mcp__ctxpress"), {})
        control = method_call(turn, {t.get("name") for t in namespace.get("tools") or []}, text, self.disabled) if main else None
        if control:
            item = dict(type="function_call", id=f"fc_{turn}", call_id=f"call_{turn}", namespace="mcp__ctxpress",
                        name=control[0], status="completed", arguments=json.dumps(control[1]))
        elif main:
            item = dict(type="function_call", id=f"fc_{turn}", call_id=f"call_{turn}", name="shell", status="completed",
                        arguments=json.dumps(dict(command=["bash", "-lc", command(turn)])))
        else:                                              # a summary, reflection or other side call
            item = dict(type="message", id=f"msg_{turn}", role="assistant", status="completed",
                        content=[dict(type="output_text", annotations=[],
                                      text="Synthetic summary: the task, the files read so far and the next step.")])
        response = dict(id=f"resp_{len(self.calls)}", object="response", created_at=1, status="completed", model=body.get("model"),
                        output=[item], usage=usage)
        if body.get("stream"):
            events = [dict(type="response.created", response=dict(response, status="in_progress", output=[])),
                      dict(type="response.output_item.done", output_index=0, item=item),
                      dict(type="response.completed", response=response)]
            raw = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
            kind = "text/event-stream"
        else:
            raw, kind = json.dumps(response).encode(), "application/json"
        handler.send_response(200); handler.send_header("Content-Type", kind)
        handler.send_header("Content-Length", str(len(raw))); handler.end_headers(); handler.wfile.write(raw)

    def close(self):
        self.server.shutdown(); self.server.server_close()


def method_call(step, names, text, disabled):
    """The scripted agent also uses a method's own tools, as the method's instructions ask: CWL marks exploration and
    action chunks, DTOC hides older outputs by tool key, ACM folds the history and later queries the folded memory."""
    if "delimiter" in names:                                       # CWL
        part = step // 9
        act = part % 3 == 2                                        # two exploration chunks, then an action on both
        if step % 9 == 0:
            if act:
                return "delimiter", dict(action="start", name=f"chunk-{part}", type="act",
                                         dependencies=[f"chunk-{part - 2}", f"chunk-{part - 1}"])
            return "delimiter", dict(action="start", name=f"chunk-{part}", type="expl")
        if step % 9 == 8:
            return "delimiter", (dict(action="end") if act else
                                 dict(action="end", description="Read the server, handlers and storage; the timeout is set in storage."))
    elif "query_memory" in names:                                  # ACM
        if step in (10, 20):
            return "manage_context", {}
        if step == 25:
            return "query_memory", dict(summary_id=1, query="where is the request timeout handled?")
    elif "manage_context" in names and step and step % 6 == 0:     # DTOC
        keys = [k for k in dict.fromkeys(re.findall(r"tk_[0-9a-z]+", text)) if k not in disabled][:3]
        if keys:
            disabled.update(keys)
            return "manage_context", dict(enable=[], disable=keys)
    return None


class MethodToolServer:
    """`ctxpress mcp` as Codex starts it (stdio JSON-RPC, run-specific store, control receipts)."""

    def __init__(self, entry, store, log, home):
        env = dict(os.environ, CTXPRESS_HOME=str(home), CTXPRESS_STORE_DIR=str(store), CTXPRESS_LOG_PATH=str(log),
                   CTXPRESS_METHOD_CONFIG=json.dumps(entry), CTXPRESS_REQUIRED_TOOLS="1", CTXPRESS_CONTROL_RECEIPTS="1",
                   PYTHONPATH=os.pathsep.join(p for p in (str(Path(__file__).resolve().parents[3]), os.environ.get("PYTHONPATH")) if p))
        self.process = subprocess.Popen([sys.executable, "-m", "ctxpress", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        env=env, text=True, encoding="utf-8")
        self.next_id = 0
        self.request("initialize", dict(protocolVersion="2025-06-18", capabilities={}, clientInfo=dict(name="smoke")))

    def request(self, method, params):
        self.next_id += 1
        self.process.stdin.write(json.dumps(dict(jsonrpc="2.0", id=self.next_id, method=method, params=params)) + "\n")
        self.process.stdin.flush()
        reply = json.loads(self.process.stdout.readline())
        if "error" in reply:
            raise RuntimeError(f"ctxpress mcp {method}: {reply['error']}")
        return reply["result"]

    def call(self, name, arguments):
        """The output Codex records: the tool text, then the receipt on its own line."""
        result = self.request("tools/call", dict(name=name, arguments=arguments))
        return "\n".join(part["text"] for part in result["content"])

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=10)


def command(turn):
    """The scripted agent: read files (some twice), search, run tests, then edit; long reads ask a focus question."""
    path = FILES[turn % len(FILES)]
    kinds = ["read", "search", "read", "test", "read", "edit"]
    kind = kinds[turn % len(kinds)]
    if kind == "read":
        return f"nl -ba {path}  # context_focus_question: where is the request timeout handled?"
    if kind == "search":
        return "grep -rn timeout src tests"
    if kind == "test":
        return "python -m pytest tests -q"
    return f"apply_patch <<'EOF'\n*** Update File: {path}\n@@\n-TIMEOUT = 5\n+TIMEOUT = 30\nEOF"


def run_output(cmd, turn, chars):
    if cmd.startswith("nl -ba"):
        path = cmd.split()[2]
        lines = [f"{i:6}\tdef handler_{turn}_{i}(request):  # {path}" + " timeout" * (i % 7 == 0) for i in range(1, 400)]
        return "\n".join(lines)[:chars]
    if cmd.startswith("grep"):
        return "\n".join(f"{FILES[i % 3]}:{i}: TIMEOUT = {i}" for i in range(60))[: chars // 3]
    if cmd.startswith("python -m pytest"):
        return ("F" * 10 + "." * 50 + "\nFAILED tests/test_server.py::test_timeout - AssertionError\n" + "trace line\n" * 200)[:chars]
    return "Success. Updated the following files:\nM " + cmd.split("*** Update File: ")[1].split("\n")[0]


def drive(proxy, turns, chars, method_tools=None):
    """A Codex-like client: the whole history every request, streamed responses, the model's call then its output."""
    instructions = "You are a coding agent. Use the shell tool to inspect and fix the repository."
    tools = [dict(type="function", name="shell", description="Run a shell command.",
                  parameters=dict(type="object", properties=dict(command=dict(type="array", items=dict(type="string"))),
                                  required=["command"]))]
    history = [dict(type="message", role="user", content=[dict(type="input_text",
               text="The server times out on large uploads. Find the cause in src/ and fix it; tests must pass.")])]
    statuses = []
    for turn in range(turns):
        body = dict(model=MODEL, instructions=instructions, tools=tools, tool_choice="auto", parallel_tool_calls=False,
                    input=copy.deepcopy(history), stream=True, store=False, prompt_cache_key="smoke")
        request = urllib.request.Request(proxy + "/responses", json.dumps(body).encode(),
                                         {"Content-Type": "application/json", "Authorization": "Bearer smoke"})
        with urllib.request.urlopen(request, timeout=120) as response:
            statuses.append(response.status)
            events = [json.loads(line[6:]) for line in response.read().decode().splitlines() if line.startswith("data: ")]
        done = [e["item"] for e in events if e.get("type") == "response.output_item.done"]
        call = next((i for i in done if i.get("type") == "function_call"), None)
        if call is None:
            raise RuntimeError(f"turn {turn}: the fake model returned no tool call")
        if call.get("namespace") == "mcp__ctxpress":
            output = method_tools.call(call["name"], json.loads(call["arguments"]))
        else:
            output = run_output(json.loads(call["arguments"])["command"][-1], turn, chars)
        history += [call, dict(type="function_call_output", call_id=call["call_id"], output=output)]
    return statuses


def run_method(name, directory, inputs, turns, chars, args=None):
    from ctxpress.live.proxy import serve
    from ctxpress.live.control_receipts import ControlReceipts
    from ctxpress.harness.results.analysis import analyze

    folder = Path(directory) / name
    folder.mkdir(parents=True, exist_ok=False)
    entry = {"class": name, "args": smoke_args(name, inputs) if args is None else args}
    log, store = folder / "requests.jsonl", folder / "store" / uuid.uuid4().hex   # a launch-specific store, as `ctxpress codex` makes
    row = dict(method=name, args=entry["args"], ok=False)
    api = FakeAPI()
    srv = tools = None
    started = time.time()
    try:
        sample = build(entry)
        try:
            sample.validate_live()
        except Exception as error:                        # e.g. a method that only runs in the replay simulator
            row.update(live=False, reason=f"{type(error).__name__}: {error}")
            return row
        receipts = ControlReceipts(str(store), str(log)) if sample.agent_tools else None
        srv, rewriter = serve(lambda: build(entry), 0, api.url + "/v1", log=str(log), host="127.0.0.1", store_dir=str(store),
                              store_prefix=str(store), retrieve_tool=True, codex_method_tools=True, control_receipts=receipts)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        if sample.agent_tools:
            tools = MethodToolServer(entry, store, log, folder / "home")
        statuses = drive(f"http://127.0.0.1:{srv.server_address[1]}", turns, chars, tools)
        settle(log)
        analysis, _ = analyze(log, prices=PRICES)
        (folder / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        job = analysis["families"][0]["jobs"][0]
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        row.update(live=True, ok=all(s == 200 for s in statuses) and len(statuses) == turns, requests=len(statuses),
                   rewritten=sum(1 for r in rows if r.get("changed") or r.get("operations")),
                   model_side_calls=sum(1 for c in api.calls if not c["main"]), pruner_calls=inputs["pruned"](),
                   operations=_operations(rows), log_rows=len(rows), self_checks=_checks(job),
                   stats=_stats(job), seconds=round(time.time() - started, 2))
        return row
    except Exception as error:
        row.update(error=f"{type(error).__name__}: {error}")
        return row
    finally:
        if tools is not None:
            tools.close()
        if srv is not None:
            srv.shutdown(); srv.server_close()
        api.close()


def settle(log, quiet=0.3, limit=15.0):
    """The proxy logs a request after its response has streamed; wait until the log stops growing and ends a line."""
    deadline, last, still = time.time() + limit, None, time.time()
    while time.time() < deadline:
        raw = log.read_bytes() if log.exists() else b""
        if raw != last:
            last, still = raw, time.time()
        elif raw.endswith(b"\n") and time.time() - still >= quiet:
            return
        time.sleep(0.05)
    raise TimeoutError("the request log did not settle")


def _operations(rows):
    out = {}
    for r in rows:
        for key, value in (r.get("operations") or {}).items():
            if isinstance(value, (int, float)):
                out[key] = out.get(key, 0) + value
    return out


def _checks(job):
    checks = job.get("checks") or {}
    problems = [*(job.get("problems") or []), *(checks.get("hard") or [])]
    return dict(passed=not problems, problems=[str(p)[:200] for p in problems][:5], warnings=checks.get("warnings") or [])


def _stats(job):
    """The headline numbers of the unified process analysis (analysis.json has all of them)."""
    tokens, context, cache, cost = job["tokens"], job["context"], job["cache"], job["cost"]
    return dict(main_input_tokens=tokens["main"]["input"], cache_read_share=cache.get("read_share"),
                aux_input_tokens=tokens["aux"]["input"], cost_usd=cost.get("total"),
                max_api_input=(context.get("api_input") or {}).get("max"), history_reduction=context.get("history_reduction"),
                cache_breaks_method=cache.get("breaks_method"), aux_calls=job["efficiency"].get("aux_calls"),
                operations=(job.get("activity") or {}).get("operations"))


def smoke(directory, methods=None, turns=30, chars=6000):
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("smoke output must be a new or empty directory")
    directory.mkdir(parents=True, exist_ok=True)
    names = methods or list(REGISTRY)
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise ValueError(f"unknown methods: {', '.join(unknown)}")
    policy_path, profile = policy_inputs(directory)
    inputs = dict(policy=policy_path, profile=profile, pruner=None, pruned=lambda: 0)
    rows = []
    for name in names:
        api_for_pruner = None
        if name == "SWEPruner":
            api_for_pruner = FakeAPI()
            inputs["pruner"] = api_for_pruner.url + "/prune"
            inputs["pruned"] = lambda api=api_for_pruner: api.pruned
        try:
            rows.append(run_method(name, directory, inputs, turns, chars))
        finally:
            if api_for_pruner:
                api_for_pruner.close()
                inputs["pruned"] = lambda: 0
    report = dict(schema="ctxpress.smoke", version=1, synthetic=True, model=MODEL, turns=turns, output_chars=chars,
                  methods=rows, passed=all(r.get("ok") or r.get("live") is False for r in rows),
                  limitations=["A fake model and scripted agent: no task quality, no real cost, no benchmark evidence.",
                               "Method tools (CWL, DTOC, ACM) run in the real `ctxpress mcp` server, called on a fixed schedule."])
    (directory / "smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (directory / "smoke.md").write_text(markdown(report), encoding="utf-8")
    return report


def markdown(report):
    lines = [f"# Smoke run (synthetic: fake model, {report['turns']} scripted turns)", "",
             "| method | ran | requests | rewritten | model side calls | operations | max input | history cut | cache read | cost (USD) | self-checks |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in report["methods"]:
        if r.get("live") is False:
            lines.append(f"| {r['method']} | replay only | — | — | — | — | — | — | — | — | {r.get('reason', '')[:60]} |")
            continue
        if not r.get("ok"):
            lines.append(f"| {r['method']} | **no**: {r.get('error', '')[:100]} |" + " — |" * 9)
            continue
        ops = dict(r.get("operations") or {})
        if r.get("pruner_calls"):
            ops["pruner_calls"] = r["pruner_calls"]
        ops = ", ".join(f"{k} {v}" for k, v in sorted(ops.items())) or "—"
        st = r.get("stats") or {}
        checks = "pass" if (r.get("self_checks") or {}).get("passed") else "problems"
        lines.append(f"| {r['method']} | yes | {r['requests']} | {r['rewritten']} | {r['model_side_calls']} | {ops[:70]} | "
                     f"{st.get('max_api_input')} | {_pct(st.get('history_reduction'))} | {_pct(st.get('cache_read_share'))} | "
                     f"{st.get('cost_usd', 0):.4f} | {checks} |")
    lines += ["", "Synthetic usage from a fake model; not quality or cost evidence."]
    return "\n".join(lines) + "\n"


def _pct(value):
    return "—" if value is None else f"{100 * value:.0f}%"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ctxpress smoke", description=__doc__.split("\n\n")[0])
    ap.add_argument("--method", action="append", help="method to run (default: every method)")
    ap.add_argument("--turns", type=int, default=30)
    ap.add_argument("--output-chars", type=int, default=6000)
    ap.add_argument("--output", required=True, help="new directory for smoke.json, smoke.md and one folder per method")
    a = ap.parse_args(argv)
    report = smoke(a.output, a.method, a.turns, a.output_chars)
    print((Path(a.output) / "smoke.md").read_text(encoding="utf-8"))
    return 0 if report["passed"] else 1
