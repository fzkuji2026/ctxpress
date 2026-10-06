"""Platform rules for the suite.

`linux_only` marks tests of the Linux container harness (Docker mounts, POSIX paths and process groups, shell-script
fixtures); they run on Linux only. Tests of the Harbor and Pier adapters need Python 3.12 or later, like the official
runtimes they drive. Tests that build symlinks are skipped where the account may not create them (Windows without
Developer Mode); everywhere else they run.
"""
import os
import sys

import pytest

SYMLINK_PRIVILEGE = 1314     # ERROR_PRIVILEGE_NOT_HELD
# Harbor and Pier (Terminal-Bench, Science, SWE-bench Pro V2, DeepSWE) run on Python 3.12 or later
HARBOR_RUNTIME_TESTS = {'test_deep_swe', 'test_harbor_catalog', 'test_harbor_driver', 'test_harbor_gpu', 'test_harbor_modern',
                        'test_swe_pro'}


def pytest_configure(config):
    config.addinivalue_line('markers', 'linux_only: tests the Linux container harness; skipped elsewhere')


def pytest_collection_modifyitems(config, items):
    linux = pytest.mark.skip(reason='Linux container harness (POSIX paths, Docker mounts, process groups)')
    harbor = pytest.mark.skip(reason='the official Harbor and Pier runtimes require Python 3.12 or later')
    for item in items:
        if not sys.platform.startswith('linux') and item.get_closest_marker('linux_only'):
            item.add_marker(linux)
        if sys.version_info < (3, 12) and item.module.__name__.rsplit('.', 1)[-1] in HARBOR_RUNTIME_TESTS:
            item.add_marker(harbor)


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
