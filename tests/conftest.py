"""Platform rules for the suite.

`linux_only` marks tests of the Linux container harness (Docker mounts, POSIX paths and process groups, shell-script
fixtures); they run on Linux only. Tests that build symlinks are skipped where the account may not create them
(Windows without Developer Mode); everywhere else they run.
"""
import os
import sys

import pytest

SYMLINK_PRIVILEGE = 1314     # ERROR_PRIVILEGE_NOT_HELD


def pytest_configure(config):
    config.addinivalue_line('markers', 'linux_only: tests the Linux container harness; skipped elsewhere')


def pytest_collection_modifyitems(config, items):
    if sys.platform.startswith('linux'):
        return
    skip = pytest.mark.skip(reason='Linux container harness (POSIX paths, Docker mounts, process groups)')
    for item in items:
        if item.get_closest_marker('linux_only'):
            item.add_marker(skip)


def _symlinks_refused():
    try:
        return (yield)
    except OSError as error:
        if getattr(error, 'winerror', None) == SYMLINK_PRIVILEGE:
            pytest.skip('creating symlinks needs Windows Developer Mode or administrator rights')
        raise


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    return (yield from _symlinks_refused())


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    return (yield from _symlinks_refused())
