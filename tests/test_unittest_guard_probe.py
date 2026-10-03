"""Probe used from a clean ``python -m unittest`` child process.

The bootstrap is the direct-``unittest`` contract: it installs the guard before any project
module. ``sitecustomize.py`` additionally covers guarded Python descendants that inherit the
marker and repository path.
"""

import _test_bootstrap  # noqa: F401

import socket
import unittest

from offline_test_guard import OfflineNetworkAttempt


class UnittestGuardProbe(unittest.TestCase):
    def test_socket_is_blocked_before_any_request(self):
        self.assertTrue(getattr(socket, "_tradutor_ia_offline_guard", False))
        with self.assertRaises(OfflineNetworkAttempt):
            socket.create_connection(("198.51.100.9", 443), timeout=0.01)


if __name__ == "__main__":
    unittest.main()
