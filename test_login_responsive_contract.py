from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CSS = (ROOT / "static" / "tradutor_ui.css").read_text(encoding="utf-8")


class CompactAuthenticationViewportContractTest(unittest.TestCase):
    def test_wide_low_viewport_preserves_control_floors(self) -> None:
        interactive = re.sub(
            r"\s+", "", CSS[CSS.index("purposeful authentication interaction") :]
        )
        for marker in (
            "@media(min-width:901px)and(max-height:820px)",
            "@media(min-width:901px)and(max-height:680px)",
        ):
            self.assertIn(marker, interactive)
        self.assertGreaterEqual(
            interactive.count(".auth-login-fieldinput{min-height:52px}"), 2
        )
        self.assertGreaterEqual(
            interactive.count(".auth-login-submit{min-height:51px}"), 2
        )

        marker = "@media (min-width: 1100px) and (max-height: 700px)"
        self.assertIn(marker, CSS)
        compact = re.sub(r"\s+", "", CSS[CSS.index(marker) :])

        prefix = '.auth-login-shell[data-viewport-height="compact"]'
        self.assertIn(f"{prefix}.auth-login-fieldinput{{min-height:52px;}}", compact)
        self.assertIn(f"{prefix}.auth-login-submit{{min-height:51px;}}", compact)
        self.assertIn(
            ".auth-login-toggle-pw{min-width:44px;min-height:44px}",
            re.sub(r"\s+", "", CSS),
        )


if __name__ == "__main__":
    unittest.main()
