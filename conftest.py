"""Pytest collection is offline and runtime-isolated before test modules are imported."""

import os

import pytest

from hermetic_runtime import install_runtime_isolation_guard
from offline_test_guard import install_offline_network_guard


os.environ.setdefault("TRADUTOR_IA_HERMETIC_TEST_ENV", "1")
install_offline_network_guard()
install_runtime_isolation_guard()


@pytest.fixture(autouse=True)
def _restore_process_environment():
    """Fail closed against cross-test os.environ contamination.

    A test that mutates os.environ and skips its own restore (e.g. a fragile
    setUp/tearDown, or a CLI path that loads the real .env.local and leaks
    BETA_LICENSE_PROVIDER) used to poison every later test, making the whole
    suite order-dependent.  Snapshot the process environment around each test and
    restore it exactly, so leaks stay local to the test that caused them.  Only
    env vars are touched — never test data or setUpClass-seeded state.
    """
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        if os.environ != snapshot:
            os.environ.clear()
            os.environ.update(snapshot)
