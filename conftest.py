"""Pytest collection is offline before test modules are imported."""

import os

from offline_test_guard import install_offline_network_guard


os.environ.setdefault("TRADUTOR_IA_HERMETIC_TEST_ENV", "1")
install_offline_network_guard()
