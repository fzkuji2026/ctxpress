"""BigCodeBench report ownership stays narrow; global/private artifacts stay private."""
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from ctxpress.benchmarks.bigcode import grading as code_grading
from ctxpress.harness.jobs import plan as eval_plan


pytestmark = pytest.mark.skipif(
    os.name != 'posix' or not hasattr(os, 'fchown') or not hasattr(os, 'O_NOFOLLOW'),
    reason='Linux-only grading requires POSIX file ownership and no-follow APIs',
)


def test_official_report_keeps_bytes_hash_and_private_mode(tmp_path):
    report = tmp_path / 'sample-result.json'
    private = tmp_path / 'private.json'
    result = {'status': 'pass', 'details': {}, 'task_id': 'BigCodeBench/294'}
    eval_plan.atomic_json(private, result)
    before = private.stat()
    expected = hashlib.sha256(private.read_bytes()).hexdigest()

    code_grading.write_report(report, result)

    assert json.loads(report.read_text(encoding='utf-8')) == result
    assert eval_plan.file_sha256(report) == expected
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    assert (report.stat().st_uid, report.stat().st_gid) == (tmp_path.stat().st_uid, tmp_path.stat().st_gid)
    assert private.stat() == before
    assert hashlib.sha256(private.read_bytes()).hexdigest() == expected


def test_report_ownership_uses_bound_output_directory_and_exact_report_fd(tmp_path, monkeypatch):
    report = tmp_path / 'sample-result.json'
    actual_stat = Path.stat
    directory_owner = type('Owner', (), {'st_uid': 1234, 'st_gid': 2345, 'st_mode': tmp_path.stat().st_mode})()
    monkeypatch.setattr(Path, 'stat', lambda path, **kwargs: directory_owner if path == tmp_path else actual_stat(path, **kwargs))
    calls = []

    def transfer(descriptor, uid, gid):
        inode = os.fstat(descriptor)
        assert stat.S_ISREG(inode.st_mode)
        assert inode.st_ino == report.stat().st_ino
        assert stat.S_IMODE(inode.st_mode) == 0o600
        calls.append((uid, gid))

    monkeypatch.setattr(code_grading.os, 'fchown', transfer)
    code_grading.write_report(report, {'status': 'fail'})
    assert calls == [(1234, 2345)]


@pytest.mark.parametrize('name', ['request.json', 'solution.py', 'private.json'])
def test_publisher_rejects_other_output_files_without_changing_them(tmp_path, name):
    other = tmp_path / name
    eval_plan.atomic_json(other, {'untouched': True})
    before = other.stat()
    with pytest.raises(ValueError, match='official sample report path'):
        code_grading.write_report(other, {'status': 'pass'})
    assert other.stat() == before
    assert json.loads(other.read_text(encoding='utf-8')) == {'untouched': True}


def test_late_report_symlink_cannot_transfer_private_file_ownership(tmp_path, monkeypatch):
    report = tmp_path / 'sample-result.json'
    private = tmp_path / 'private.json'
    eval_plan.atomic_json(private, {'untouched': True})
    before = private.stat()
    atomic_json = eval_plan.atomic_json

    def replace_with_symlink(path, result):
        atomic_json(path, result)
        path.unlink()
        path.symlink_to(private)

    monkeypatch.setattr(eval_plan, 'atomic_json', replace_with_symlink)
    with pytest.raises(OSError):
        code_grading.write_report(report, {'status': 'pass'})
    assert private.stat() == before
    assert json.loads(private.read_text(encoding='utf-8')) == {'untouched': True}


def test_ownership_failure_propagates_and_closes_report_descriptor(tmp_path, monkeypatch):
    descriptors = []

    def fail(descriptor, uid, gid):
        descriptors.append(descriptor)
        raise PermissionError('ownership transfer denied')

    monkeypatch.setattr(code_grading.os, 'fchown', fail)
    with pytest.raises(PermissionError, match='ownership transfer denied'):
        code_grading.write_report(tmp_path / 'sample-result.json', {'status': 'pass'})
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
