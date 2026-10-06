"""`ctxpress install codex` / use / uninstall on temporary shell start-up files, and the MCP tools."""
import json, os, subprocess, sys, tempfile
from ctxpress.hosts.codex import install
from ctxpress import settings


def test_install_use_uninstall(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("CTXPRESS_HOME", os.path.join(d, "ctx"))
        bashrc, ps1 = os.path.join(d, ".bashrc"), os.path.join(d, "ps", "profile.ps1")
        open(bashrc, "w", encoding='utf-8').write("export FOO=1\nalias ll='ls -l'\n")
        files = [(bashrc, install.POSIX), (ps1, install.POWERSHELL)]
        install.install("ComplexityTrap", {"n": 10}, files=files)
        b = open(bashrc, encoding='utf-8').read()
        assert b.startswith("export FOO=1\nalias ll='ls -l'\n") and install.POSIX in b
        assert install.POWERSHELL in open(ps1, encoding='utf-8').read()
        install.install("ComplexityTrap", {"n": 10}, files=files)                 # idempotent: one block
        assert open(bashrc, encoding='utf-8').read().count(install.BEGIN) == 1
        assert settings.load()["method"] == "ComplexityTrap"
        install.use("Pichay", {"age": 4})
        assert settings.load()["method"] == "Pichay"
        install.uninstall(files=files)
        assert open(bashrc, encoding='utf-8').read() == "export FOO=1\nalias ll='ls -l'\n"          # back to the user's file
        assert install.BEGIN not in open(ps1, encoding='utf-8').read()


def test_mcp_server_tools():
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, CTXPRESS_HOME=d, PYTHONPATH=os.path.dirname(os.path.dirname(__file__)))
        os.makedirs(os.path.join(d, "store")); open(os.path.join(d, "store", "7.txt"), "w", encoding='utf-8').write("original seven")
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ctxpress_retrieve", "arguments": {"id": 7}}}]
        p = subprocess.Popen([sys.executable, "-m", "ctxpress", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, text=True)
        out, _ = p.communicate("".join(json.dumps(m) + "\n" for m in msgs), timeout=20)
        replies = [json.loads(l) for l in out.splitlines() if l.strip()]
        assert replies[0]["result"]["serverInfo"]["name"] == "ctxpress"
        assert {t["name"] for t in replies[1]["result"]["tools"]} == {"ctxpress_retrieve", "ctxpress_status"}
        assert replies[2]["result"]["content"][0]["text"] == "original seven"
