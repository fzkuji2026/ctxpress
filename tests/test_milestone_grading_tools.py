"""Parser binaries must come from the frozen dependency tree, never host PATH."""
import sys
from pathlib import Path
from types import ModuleType
import pytest
from ctxpress.benchmarks.milestone.grading_tools import bind_ast_grep


def detector(tmp_path, monkeypatch):
    source = tmp_path/'source'
    for name in ('harness', 'harness.prepare_repo', 'harness.prepare_repo.split_test_patches'):
        module = ModuleType(name); module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    module = ModuleType('harness.prepare_repo.split_test_patches.test_detector')
    module.__file__ = str(source/'harness/prepare_repo/split_test_patches/test_detector.py')
    module.resolve_ast_grep = lambda: '/mutable/host/ast-grep'
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return source, module


def test_missing_frozen_parser_fails_even_if_host_binary_exists(tmp_path, monkeypatch):
    source, module = detector(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='frozen dependencies'):
        bind_ast_grep(source, tmp_path/'dependencies')
    assert module.resolve_ast_grep() == '/mutable/host/ast-grep'


def test_frozen_parser_overrides_host_resolution(tmp_path, monkeypatch):
    source, module = detector(tmp_path, monkeypatch)
    binary = tmp_path/'dependencies/bin/ast-grep'
    binary.parent.mkdir(parents=True); binary.write_text('fixture'); binary.chmod(0o755)
    assert bind_ast_grep(source, tmp_path/'dependencies') == binary
    assert module.resolve_ast_grep() == str(binary)


def test_other_author_source_rejected(tmp_path, monkeypatch):
    source, module = detector(tmp_path, monkeypatch)
    module.__file__ = str(tmp_path/'other/test_detector.py')
    with pytest.raises(ValueError, match='undeclared source'):
        bind_ast_grep(source, tmp_path/'dependencies')


def test_parser_failure_keeps_exact_temporary_bytes_and_exception(tmp_path):
    import hashlib, json
    from types import SimpleNamespace
    from ctxpress.benchmarks.milestone.grading_tools import preserve_parser_failures
    class Failure(RuntimeError): pass
    error = Failure('original timeout')
    calls = []
    def parser(command, **kwargs):
        calls.append((command, kwargs))
        raise error
    detector = SimpleNamespace(_run_ast_grep_json=parser, RustTestDetectionError=Failure)
    output = tmp_path/'evidence'
    preserve_parser_failures(detector, output)
    source = tmp_path/'temporary.rs';content = b'#[test]\r\nfn x() {}\n'
    source.write_bytes(content)
    command = ['ast-grep', 'run', '--pattern', '#[test]', str(source)]
    with pytest.raises(Failure) as caught:
        try:
            detector._run_ast_grep_json(command, purpose='detect tests', strict=True)
        finally:
            source.unlink()
    assert caught.value is error
    record = next(output.glob('*/failure.json'));row = json.loads(record.read_text())
    assert record.with_name('input.rs').read_bytes() == content
    assert row['input_sha256'] == hashlib.sha256(content).hexdigest()
    assert row['arguments'] == command[1:-1]
    assert calls == [(command, dict(purpose='detect tests', strict=True))]


def test_parser_success_creates_no_snapshot(tmp_path):
    from types import SimpleNamespace
    from ctxpress.benchmarks.milestone.grading_tools import preserve_parser_failures
    result = []
    detector = SimpleNamespace(_run_ast_grep_json=lambda *a, **k: result, RustTestDetectionError=RuntimeError)
    preserve_parser_failures(detector, tmp_path/'evidence')
    assert detector._run_ast_grep_json(['ast-grep','missing.rs'], purpose='test') is result
    assert not (tmp_path/'evidence').exists()


def test_parser_evidence_write_failure_preserves_original_error(tmp_path):
    from types import SimpleNamespace
    from ctxpress.benchmarks.milestone.grading_tools import preserve_parser_failures
    error = RuntimeError('parser failed')
    def parser(*a, **k): raise error
    detector = SimpleNamespace(_run_ast_grep_json=parser, RustTestDetectionError=RuntimeError)
    preserve_parser_failures(detector, tmp_path/'evidence')
    with pytest.raises(RuntimeError) as caught:
        detector._run_ast_grep_json(['ast-grep','missing.rs'], purpose='test', strict=True)
    assert caught.value is error


def test_plan_requirements_reject_stale_dependency_manifest():
    from ctxpress.benchmarks.milestone.protocol import requirements
    lock = {'runtime': {'platform': 'linux', 'version': '3.13.5'},
            'trees': {'code': {'files': {}}, 'dependencies': {'files': {}}}, 'tasks': {}}
    assert any('bin/ast-grep' in item for item in requirements({}, [], lock))
    lock['trees']['dependencies']['files']['bin/ast-grep'] = 'a' * 64
    assert not any('bin/ast-grep' in item for item in requirements({}, [], lock))
