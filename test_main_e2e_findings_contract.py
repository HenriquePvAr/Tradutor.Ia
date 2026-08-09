"""Regression contracts for the real main-branch E2E findings.

These are deliberately hermetic.  They do not open the app, do not fetch any
reader page and do not use remote credentials; they pin the local UI/backend
contracts that the E2E run showed were too easy to regress.
"""

import _test_bootstrap  # noqa: F401

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class MainE2eUiFindingContracts(unittest.TestCase):
    def setUp(self):
        self.js = read("static/tradutor_ui.js")
        self.html = read("ui/ui_shell.html")

    def test_cancel_control_is_visible_and_the_visible_button_is_used(self):
        self.assertIn("function visibleCancelControl", self.js)
        self.assertIn("visibleCancelControl() || $('#cancelSourceReview')", self.js)
        controls = self.js[self.js.index("function setRunControls"):]
        controls = controls[:controls.index("\n  function setTranslationFormLocked")]
        self.assertIn("cancelBtn.hidden = !active", controls)
        self.assertIn("cancelBtn.classList.toggle('show', active)", controls)

    def test_pipeline_progress_uses_the_pipeline_record_status_not_global_ready(self):
        block = self.js[self.js.index("function renderProgress"):]
        block = block[:block.index("\n  function shouldRenderSourceReview")]
        self.assertIn("const stageActiveStatus =", block)
        active_line = next(line for line in block.splitlines() if "item.classList.toggle('active'" in line)
        self.assertIn("stageActiveStatus", active_line)
        self.assertNotIn("appState.status === 'running'", active_line)

    def test_queue_view_projects_the_active_translation_job(self):
        self.assertIn("function queueItemsForDisplay", self.js)
        queue_fn = self.js[self.js.index("function renderQueue"):]
        queue_fn = queue_fn[:queue_fn.index("\n  $('#queueAddBtn')")]
        self.assertIn("queueItemsForDisplay()", queue_fn)
        helper = self.js[self.js.index("function queueItemsForDisplay"):]
        helper = helper[:helper.index("\n  function renderQueue")]
        self.assertIn("appState.currentPipelineState", helper)
        self.assertIn("inFlightStatuses.has(status)", helper)
        self.assertIn("item.id === projected.id", helper)
        self.assertIn("items.unshift(projected)", helper)

    def test_queue_projection_is_inflight_only_and_cannot_leave_terminal_ghost(self):
        helper = self.js[self.js.index("function queueItemsForDisplay"):]
        helper = helper[:helper.index("\n  function renderQueue")]
        self.assertIn("if (id && inFlightStatuses.has(status))", helper)
        self.assertNotIn("terminalRunStatuses.has(status)", helper)

    def test_bootstrap_ready_surface_is_not_left_in_new_translation(self):
        self.assertIn("function clearBootstrapSurfaceForFreshTranslation", self.js)
        tab_block = self.js[self.js.index("function activateTab"):]
        tab_block = tab_block[:tab_block.index("\n  $$('.rail-tab')")]
        self.assertIn("clearBootstrapSurfaceForFreshTranslation()", tab_block)

    def test_sidebar_tabs_are_native_buttons_inside_list_items(self):
        self.assertNotRegex(self.html, r"<li\s+class=\"rail-tab\b")
        self.assertRegex(self.html, r"<li>\s*<button type=\"button\" class=\"rail-tab active\" data-tab=\"inicio\"")
        self.assertRegex(self.html, r"<button type=\"button\" class=\"rail-tab\" data-tab=\"nova\"")

    def test_community_publish_stays_fail_closed_on_quality_gate_failure(self):
        block = self.js[self.js.index("function publicationEligibility"):]
        block = block[:block.index("\n  function publicationAction")]
        self.assertIn("qualityApproved", block)
        self.assertIn("eligible: baseEligible && ownerReady && qualityApproved && publishableTerminal", block)
        action = self.js[self.js.index("function publicationAction"):]
        action = action[:action.index("\n  function reviewAction")]
        self.assertIn("Revis\\u00e3o necess\\u00e1ria", action)


class MainE2eDownloadFindingContracts(unittest.TestCase):
    def test_reader_count_mismatch_171_expected_101_downloaded_fails_closed(self):
        from down import _build_download_gate

        expected = [f"reader-slot-{index:03}" for index in range(1, 172)]
        downloaded = [
            {
                "candidate_id": candidate_id,
                "url": f"https://cdn.example/{candidate_id}.jpg",
                "order": index,
                "is_chapter_candidate": True,
            }
            for index, candidate_id in enumerate(expected[:101], start=1)
        ]
        gate = _build_download_gate({
            "expected_chapter_candidate_ids": expected,
            "downloaded": downloaded,
        })

        self.assertFalse(gate["passed"])
        self.assertIn("viewer_count_mismatch", gate["reasons"])
        self.assertIn("viewer_images_missing", gate["reasons"])
        self.assertEqual(gate["expected_viewer_images"], 171)
        self.assertEqual(gate["downloaded_viewer_images"], 101)
        self.assertEqual(gate["missing_viewer_images"], 70)


if __name__ == "__main__":
    unittest.main()
