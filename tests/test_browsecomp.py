"""BrowseComp-Plus with the ACM author agent: planning, and a full job against a stand-in author checkout.

The stand-in has the author's entry points (`python -m src.run`, `src.evaluator.evaluate_browsecomp_plus`) and the
same wire behaviour (LiteLLM-style Chat Completions with tools, a separate summarizer, a Responses-API judge),
so the job runs every real part of ctxpress: the proxies, key injection, the method and the logs.
"""
import json, sys, threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from ctxpress import benchmarks
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.live.proxy import ThreadingHTTPServer

RUN = r'''
import argparse, json, os, sys, urllib.request
ap = argparse.ArgumentParser()
for flag in ("--mode", "--client", "--benchmark", "--model", "--agent_api_base", "--summarizer_model", "--summarizer_api_base",
             "--index_path", "--data", "--results_dir", "--run_dir", "--run_id", "--config"):
    ap.add_argument(flag)
ap.add_argument("--no_skip", action="store_true"); ap.add_argument("--use_memory_tool", action="store_true")
ap.add_argument("--override", nargs="*", default=[])
a = ap.parse_args()

def post(base, body):
    request = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]})
    return json.load(urllib.request.urlopen(request, timeout=30))

question = json.load(open(a.data))[0]
served = a.model.split("/", 1)[1]                     # LiteLLM strips the provider prefix
tools = [{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}]
messages = [{"role": "system", "content": "Research agent."}, {"role": "user", "content": question["question"]}]
turns = 0
while True:
    turns += 1
    sent = [dict(m) for m in messages]                 # the author marks only the newest tool result
    tool = [m for m in sent if m["role"] == "tool"]
    if tool:
        tool[-1]["content"] += "\n\n[CURRENT CONTEXT TOKEN: %d]" % (1000 * turns)
    reply = post(a.agent_api_base, {"model": served, "messages": sent, "tools": tools, "max_tokens": 100})
    message = reply["choices"][0]["message"]
    messages.append({k: v for k, v in message.items() if v is not None})
    if not message.get("tool_calls"):
        break
    for call in message["tool_calls"]:
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": "document text " + "z" * 4000})
if a.use_memory_tool:
    post(a.summarizer_api_base, {"model": a.summarizer_model, "messages": [{"role": "user", "content": "compress"}]})
out = os.path.join(a.results_dir, a.benchmark, served, a.run_dir, a.run_id)
os.makedirs(out, exist_ok=True)
json.dump(dict(status="complete", final_answer=message["content"], num_turns=turns, tool_call_counts={"search": turns - 1},
               mem_operations=[], usage={}, key_seen=os.environ["OPENAI_API_KEY"],
               result=[{"type": "output_text", "output": message["content"]}]),
          open(os.path.join(out, "run_%s.json" % question["id"]), "w"))
'''

EVALUATOR = r'''
import glob, json, os, urllib.request

def evaluate_browsecomp_plus(input_dir, ground_truth, eval_dir, model, qrel_evidence_path=None):
    os.makedirs(eval_dir, exist_ok=True)
    for path in glob.glob(os.path.join(input_dir, "run_*.json")):
        response = json.load(open(path))["result"][-1]["output"]
        request = urllib.request.Request(os.environ["OPENAI_BASE_URL"] + "/responses",
            data=json.dumps({"model": model, "input": "judge: " + response}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]})
        text = json.load(urllib.request.urlopen(request, timeout=30))["output_text"]
        stem = os.path.basename(path)[:-5]
        json.dump({"judge_result": {"correct": "correct: yes" in text, "confidence": 90, "parse_error": False},
                   "retrieval": {"recall": 0.5}}, open(os.path.join(eval_dir, stem + "_eval.json"), "w"))
'''


class Upstream(BaseHTTPRequestHandler):
    """The served agent model, the summarizer and the judge, recording each request's path and credential."""
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Upstream.seen.append((self.path, self.headers.get("Authorization"), body))
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 80}}
        if self.path.endswith("/responses"):
            reply = {"model": body["model"], "output_text": "correct: yes", "usage": {"input_tokens": 30, "output_tokens": 5}}
        elif body.get("tools"):
            done = sum(m["role"] == "tool" for m in body["messages"]) >= 3
            message = ({"role": "assistant", "content": "Exact Answer: Paris"} if done else
                       {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{len(body['messages'])}", "type": "function",
                        "function": {"name": "search", "arguments": "{\"query\": \"capital\"}"}}]})
            reply = {"model": body["model"], "choices": [{"index": 0, "message": message,
                     "finish_reason": "stop" if done else "tool_calls"}], "usage": usage}
        else:
            reply = {"model": body["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": "summary"},
                     "finish_reason": "stop"}], "usage": usage}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    checkout = tmp_path / "acm"
    (checkout / "src").mkdir(parents=True)
    (checkout / "src" / "__init__.py").write_text("")
    (checkout / "src" / "run.py").write_text(RUN)
    (checkout / "src" / "evaluator.py").write_text(EVALUATOR)
    files = {f"src/{name}": eval_plan.file_sha256(checkout / "src" / name) for name in ("__init__.py", "run.py", "evaluator.py")}
    from ctxpress.harness import author_acm
    monkeypatch.setattr(author_acm, "verify_checkout", lambda root: (Path(root).resolve(), files))
    data = tmp_path / "bcp" / "questions.json"
    data.parent.mkdir()
    data.write_text(json.dumps([{"id": "101", "question": "What is the capital of France?", "answer": "Paris"},
                                {"query_id": 102, "query": "Second question?", "answer": "x"}]))
    (tmp_path / "bcp" / "truth.jsonl").write_text(json.dumps({"query_id": "101", "answer": "Paris"}) + "\n")
    (tmp_path / "bcp" / "qrels.txt").write_text("101 0 doc1 1\n")
    (tmp_path / "index").mkdir()
    (tmp_path / "index" / "segments_1").write_text("lucene")
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    worker = threading.Thread(target=upstream.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{upstream.server_port}/v1"
    config = dict(schema="ctxpress.eval", version=1, scope="benchmark", benchmark="browsecomp-plus", start_mode="task_start",
                  backend="acm_author", model="openai/acm-9b",
                  environment=dict(checkout=str(checkout), python=sys.executable, data=str(data),
                                   ground_truth=str(tmp_path / "bcp" / "truth.jsonl"), qrels=str(tmp_path / "bcp" / "qrels.txt"),
                                   index=str(tmp_path / "index"), agent_upstream=base,
                                   summarizer=dict(model="gpt-mini", upstream=base), grader=dict(model="judge", upstream=base)),
                  tasks=["101"], methods=[{"class": "NoCompaction"}, {"class": "ComplexityTrap", "args": {"n": 1}, "label": "masking"}],
                  run=dict(use_memory_tool=True, timeout=120))
    Upstream.seen = []
    yield config, tmp_path
    upstream.shutdown(); upstream.server_close(); worker.join(timeout=2)


def test_plan_binds_author_sources_questions_and_index(prepared):
    config, tmp = prepared
    plan = benchmarks.get("browsecomp-plus").compile_plan(config, tmp)
    assert [job["label"] for job in plan["jobs"]] == ["NoCompaction", "masking"]
    task = plan["jobs"][0]["task"]
    assert task["id"] == "101" and "answer" not in json.dumps(task["initial_state"]).lower()
    assert any(path.endswith("segments_1") for path in plan["artifacts"])
    assert any(path.endswith("run.py") for path in plan["artifacts"])
    assert plan["config"]["environment"]["agent_upstream"].endswith("/v1")
    eval_plan.verify(plan)


def test_plan_rejects_unknown_questions_tool_methods_and_inline_credentials(prepared):
    config, tmp = prepared
    adapter = benchmarks.get("browsecomp-plus")
    with pytest.raises(ValueError, match="unknown"):
        adapter.compile_plan(dict(config, tasks=["999"]), tmp)
    with pytest.raises(ValueError, match="own agent tools"):
        adapter.compile_plan(dict(config, methods=[{"class": "DTOC"}]), tmp)
    environment = dict(config["environment"], agent_upstream="http://user:secret@127.0.0.1:1/v1")
    with pytest.raises(ValueError, match="credentials"):
        adapter.compile_plan(dict(config, environment=environment), tmp)
    with pytest.raises(ValueError, match="openai/"):
        adapter.compile_plan(dict(config, model="acm-9b"), tmp)


@pytest.mark.linux_only
def test_a_job_runs_the_author_loop_through_the_method_and_grades_it(prepared, monkeypatch):
    config, tmp = prepared
    monkeypatch.setenv("CTXPRESS_AGENT_API_KEY", "secret-agent")
    monkeypatch.setenv("CTXPRESS_SUMMARIZER_API_KEY", "secret-summary")
    monkeypatch.setenv("CTXPRESS_GRADER_API_KEY", "secret-judge")
    adapter = benchmarks.get("browsecomp-plus")
    plan = adapter.compile_plan(config, tmp)
    job = plan["jobs"][1]                                                     # ComplexityTrap(n=1)
    result = adapter.execute(job["task"], job["method"], plan["config"], job, paths=None, folder=tmp / "job", label="t")
    from ctxpress.harness.jobs.queue import validate_execution
    from ctxpress.harness.results.outcomes import observe
    validate_execution(result)
    assert result["grade"]["resolved"] is True and observe(result["grade"], result)["boolean_valid"]
    assert result["author_status"] == "exited" and result["calls"] == 4 and result["requests"] == 4
    agent = [row for row in result["rewrites"] if "request" in row]
    assert all(row["dialect"] == "chat" and row["status"] == 200 for row in agent)
    assert agent[-1]["usage"]["cached_tokens"] == 80 and agent[-1]["operations"]
    assert {row["session"] for row in agent} == {"browsecomp:" + job["id"]}       # one conversation per job
    assert not any(row["history_rebased"] for row in agent)                         # the moving marker is not a revision
    sent = [body for path, _, body in Upstream.seen if body.get("tools")]
    last = [m["content"] for m in sent[-1]["messages"] if m["role"] == "tool"]
    assert [text.startswith("Old environment output") for text in last] == [True, True, False]     # the method acted
    assert last[-1].endswith("[CURRENT CONTEXT TOKEN: 4000]")
    keys = {(path, auth) for path, auth, body in Upstream.seen}                    # exact upstream paths
    assert ("/v1/chat/completions", "Bearer secret-agent") in keys and ("/v1/chat/completions", "Bearer secret-summary") in keys
    assert ("/v1/responses", "Bearer secret-judge") in keys
    author = json.loads(next((tmp / "job" / "author-results").rglob("run_101.json")).read_text())
    assert author["key_seen"] == "ctxpress-proxy-holds-the-key"                # the author process never held a key
    summarizer = [row for row in result["rewrites"] if row.get("host_role") == "summarizer"]
    assert summarizer and summarizer[0]["type"] == "passthrough" and summarizer[0]["model"] == "gpt-mini"
    grader = [json.loads(line) for line in (tmp / "job" / "grader-requests.jsonl").read_text().splitlines()]
    assert grader[0]["type"] == "passthrough" and grader[0]["usage"]["input_tokens"] == 30
    for name in ("ctxpress-requests.jsonl", "summarizer-requests.jsonl", "grader-requests.jsonl", "author-command.json"):
        assert "secret" not in (tmp / "job" / name).read_text()


def test_missing_or_unparsed_verdicts_are_grading_errors(tmp_path):
    adapter = benchmarks.get("browsecomp-plus")
    task = adapter.task(dict(id="7", question="q", answer="a"), [])
    from ctxpress.harness.results.outcomes import observe
    missing = adapter.read_grade(task, tmp_path)
    assert missing["resolved"] is None and observe(missing)["grading_error"]
    (tmp_path / "run_7_eval.json").write_text(json.dumps({"judge_result": {"parse_error": True}}))
    assert observe(adapter.read_grade(task, tmp_path))["grading_error"]
    (tmp_path / "run_7_eval.json").write_text(json.dumps({"judge_result": {"correct": False, "parse_error": False}}))
    wrong = adapter.read_grade(task, tmp_path)
    assert wrong["resolved"] is False and observe(wrong)["boolean_valid"]
