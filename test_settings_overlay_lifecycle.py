"""Hermetic tests for the Settings/preload overlay lifecycle in the Comix resolver.

These prove the fixed contract of ``_configure_preload_all``:
  1. the Settings close runs even when preload throws (finally);
  2. a preload failure with the modal closed continues with partial preload;
  3. a modal that stays open fails closed (``reader_overlay_visible``) and is NOT
     swallowed by the preload ``except``;
  5. closing when no modal exists is a safe no-op.
Plus the overlay gate predicate (4) that guards missing-loop canonical capture.

No browser, no network, no real reader state is touched.  The pure module helpers
(``_extract_candidates`` / ``_close_reader_settings`` / ``_read_reader_overlay_state``)
are replaced with recording doubles — this is a unit test double, not a stateful stub
of a live reader run.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import scrapling_reader_resolver as resolver


class _FakePreloadPage:
    """Returns a canned preload/DOM state for every evaluate; timeouts are no-ops."""

    def __init__(self):
        self.evaluate_count = 0

    def wait_for_timeout(self, _ms):
        return None

    def evaluate(self, _script, *_args):
        self.evaluate_count += 1
        return {
            "some": True, "all": True, "checked": "all", "direction": "ltr",
            "count": 95, "loaded": 95, "controls": 105,
        }


def _expanded_candidates():
    return [
        {"logical_page_index": index, "source": "browser_response", "url": f"x/{index}"}
        for index in range(1, 96)
    ]


def _call(page, scroll_diagnostics):
    return resolver._configure_preload_all(
        page,
        candidates=[{"logical_page_index": 1, "source": "browser_response"}],
        selector="img.rpage-page__img",
        url="https://comix.to/title/x/chapter",
        events=[],
        cancel_check=None,
        browser_bodies={},
        scroll_diagnostics=scroll_diagnostics,
    )


class _RecordingClose:
    """Stands in for _close_reader_settings; records calls and reports a fixed result."""

    def __init__(self, *, before_visible, closed):
        self.before_visible = before_visible
        self.closed = closed
        self.calls = 0

    def __call__(self, _page):
        self.calls += 1
        return {
            "before": {
                "settings_modal_visible": self.before_visible,
                "blocking_overlay_visible": self.before_visible,
                "settings_selector": ".rpage-modal--settings" if self.before_visible else "",
            },
            "after": {"settings_modal_visible": not self.closed,
                      "blocking_overlay_visible": not self.closed},
            "closed": self.closed,
        }


_CLEAN_OVERLAY = {"settings_modal_visible": False, "blocking_overlay_visible": False}
_VISIBLE_OVERLAY = {"settings_modal_visible": True, "blocking_overlay_visible": True}


class SettingsOverlayLifecycleTest(unittest.TestCase):
    # 1) A throw before the close must NOT skip the close (it now lives in finally).
    def test_close_runs_even_when_preload_throws(self):
        close = _RecordingClose(before_visible=True, closed=True)
        diagnostics = {}
        with mock.patch.object(resolver, "_extract_candidates",
                               side_effect=RuntimeError("Execution context was destroyed")), \
             mock.patch.object(resolver, "_close_reader_settings", close), \
             mock.patch.object(resolver, "_read_reader_overlay_state",
                               return_value=_CLEAN_OVERLAY):
            _call(_FakePreloadPage(), diagnostics)
        self.assertEqual(close.calls, 1)
        self.assertTrue(diagnostics["settings_close_finally_entered"])
        self.assertTrue(diagnostics["settings_close_helper_called"])
        self.assertEqual(diagnostics["preload_exception_type"], "RuntimeError")
        self.assertEqual(diagnostics["preload_exception_stage"], "extract_candidates")
        self.assertIn("destroyed", diagnostics["preload_exception_message"])

    # 2) Preload failed but modal closed -> continue with partial preload (no abort).
    def test_preload_failure_with_modal_closed_continues_partial(self):
        close = _RecordingClose(before_visible=True, closed=True)
        diagnostics = {}
        with mock.patch.object(resolver, "_extract_candidates",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(resolver, "_close_reader_settings", close), \
             mock.patch.object(resolver, "_read_reader_overlay_state",
                               return_value=_CLEAN_OVERLAY):
            candidates, selector = _call(_FakePreloadPage(), diagnostics)
        # partial preload preserved (the single input candidate), no exception raised
        self.assertEqual(len(candidates), 1)
        self.assertEqual(selector, "img.rpage-page__img")
        self.assertEqual(diagnostics["preload_mode_discovery"], "failed")
        self.assertFalse(diagnostics["settings_visible_after_finally"])
        self.assertEqual(diagnostics["overlay_gate_after_preload"], "PASS")

    # 3) Modal still visible after finally -> fail closed, not swallowed.
    def test_modal_not_closed_fails_closed(self):
        close = _RecordingClose(before_visible=True, closed=False)
        diagnostics = {}
        with mock.patch.object(resolver, "_extract_candidates",
                               return_value=(_expanded_candidates(), "sel")), \
             mock.patch.object(resolver, "_close_reader_settings", close), \
             mock.patch.object(resolver, "_read_reader_overlay_state",
                               return_value=_VISIBLE_OVERLAY):
            with self.assertRaises(resolver.DynamicReaderError) as ctx:
                _call(_FakePreloadPage(), diagnostics)
        self.assertEqual(ctx.exception.code, "reader_overlay_visible")
        self.assertTrue(diagnostics["settings_visible_after_finally"])
        self.assertEqual(diagnostics["overlay_gate_after_preload"], "FAIL")
        self.assertEqual(close.calls, 1)  # close was still attempted in finally

    # 3b) Fail-closed also fires when preload itself threw and modal remained open.
    def test_throw_and_modal_open_still_fails_closed(self):
        close = _RecordingClose(before_visible=True, closed=False)
        with mock.patch.object(resolver, "_extract_candidates",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(resolver, "_close_reader_settings", close), \
             mock.patch.object(resolver, "_read_reader_overlay_state",
                               return_value=_VISIBLE_OVERLAY):
            with self.assertRaises(resolver.DynamicReaderError) as ctx:
                _call(_FakePreloadPage(), {})
        self.assertEqual(ctx.exception.code, "reader_overlay_visible")

    # 4) Overlay gate predicate that guards every missing-loop canonical capture.
    def test_overlay_gate_predicate(self):
        self.assertTrue(resolver._overlay_blocks_capture(
            {"settings_modal_visible": True, "blocking_overlay_visible": False}))
        self.assertTrue(resolver._overlay_blocks_capture(
            {"settings_modal_visible": False, "blocking_overlay_visible": True}))
        self.assertFalse(resolver._overlay_blocks_capture(_CLEAN_OVERLAY))

    # 5) No modal present -> close is a safe no-op, resolver continues.
    def test_no_modal_present_is_safe_noop(self):
        close = _RecordingClose(before_visible=False, closed=True)
        diagnostics = {}
        with mock.patch.object(resolver, "_extract_candidates",
                               return_value=(_expanded_candidates(), "sel")), \
             mock.patch.object(resolver, "_close_reader_settings", close), \
             mock.patch.object(resolver, "_read_reader_overlay_state",
                               return_value=_CLEAN_OVERLAY):
            candidates, _ = _call(_FakePreloadPage(), diagnostics)
        self.assertEqual(len(candidates), 95)
        self.assertEqual(close.calls, 1)
        self.assertFalse(diagnostics["settings_close_clicked"])
        self.assertFalse(diagnostics["settings_visible_after_finally"])

    # Close-helper exception is recorded and does not mask the fail-closed gate.
    def test_close_exception_recorded_not_masked(self):
        def _raising_close(_page):
            raise RuntimeError("close click failed")
        diagnostics = {}
        with mock.patch.object(resolver, "_extract_candidates",
                               return_value=(_expanded_candidates(), "sel")), \
             mock.patch.object(resolver, "_close_reader_settings", _raising_close), \
             mock.patch.object(resolver, "_read_reader_overlay_state",
                               return_value=_VISIBLE_OVERLAY):
            with self.assertRaises(resolver.DynamicReaderError) as ctx:
                _call(_FakePreloadPage(), diagnostics)
        self.assertEqual(ctx.exception.code, "reader_overlay_visible")
        self.assertEqual(diagnostics["settings_close_exception_type"], "RuntimeError")


class _RecordingScriptPage:
    """Records every script sent to evaluate; reports the modal as visible so the
    close-click branch of _close_reader_settings actually runs."""

    def __init__(self):
        self.scripts = []

    def evaluate(self, script, *_args):
        self.scripts.append(script)
        return {"settings_modal_visible": True, "blocking_overlay_visible": True}

    def wait_for_timeout(self, _ms):
        return None


class CloseSettingsJsParseTest(unittest.TestCase):
    """Guards against the SyntaxError ("missing ) after argument list") caused by three
    adjacent JS string literals inside querySelector() — Python concatenates adjacent
    string literals, JavaScript does not."""

    def _capture_close_script(self):
        page = _RecordingScriptPage()
        resolver._close_reader_settings(page)
        close = [s for s in page.scripts if "Close settings" in s]
        self.assertTrue(close, "close-settings script was not captured")
        return close[0]

    def test_no_adjacent_string_literals(self):
        # A closing quote followed only by whitespace/newline then an opening quote is
        # the exact adjacency bug (invalid as a single JS argument).
        self.assertNotRegex(self._capture_close_script(), r"'\s*\n\s*'")

    def test_close_settings_js_parses_with_node(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        script = self._capture_close_script()
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as handle:
            handle.write("const f = " + script + ";\n")
            path = handle.name
        try:
            result = subprocess.run([node, "--check", path],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
