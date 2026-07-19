"""First project import for every discoverable test module.

`conftest.py` covers pytest, while direct ``unittest`` discovery has no equivalent central
hook. Importing this tiny bootstrap before any production module gives both runners the same
fail-closed socket/DNS guard. A static regression test checks that every discoverable test
module keeps this import.
"""

from offline_test_guard import install_offline_network_guard


install_offline_network_guard()
