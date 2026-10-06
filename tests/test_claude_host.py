"""`ctxpress claude`: launcher command and MCP wiring; ordinary interactive sessions when real CLIs are given."""
import json, os, shutil

import pytest
from ctxpress.hosts.claude import launch
from ctxpress.methods import build


def test_dry_run_command_gives_mcp_and_preallows_the_methods_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("CTXPRESS_HOME", str(tmp_path))
    cmd, result = launch.run({"class": "CWL", "args": {"budget": 80000}}, ["--model", "m"], claude_bin="claude", dry_run=True)
    assert result is None and cmd[0] == "claude" and cmd[-2:] == ["--model", "m"]
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert allowed == ["mcp__ctxpress__ctxpress_retrieve", "mcp__ctxpress__ctxpress_status", "mcp__ctxpress__delimiter"]
    assert "--mcp-config" in cmd


def test_no_tools_leaves_claude_arguments_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("CTXPRESS_HOME", str(tmp_path))
    cmd, _ = launch.run({"class": "NoCompaction"}, ["--continue"], claude_bin="claude", dry_run=True, tools=False)
    assert cmd == ["claude", "--continue"]


def test_mcp_server_imports_this_ctxpress():
    config = launch.mcp_config({"CTXPRESS_METHOD_CONFIG": json.dumps({"class": "DTOC"})})["mcpServers"]["ctxpress"]
    assert config["args"] == ["-m", "ctxpress", "mcp"] and launch.package_root() in config["env"]["PYTHONPATH"].split(os.pathsep)
    assert config["env"]["CTXPRESS_METHOD_CONFIG"] == json.dumps({"class": "DTOC"})


@pytest.mark.skipif(not shutil.which("tmux"), reason="interactive check drives the TUI through tmux")
@pytest.mark.parametrize("host,variable", [("claude", "CTXPRESS_CLAUDE_BIN"), ("codex", "CTXPRESS_CODEX_BIN")])
def test_ordinary_interactive_session(host, variable, tmp_path):
    binary = os.environ.get(variable)
    if not binary:
        pytest.skip(f"set {variable} to a real CLI to run the interactive session check")
    from ctxpress.harness.checks import interactive as interactive_check
    result = interactive_check.check(host, binary, str(tmp_path))
    assert result["passed"], json.dumps({k: result[k] for k in ("checks", "problems", "requests")}, indent=1)
