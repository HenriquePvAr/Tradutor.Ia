"""First project import for every discoverable test module.

`conftest.py` covers pytest, while direct ``unittest`` discovery has no equivalent central
hook. Importing this tiny bootstrap before any production module gives both runners the same
fail-closed socket/DNS guard, an isolated temporary runtime root instead of the project's
real ``.cache/runtime``, and prevents production .env/.env.local files from being loaded
inside tests. A static regression test checks that every discoverable test module keeps this
import.
"""

import os

from hermetic_runtime import install_runtime_isolation_guard
from offline_test_guard import install_offline_network_guard


os.environ.setdefault("TRADUTOR_IA_HERMETIC_TEST_ENV", "1")
install_offline_network_guard()
install_runtime_isolation_guard()
