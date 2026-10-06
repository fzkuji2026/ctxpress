"""Snapshot a configured method once, then isolate mutable state per session."""
from __future__ import annotations
import copy
from ctxpress.methods.base import Method


def frozen_factory(method):
    """Clone a resolved method, including any loaded external policy statistics.

    Configuration files are inputs to construction, not live switches within a
    run. A new manager or launch can load a changed file. Cloning also prevents a
    caller or one session from changing another session's method state.
    """
    if not isinstance(method, Method):
        raise TypeError('the method factory must return a Method')
    template = copy.deepcopy(method)
    return lambda: copy.deepcopy(template)
