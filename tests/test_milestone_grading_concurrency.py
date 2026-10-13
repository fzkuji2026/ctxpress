"""Local thread execution tests; no model calls or benchmark grades."""
import concurrent.futures
import threading
from types import SimpleNamespace

import pytest

from ctxpress.benchmarks.milestone.grading_concurrency import installed, validate_workers


class Native:
    def _run_watcher_loop(self):
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self.grade, range(3)))


def test_serial_grading_does_not_change_other_pools_and_restores_on_error(tmp_path):
    author = SimpleNamespace(__file__=str(tmp_path / 'harness/e2e/run_e2e.py'), E2ETrialRunner=Native)
    entered, release, second = threading.Event(), threading.Event(), threading.Event()
    original_executor = concurrent.futures.ThreadPoolExecutor
    with pytest.raises(RuntimeError, match='after drain'):
        with installed(author, tmp_path, 1):
            trial = author.E2ETrialRunner()
            def grade(index):
                if index == 0:
                    entered.set()
                    assert release.wait(3)
                else:
                    second.set()
            trial.grade = grade
            watcher = threading.Thread(target=trial._run_watcher_loop)
            watcher.start()
            try:
                assert entered.wait(2)
                assert not second.wait(0.1)
                assert concurrent.futures.ThreadPoolExecutor is original_executor
            finally:
                release.set()
                watcher.join(3)
            assert not watcher.is_alive()
            assert second.is_set()
            raise RuntimeError('after drain')
    assert author.E2ETrialRunner is Native


@pytest.mark.parametrize('value', [True, False, 0, 5, 1.0, '1', None])
def test_invalid_workers_rejected(value):
    with pytest.raises(ValueError, match='grading_workers'):
        validate_workers(value)


def test_no_override_leaves_author_class_unchanged(tmp_path):
    author = SimpleNamespace(E2ETrialRunner=Native)
    with installed(author, tmp_path):
        assert author.E2ETrialRunner is Native


def test_undeclared_source_rejected(tmp_path):
    author = SimpleNamespace(__file__='/other/run_e2e.py', E2ETrialRunner=Native)
    with pytest.raises(ValueError, match='undeclared'):
        with installed(author, tmp_path, 1):
            pass


def test_adapter_exposes_and_validates_frozen_run_option():
    from ctxpress.benchmarks.milestone.adapter import SWEMilestone
    adapter = SWEMilestone()
    assert 'grading_workers' in adapter.run_fields()
    assert adapter.normalize_run({'grading_workers': 1}, 1)['grading_workers'] == 1
    with pytest.raises(ValueError, match='grading_workers'):
        adapter.normalize_run({'grading_workers': True}, 1)
    with pytest.raises(ValueError, match='task_start'):
        adapter.compile_plan({'start_mode': 'checkpoint', 'run': {'grading_workers': 1}})
