"""Explicit per-trial grading concurrency without changing global executors."""
from __future__ import annotations

import contextlib
import builtins
import concurrent.futures
import types
from pathlib import Path


def validate_workers(value):
    if type(value) is not int or not 1 <= value <= 4:
        raise ValueError('grading_workers must be an integer between 1 and 4')
    return value


@contextlib.contextmanager
def installed(author, source, workers=None):
    if workers is None:
        yield
        return
    validate_workers(workers)
    if Path(author.__file__).resolve() != Path(source).resolve() / 'harness/e2e/run_e2e.py':
        raise ValueError('grading concurrency imported undeclared author code')
    original = author.E2ETrialRunner
    loop = original._run_watcher_loop
    if not isinstance(loop, types.FunctionType) or 'ThreadPoolExecutor' not in loop.__code__.co_names:
        raise ValueError('unsupported author grading executor contract')
    executor = concurrent.futures.ThreadPoolExecutor

    def limited_executor(max_workers=None, *args, **kwargs):
        if max_workers != 4:
            raise ValueError('author grading executor default changed')
        return executor(workers, *args, **kwargs)

    # Clone only the watcher function's globals. Never patch the shared stdlib:
    # unrelated HTTP, model, or cleanup pools must keep their own concurrency.
    futures = types.SimpleNamespace(**vars(concurrent.futures))
    futures.ThreadPoolExecutor = limited_executor
    namespace = dict(loop.__globals__)
    proxy = types.SimpleNamespace(**{**vars(concurrent), 'futures': futures})
    namespace['concurrent'] = proxy
    original_builtins = loop.__globals__.get('__builtins__', builtins)
    scoped_builtins = dict(original_builtins if isinstance(original_builtins, dict) else vars(original_builtins))
    original_import = scoped_builtins['__import__']

    def scoped_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'concurrent.futures' and level == 0:
            return futures if fromlist else proxy
        return original_import(name, globals, locals, fromlist, level)

    # The author imports concurrent.futures inside the watcher. Scope that
    # import to this cloned function as well; builtins/stdlib stay untouched.
    scoped_builtins['__import__'] = scoped_import
    namespace['__builtins__'] = scoped_builtins
    bounded_loop = types.FunctionType(loop.__code__, namespace, loop.__name__, loop.__defaults__, loop.__closure__)
    bounded_loop.__kwdefaults__ = loop.__kwdefaults__

    class Trial(original):
        _run_watcher_loop = bounded_loop

    author.E2ETrialRunner = Trial
    try:
        yield
    finally:
        author.E2ETrialRunner = original
