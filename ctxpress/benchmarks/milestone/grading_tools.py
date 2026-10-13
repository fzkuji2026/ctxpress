"""Bind the author's parser CLI to the already verified dependency snapshot."""
from __future__ import annotations
import os
from pathlib import Path


def bind_ast_grep(source, dependencies, diagnostics=None):
    from harness.prepare_repo.split_test_patches import test_detector
    expected = Path(source).resolve() / 'harness/prepare_repo/split_test_patches/test_detector.py'
    if Path(test_detector.__file__).resolve() != expected:
        raise ValueError('native Rust parser imported an undeclared source')
    binary = Path(dependencies).resolve() / 'bin/ast-grep'
    if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError('native grading requires executable bin/ast-grep in the frozen dependencies; '
                         'prepare and capture it before launching model jobs')
    # Keep author parsing and CLI arguments unchanged; bind only the executable.
    # Do not fall back to mutable host PATH or a different interpreter-side copy.
    test_detector.resolve_ast_grep = lambda: str(binary)
    if diagnostics is not None:
        preserve_parser_failures(test_detector, diagnostics)
    return binary


def preserve_parser_failures(detector, directory):
    """Keep the temporary Rust bytes before the author's caller deletes them."""
    import functools
    import hashlib
    import logging
    import time
    import uuid
    from ctxpress.core.artifacts import atomic_json

    original = detector._run_ast_grep_json
    error_type = detector.RustTestDetectionError

    @functools.wraps(original)
    def observed(command, *, purpose, strict=False):
        started = time.monotonic()
        try:
            return original(command, purpose=purpose, strict=strict)
        except error_type:
            try:
                source = Path(command[-1])
                if source.is_symlink() or not source.is_file():
                    raise ValueError('parser diagnostic requires a regular input file')
                content = source.read_bytes()
                folder = Path(directory) / uuid.uuid4().hex
                folder.mkdir(parents=True, exist_ok=False)
                (folder / 'input.rs').write_bytes(content)
                atomic_json(folder / 'failure.json', dict(
                    schema='ctxpress.eval.milestone_parser_failure', version=1,
                    input_file='input.rs', input_sha256=hashlib.sha256(content).hexdigest(),
                    input_bytes=len(content), arguments=list(command[1:-1]),
                    purpose=purpose, strict=strict, elapsed_seconds=time.monotonic()-started))
            except (OSError, ValueError):
                logging.getLogger(__name__).warning('Could not preserve parser failure input', exc_info=True)
            raise
    detector._run_ast_grep_json = observed
