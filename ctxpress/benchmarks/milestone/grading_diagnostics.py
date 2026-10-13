"""Preserve runner exit evidence before the author reduces it to a fatal log."""
from __future__ import annotations

import functools
import logging
import re
import time
import types
import uuid
from pathlib import Path

from ctxpress.core.artifacts import atomic_json


def observed_tests(original):
    @functools.wraps(original)
    def invoke(runner, *args, **kwargs):
        output = Path(kwargs['output_dir'])

        class ObservedRunner:
            def __getattr__(self, name):
                return getattr(runner, name)

            def run(self, *run_args, **run_kwargs):
                started = time.monotonic()
                state_path = output / ('ctxpress-runner-state-'+uuid.uuid4().hex+'.json')
                state = dict(schema='ctxpress.eval.milestone_runner_state', version=1,
                             milestone_id=kwargs.get('milestone_id'), phase='running',
                             started_wall=time.time(), started_monotonic=started,
                             timeout_seconds=run_kwargs.get('timeout', run_args[1] if len(run_args)>1 else None))
                def persist():
                    try:
                        atomic_json(state_path, state)
                    except OSError:
                        logging.getLogger(__name__).warning('Could not persist runner state', exc_info=True)
                persist()
                try:
                    result = runner.run(*run_args, **run_kwargs)
                except BaseException as error:
                    state.update(phase='raised', error_type=type(error).__name__)
                    raise
                else:
                    state.update(phase='returned', returncode=result[0])
                finally:
                    state.update(elapsed_seconds=time.monotonic()-started, finished_wall=time.time())
                    persist()
                code, _, stderr = result
                # Record only diagnostic metadata, not commands, environment,
                # stdout or arbitrary stderr (which can contain private data).
                timeout = re.fullmatch(r'Command timed out after ([0-9.]+) seconds', stderr or '')
                if code != 0:
                    record = dict(schema='ctxpress.eval.milestone_runner_exit', version=1,
                                  milestone_id=kwargs.get('milestone_id'), returncode=code,
                                  elapsed_seconds=time.monotonic()-started,
                                  cause='timeout' if code == -1 and timeout else 'nonzero_exit',
                                  timeout_seconds=float(timeout.group(1)) if code == -1 and timeout else None)
                    try:
                        atomic_json(output / ('ctxpress-runner-exit-'+uuid.uuid4().hex+'.json'), record)
                    except OSError:
                        logging.getLogger(__name__).warning('Could not persist runner exit evidence', exc_info=True)
                return result

        return original(ObservedRunner(), *args, **kwargs)
    return invoke


def bind(function):
    """Clone only this evaluator method's globals; leave author modules untouched."""
    scope = dict(function.__globals__)
    original = scope.get('run_single_state_tests')
    if not callable(original):
        raise ValueError('native evaluator lacks run_single_state_tests diagnostic boundary')
    scope['run_single_state_tests'] = observed_tests(original)
    clone = types.FunctionType(function.__code__, scope, function.__name__,
                               function.__defaults__, function.__closure__)
    clone.__kwdefaults__ = function.__kwdefaults__
    return clone


def collect(trial):
    """Return supplemental evidence only; never decide official grade validity."""
    import hashlib
    import json
    import os

    root = Path(trial) / 'evaluation'
    records, errors = [], []
    if root.is_symlink():
        return dict(records=[], errors=[dict(path='evaluation', error='symlink')])
    def walk_error(error):
        errors.append(dict(error=type(error).__name__))
    for directory, children, files in os.walk(root, followlinks=False, onerror=walk_error):
        for child in list(children):
            path = Path(directory) / child
            if path.is_symlink():
                children.remove(child)
                errors.append(dict(path=str(path.relative_to(trial)), error='symlink'))
        for name in sorted(files):
            if not name.startswith('ctxpress-runner-exit-') or not name.endswith('.json'):
                continue
            path = Path(directory) / name
            relative = str(path.relative_to(trial))
            try:
                if path.is_symlink():
                    raise ValueError('symlink')
                content = path.read_bytes()
                value = json.loads(content)
                if (not isinstance(value, dict) or value.get('schema') != 'ctxpress.eval.milestone_runner_exit'
                        or value.get('version') != 1):
                    raise ValueError('unknown diagnostic schema')
                if path.read_bytes() != content:
                    raise ValueError('diagnostic changed during read')
                records.append(dict(path=relative, sha256=hashlib.sha256(content).hexdigest(), evidence=value))
            except (OSError, ValueError) as error:
                errors.append(dict(path=relative, error=type(error).__name__))
    return dict(records=sorted(records, key=lambda row: row['path']), errors=errors)
