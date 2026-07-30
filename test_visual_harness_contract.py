"""Contract for the visual pipeline harness.

The harness listened for `tradutor:auth-changed` while the application has only
ever dispatched `tradutor-auth-changed`. The listener therefore never fired: the
harness appeared to work purely because it also re-rendered on two timers, so a
silent dependency on a fallback was hiding a dead subscription.

The event name is the application's to define. These tests pin the harness to
it, and pin the application to a single name so the divergence cannot be
"fixed" by dispatching both.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
HARNESS = STATIC / "pipeline_loading_harness.js"

CANONICAL_EVENT = "tradutor-auth-changed"
DIVERGENT_EVENT = "tradutor:auth-changed"


def strip_comments(source: str) -> str:
    """Removes comments while leaving string literals intact."""
    source = re.sub(r"/\*[\s\S]*?\*/", "", source)
    pattern = re.compile(
        r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|//[^\n]*""")
    return pattern.sub(lambda m: "" if m.group(0).startswith("//") else m.group(0), source)


class HarnessAuthEventTest(unittest.TestCase):
    harness = strip_comments(HARNESS.read_text(encoding="utf-8"))

    def test_harness_listens_for_the_canonical_event(self):
        self.assertIn(
            f"addEventListener('{CANONICAL_EVENT}'", self.harness,
            "the harness must subscribe to the event the application dispatches")

    def test_the_divergent_event_name_is_gone(self):
        self.assertNotIn(
            DIVERGENT_EVENT, self.harness,
            "a listener for an event nobody dispatches is a dead subscription")

    def test_the_application_dispatches_exactly_one_auth_event_name(self):
        """The divergence must not be resolved by emitting both names."""
        for module in ("auth_ui.js", "tradutor_ui.js"):
            code = strip_comments((STATIC / module).read_text(encoding="utf-8"))
            dispatched = {name for name in re.findall(r"CustomEvent\(\s*'([^']+)'", code)
                          if "auth" in name}
            self.assertLessEqual(dispatched, {CANONICAL_EVENT},
                                 f"{module} dispatches an unexpected auth event name")

    def test_no_new_listener_adopts_the_divergent_name(self):
        """One dead subscription for this name already exists and predates this work.

        static/tradutor_ui.js binds loadModeration to the colon spelling, and
        that line is present unchanged in the functional base a909f30 - the
        moderation panel has never reloaded on an auth change. Fixing it would
        start issuing moderation requests on every auth transition, which is a
        production behaviour change this round is not authorised to make. It is
        pinned here so the count cannot grow silently.
        """
        occurrences = []
        for path in sorted(STATIC.glob("*.js")):
            code = strip_comments(path.read_text(encoding="utf-8"))
            occurrences.extend([path.name] * code.count(DIVERGENT_EVENT))
        self.assertEqual(
            occurrences, ["tradutor_ui.js"],
            "only the known pre-existing moderation listener may use this name")

    def test_remaining_timers_are_documented(self):
        """Any timer left must say why, so a fallback is never silent again."""
        source = HARNESS.read_text(encoding="utf-8")
        for match in re.finditer(r"setTimeout|setInterval", strip_comments(source)):
            line_no = source[:match.start()].count("\n")
            window = "\n".join(source.splitlines()[max(0, line_no - 6):line_no + 1])
            self.assertIn("//", window,
                          "every remaining timer needs a comment stating its purpose")


class HarnessIsolationTest(unittest.TestCase):
    """Guards that must keep holding after the event fix."""

    harness = strip_comments(HARNESS.read_text(encoding="utf-8"))

    def test_harness_is_served_only_under_the_visual_test_flag(self):
        app_ui = (ROOT / "app_ui.py").read_text(encoding="utf-8")
        tree = ast.parse(app_ui)
        guarded = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test_src = ast.unparse(node.test)
            if "visual_test_enabled" not in test_src:
                continue
            if "PIPELINE_HARNESS_ASSET" in ast.unparse(node):
                guarded = True
        self.assertTrue(
            guarded,
            "the harness asset must be injected only inside a visual_test_enabled branch")

    def test_harness_is_fail_closed_in_the_browser(self):
        self.assertIn("__tradutorVisualTestEnabled !== true", self.harness)
        self.assertIn("127.0.0.1", self.harness)
        self.assertIn("hasOwnProperty.call(fixtures, requested)", self.harness)

    def test_harness_performs_no_request_and_persists_nothing(self):
        for forbidden in ("fetch(", "XMLHttpRequest", "localStorage", "sessionStorage",
                          "navigator.sendBeacon", "WebSocket", "EventSource",
                          "indexedDB", "/api/"):
            self.assertNotIn(forbidden, self.harness,
                             f"the harness must not use {forbidden}")

    def test_harness_only_names_stages_it_never_starts(self):
        """The fixtures mention stages by name; the harness must only render them.

        Scanning for words like "ocr" proves nothing - they are fixture keys.
        What matters is that no call leaves the module, which the request and
        persistence test above covers, and that the only thing done with a
        fixture is handing it to the mapper and the renderer.
        """
        self.assertIn("mapJobStateToLoadingView", self.harness)
        self.assertIn("renderProcessingSurface", self.harness)
        for forbidden in ("startJob", "start_job", "postMessage", "Worker("):
            self.assertNotIn(forbidden, self.harness,
                             f"the harness must not use {forbidden}")


if __name__ == "__main__":
    unittest.main()
