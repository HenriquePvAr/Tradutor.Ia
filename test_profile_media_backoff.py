"""Profile-media upstream backoff: short, self-healing, never masks; and the remote
enrichment fallback leaves a sanitized breadcrumb instead of being fully silent.

The upstream Edge Function fails transiently (intermittent 500).  A long 300s window hid an
upstream recovery for minutes; the backoff is now bounded exponential (30s -> 60s -> 120s
cap) and a single success clears it immediately.  These are process-local, no-network tests.
"""
import os
import unittest

import app_ui
from ui_helpers import mask_secrets  # noqa: F401 - ensures ui_helpers import path is intact


class BackoffPolicyTests(unittest.TestCase):
    def setUp(self):
        app_ui._PROFILE_MEDIA_UPSTREAM_BACKOFF.clear()
        self.addCleanup(app_ui._PROFILE_MEDIA_UPSTREAM_BACKOFF.clear)

    def test_bounded_exponential_schedule(self):
        self.assertEqual(app_ui._PROFILE_MEDIA_BACKOFF_BASE_SECONDS, 30.0)
        self.assertEqual(app_ui._PROFILE_MEDIA_BACKOFF_MAX_SECONDS, 120.0)
        key = ("user-1", "avatar")
        delays = [app_ui._profile_media_backoff_arm(key, 1000.0) for _ in range(5)]
        # 30 -> 60 -> 120 -> 120 (capped), never the old 300.
        self.assertEqual(delays, [30.0, 60.0, 120.0, 120.0, 120.0])
        self.assertLessEqual(max(delays), 120.0)
        self.assertNotIn(300.0, delays)

    def test_arm_stores_until_and_streak(self):
        key = ("user-2", "banner")
        app_ui._profile_media_backoff_arm(key, 500.0)
        until, streak = app_ui._PROFILE_MEDIA_UPSTREAM_BACKOFF[key]
        self.assertEqual(streak, 1)
        self.assertEqual(until, 530.0)

    def test_success_clears_backoff_immediately(self):
        # Model the route's outcome handling: a success pop() clears the negative state,
        # so a recovered upstream is not blocked for the rest of the window.
        key = ("user-3", "avatar")
        app_ui._profile_media_backoff_arm(key, 100.0)
        self.assertIn(key, app_ui._PROFILE_MEDIA_UPSTREAM_BACKOFF)
        app_ui._PROFILE_MEDIA_UPSTREAM_BACKOFF.pop(key, None)  # what a 200/404/401 does
        self.assertNotIn(key, app_ui._PROFILE_MEDIA_UPSTREAM_BACKOFF)

    def test_old_fixed_300s_constant_is_gone(self):
        self.assertFalse(hasattr(app_ui, "_PROFILE_MEDIA_UPSTREAM_BACKOFF_SECONDS"))


class RouteContractSourceTests(unittest.TestCase):
    """Source-level guarantees for the media route the unit tests cannot reach directly."""
    SRC = open(os.path.join(os.path.dirname(__file__), "app_ui.py"), encoding="utf-8").read()

    def test_success_and_semantic_paths_clear_backoff(self):
        # 200 success, 401/403, and 404 each pop the key (no lingering block).
        self.assertGreaterEqual(self.SRC.count("_PROFILE_MEDIA_UPSTREAM_BACKOFF.pop(backoff_key, None)"), 3)

    def test_5xx_and_network_arm_exponential_backoff(self):
        self.assertEqual(self.SRC.count("_profile_media_backoff_arm(backoff_key, now_mono)"), 2)

    def test_backoff_window_returns_502_not_empty_200(self):
        assert 'safe_error_code="profile_media_backoff"' in self.SRC
        assert 'status_code=502, detail="profile_media_unavailable"' in self.SRC

    def test_remote_enrichment_fallback_is_logged_but_fail_open(self):
        # Fail-open kept (remote_profile = {}), plus a sanitized breadcrumb, no token/trace.
        assert "remote_profile = {}" in self.SRC
        assert "PROFILE_MEDIA_REMOTE_ENRICH_FALLBACK" in self.SRC
        assert 'safe_error_code="profile_remote_enrich_unavailable"' in self.SRC
        # the fallback log carries only the exception class/module, never the token/trace
        fallback = self.SRC.split("PROFILE_MEDIA_REMOTE_ENRICH_FALLBACK", 1)[1].split(")", 1)[0]
        assert "token" not in fallback.lower()
        assert "traceback" not in fallback.lower()


if __name__ == "__main__":
    unittest.main()
