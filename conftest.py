"""Pytest collection is offline and runtime-isolated before test modules are imported."""

import os

from hermetic_runtime import install_runtime_isolation_guard
from offline_test_guard import install_offline_network_guard


os.environ.setdefault("TRADUTOR_IA_HERMETIC_TEST_ENV", "1")
install_offline_network_guard()
install_runtime_isolation_guard()
