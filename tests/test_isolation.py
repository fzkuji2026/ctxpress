"""Concurrent runs must not share a profile or overwrite another conversation's archives."""
import json
import threading
from pathlib import Path
import tomllib
from concurrent.futures import ThreadPoolExecutor

import pytest
from ctxpress.hosts.codex import launch
from ctxpress.live import mcp
from ctxpress.live.rewrite import Rewriter
from ctxpress.methods import WithMemory, ComplexityTrap


def test_concurrent_launch_profiles_are_independent_and_cleaned(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CTXPRESS_HOME", str(tmp_path / "ctxpress"))
    barrier = threading.Barrier(2)
    profiles = []

    def fake_process(cmd, env):
        profile = cmd[cmd.index("--profile") + 1]
        path = tmp_path / "codex" / (profile + ".config.toml")
        barrier.wait(timeout=10)  # Both launchers have written before either reads.
        data = tomllib.loads(path.read_text(encoding='utf-8'))
        profiles.append((profile, data["openai_base_url"], env["CTXPRESS_STORE_DIR"]))
        assert data["mcp_servers"]["ctxpress"]["env"]["CTXPRESS_STORE_DIR"] == env["CTXPRESS_STORE_DIR"]
        return 0

    monkeypatch.setattr(launch.subprocess, "call", fake_process)
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = [pool.submit(launch.run, {"class": "NoCompaction"}, codex_bin="unused") for _ in range(2)]
        assert all(run.result(timeout=15)[1][0] == 0 for run in runs)
    assert len({p[0] for p in profiles}) == len({p[1] for p in profiles}) == len({p[2] for p in profiles}) == 2
    assert not list((tmp_path / "codex").glob("ctxpress-*.config.toml"))


def test_archive_ids_are_isolated_between_sessions(tmp_path, monkeypatch):
    rw = Rewriter(lambda: WithMemory(ComplexityTrap(1)), store_dir=str(tmp_path), retrieve_tool=True)
    placeholders = []
    for session, text in (("one", "first session"), ("two", "second session")):
        items = [{"type": "message", "role": "user", "content": "task"}]
        for i in range(2):
            items += [{"type": "function_call", "call_id": str(i), "name": "exec_command", "arguments": "cat a.py"},
                      {"type": "function_call_output", "call_id": str(i), "output": text + str(i)}]
        body, _ = rw.rewrite_body({"input": items}, session)
        context = rw.sessions[session].ctx
        item = context.outputs()[0]
        placeholders.append(next(x["output"] for x in body["input"] if x.get("type") == "function_call_output"))
        monkeypatch.setenv("CTXPRESS_STORE_DIR", str(tmp_path))
        assert mcp.retrieve(item["id"], context.store_namespace) == text + "0"
        assert "session " + context.store_namespace in placeholders[-1]
    assert placeholders[0] != placeholders[1]
    with pytest.raises(ValueError):
        mcp.retrieve(1, "../other")


def test_mcp_status_uses_running_method_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("CTXPRESS_HOME", str(tmp_path))
    monkeypatch.setenv("CTXPRESS_METHOD_CONFIG", json.dumps({"class": "Pichay", "args": {"age": 4}}))
    assert "method: Pichay" in mcp.status()
