"""Every method runs through the real proxy against the fake model and yields the unified statistics."""
import json

from ctxpress.harness.checks.smoke import smoke
from ctxpress.methods import REGISTRY


def test_every_method_runs_and_reports_statistics(tmp_path):
    report = smoke(tmp_path / "smoke", turns=12, chars=3000)
    assert report["passed"] and report["synthetic"]
    rows = {row["method"]: row for row in report["methods"]}
    assert set(rows) == set(REGISTRY)
    for name, row in rows.items():
        assert row["live"] and row["ok"], (name, row.get("error"))
        assert row["requests"] == 12 and row["self_checks"]["passed"], (name, row["self_checks"])
        stats = row["stats"]
        assert stats["main_input_tokens"] > 0 and stats["cost_usd"] > 0 and stats["max_api_input"] > 0, name
        assert (tmp_path / "smoke" / name / "analysis.json").is_file()
    # methods that act within a dozen requests did act
    for name in ("ComplexityTrap", "ClawVM", "ARC", "TokenPilot", "EntryTruncation"):
        assert rows[name]["rewritten"] > 0 and rows[name]["operations"], name
    assert rows["NoCompaction"]["rewritten"] == 0
    assert rows["SWEPruner"]["pruner_calls"] > 0
    assert rows["AgentDiet"]["model_side_calls"] > 0                         # its reflection model was called
    saved = json.loads((tmp_path / "smoke" / "smoke.json").read_text(encoding="utf-8"))
    assert saved["methods"] == json.loads(json.dumps(report["methods"], default=str))
    assert "| ComplexityTrap | yes |" in (tmp_path / "smoke" / "smoke.md").read_text(encoding="utf-8")


def test_method_tools_run_in_the_real_tool_server(tmp_path):
    report = smoke(tmp_path / "tools", methods=["CWL", "DTOC", "ACM"], turns=30, chars=6000)
    rows = {row["method"]: row for row in report["methods"]}
    assert all(row["ok"] for row in rows.values()), rows
    assert rows["CWL"]["operations"].get("delete")                           # completed chunks evicted
    assert rows["DTOC"]["operations"].get("placeholder")                     # outputs hidden by tool key
    assert rows["ACM"]["operations"].get("segment_summary")                  # history folded into memory
