"""HostDefault: the pinned host exactly as shipped. No rewrite and no native-compaction override anywhere."""
import asyncio, copy, json, shlex

import pytest

from ctxpress import benchmarks
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import build, METHODS
from test_eval_families import NAMES, fixture_data
from test_model_accounting import rates


def history(n):
    items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task"}]}]
    for i in range(n):
        items.append({"type": "function_call", "call_id": f"c{i}", "name": "shell", "arguments": "{}"})
        items.append({"type": "function_call_output", "call_id": f"c{i}", "output": "x" * 5000})
    return {"model": "m", "input": items}


def test_host_default_never_rewrites_and_sets_no_host_config():
    method = build({"class": "HostDefault"})
    assert method.codex_config == {} and method.allow_native_compaction and not method.instructions
    assert METHODS["HostDefault"][2] is True                                  # runs live through the proxy
    rw = Rewriter(lambda: build({"class": "HostDefault"}))
    body = history(30)
    out, info = rw.rewrite_body(copy.deepcopy(body), "s")
    assert out == body and info["changed"] == 0 and info["dropped"] == 0


@pytest.mark.parametrize("name", NAMES)
def test_plans_carry_no_compaction_limit_for_host_default(tmp_path, name):
    root = fixture_data(tmp_path, name)
    binary = tmp_path / "bin"; binary.mkdir(); (binary / "codex").write_bytes(b"synthetic fixture")
    methods = [{"class": "HostDefault", "args": {}}, {"class": "CodexAutoCompact", "args": {"t": 200000}}]
    cfg = dict(schema="ctxpress.eval", version=1, benchmark=name, start_mode="task_start", scope="benchmark",
               model="main", tasks=[benchmarks.get(name).task_instances(root)[0]["id"]], backend="codex_docker",
               methods=methods, repeats=1, workers=1, prices={"models": {"main": rates()}},
               environment={"data": str(root), "bindir": str(binary)})
    plan = eval_plan.compile_plan(cfg)
    assert [job["compact_limit"] for job in plan["jobs"]] == [None, 200000]


def test_milestone_hook_passes_no_override_for_host_default():
    from test_milestone_codex import OfficialFixture, settings
    from ctxpress.benchmarks.milestone.codex_hook import framework
    cfg = dict(settings(), method={"class": "HostDefault", "args": {}}, compact_limit=None)
    command = framework(OfficialFixture, cfg)().build_run_command("fixture", None, "/tmp/prompt")
    assert "model_auto_compact_token_limit" not in command
    assert shlex.split(command)[shlex.split(command).index("--method") + 1] == "HostDefault"
    explicit = framework(OfficialFixture, settings())().build_run_command("fixture", None, "/tmp/prompt")
    assert "model_auto_compact_token_limit=230000" in shlex.split(explicit)


def test_harbor_codex_writes_and_passes_no_override_for_host_default():
    from test_harbor_codex import EnvironmentFixture, agent, settings
    cfg = dict(settings(), method={"class": "HostDefault"}, compact_limit=None)
    instance = agent(cfg)
    environment = EnvironmentFixture()
    asyncio.run(instance.setup(environment))
    assert not any("model_auto_compact_token_limit" in command for command, _ in environment.commands)
    run = instance.create_run_agent_commands("task")[-1].command
    assert "model_auto_compact_token_limit" not in run
    with pytest.raises(ValueError):
        agent(dict(settings(), compact_limit=0))
