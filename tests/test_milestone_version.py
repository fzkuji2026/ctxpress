"""Authentic local Git objects through a frozen version gate; no remote IO."""
import base64, os, shutil, subprocess
import pytest
from ctxpress.benchmarks.milestone import version


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.STDOUT).decode().strip()


def checkout(tmp_path, annotated=True):
    root = tmp_path / 'original'; root.mkdir(); git(root, 'init', '-q')
    workspace = root / 'repo'; workspace.mkdir(); (workspace / 'metadata.json').write_text('{}', encoding='utf-8')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'synthetic fixture')
    args = ['-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'tag']
    if annotated: args += ['-am', 'release fixture']
    git(root, *args, 'v1.0.2')
    task = dict(benchmark='swe-milestone', initial_state={'workspace': str(workspace)})
    return root, task


@pytest.mark.linux_only
@pytest.mark.parametrize('annotated', [False, True])
def test_native_git_queries_work_after_original_source_is_deleted(tmp_path, annotated):
    root, task = checkout(tmp_path, annotated)
    record = version.capture([task], 'v1.0.2')
    stage = tmp_path / 'staged'; stage.mkdir(); (stage / 'repo').mkdir()
    version.materialize(record, stage)
    shutil.rmtree(root)
    assert git(stage / 'repo', 'rev-parse', 'HEAD^{commit}') == record['head']
    assert git(stage / 'repo', 'rev-parse', 'refs/tags/v1.0.2^{commit}') == record['head']
    assert not (stage / '.git/hooks').exists() and not (stage / '.git/config').read_text(encoding='utf-8').count('remote')
    assert len(record['objects']) == (2 if annotated else 1)
    with pytest.raises(ValueError, match='fresh'): version.materialize(record, stage)


@pytest.mark.linux_only
def test_dirty_files_are_recorded_separately_from_git_identity(tmp_path):
    root, task = checkout(tmp_path)
    (root / 'repo/metadata.json').write_text('dirty fixture data', encoding='utf-8')
    record = version.capture([task], 'v1.0.2')
    assert record['source_dirty'] and len(record['source_status_sha256']) == 64


def test_mismatched_release_is_rejected_before_staging(tmp_path):
    root, task = checkout(tmp_path)
    (root / 'repo/metadata.json').write_text('new data', encoding='utf-8'); git(root, 'add', '.')
    git(root, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'moved HEAD')
    with pytest.raises(ValueError, match='differs'): version.capture([task], 'v1.0.2')


@pytest.mark.linux_only
@pytest.mark.parametrize('change', ['object-bytes', 'oid', 'wrong-head', 'release', 'extra-object', 'encoding'])
def test_changed_or_unrelated_version_evidence_is_rejected(tmp_path, change):
    root, task = checkout(tmp_path); record = version.capture([task], 'v1.0.2')
    if change == 'object-bytes': record['objects'][record['head']]['data'] = base64.b64encode(b'changed').decode()
    if change == 'oid': record['objects']['0'*40] = record['objects'].pop(record['head'])
    if change == 'wrong-head': record['head'] = record['tag']
    if change == 'release': record['release'] = '../escape'
    if change == 'extra-object': record['objects']['1'*40] = {'type': 'commit', 'data': ''}
    if change == 'encoding': record['objects'][record['head']]['data'] = '!!'
    with pytest.raises(ValueError): version.validate(record)


def test_pin_cannot_inherit_a_disabled_author_gate_and_restores_environment(monkeypatch):
    monkeypatch.setenv('SWE_MILESTONE_DATA_VERSION_CHECK', 'off')
    monkeypatch.setenv('SWE_MILESTONE_IMAGE_TAG', 'unrelated')
    with version.pinned_environment('v1.0.2'):
        assert os.environ['SWE_MILESTONE_IMAGE_TAG'] == 'v1.0.2'
        assert 'SWE_MILESTONE_DATA_VERSION_CHECK' not in os.environ
    assert os.environ['SWE_MILESTONE_DATA_VERSION_CHECK'] == 'off'
    assert os.environ['SWE_MILESTONE_IMAGE_TAG'] == 'unrelated'
