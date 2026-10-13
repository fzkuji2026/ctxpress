"""residue_prune_scope=reference-added against the author's own evaluator prune pass (skipped without the source)."""
import io
import os
import sys
import tarfile
from pathlib import Path
import pytest

from ctxpress.benchmarks.milestone.source_policy import lifted

SOURCE = Path(os.environ.get("SWE_MILESTONE_SOURCE", Path.home() / "swe/SWE-Milestone"))
pytestmark = pytest.mark.skipif(not (SOURCE / "harness/e2e/evaluator.py").is_file(), reason="needs SWE-Milestone source")

START = {"db/migrations/001_init.go", "db/old.go", "cmd/main.go", "tests/a_test.go"}
END = START | {"db/migrations/008_reference.go", "server/reference_handler.go", "db/migrations/009_reference.sql"}
SNAPSHOT = {"db/migrations/001_init.go", "db/migrations/008_agent.go", "cmd/main.go"}  # agent deleted db/old.go


def evaluator(tmp_path, monkeypatch):
    sys.path.insert(0, str(SOURCE))
    from harness.e2e import evaluator as ev
    from harness.utils.src_filter import SrcFileFilter
    tar = tmp_path / "snapshot.tar"
    with tarfile.open(tar, "w") as t:
        for name in SNAPSHOT:
            info = tarfile.TarInfo(name); info.size = 1
            t.addfile(info, io.BytesIO(b"x"))
    removed = []
    monkeypatch.setattr(ev.subprocess, "run", lambda cmd, input=None, **k: removed.extend(
        p.removeprefix("/testbed/") for p in (input or "").split("\0") if p) or type("R", (), {"returncode": 0})())
    e = ev.PatchEvaluator.__new__(ev.PatchEvaluator)
    e.__dict__.update(residue_prune_enabled=True, _prune_config_invalid=False, milestone_id="m", patch_file=tar,
                      residue_prune_policy_source="metadata-legacy", snapshot_legacy_unverified=False,
                      _prune_filter=SrcFileFilter(["db", "cmd", "server"], ["tests/**"]), container_name="c",
                      prune_keep_list=frozenset(), prune_extensions=frozenset({".go"}), _eval_meta={})
    e._git_ls_tree = lambda tag: END if tag.endswith("-end") else START
    e._load_capture_src_filter = lambda: None
    e._load_capture_excluded = lambda start: None
    e._load_capture_build_manifests = lambda: set()
    return ev, e, removed


def test_author_default_keeps_reference_added_sources(tmp_path, monkeypatch):
    ev, e, removed = evaluator(tmp_path, monkeypatch)
    assert ev.PatchEvaluator._maybe_prune_residue(e, "end") == (True, "")
    assert removed == ["db/old.go"]


def test_reference_added_scope_also_prunes_reference_only_sources(tmp_path, monkeypatch):
    ev, e, removed = evaluator(tmp_path, monkeypatch)
    assert lifted(ev.PatchEvaluator._maybe_prune_residue)(e, "end") == (True, "")
    # tests and non-.go assets (the reference .sql) stay; the reference .go migration and handler go
    assert sorted(removed) == ["db/migrations/008_reference.go", "db/old.go", "server/reference_handler.go"]
    assert ev.compute_prune_set.__name__ == "compute_prune_set"  # the module itself is not patched
