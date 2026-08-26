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
        self.assertIn(
            "eligible: baseEligible && ownerReady && qualityApproved && publishableTerminal",
            block,
        )
        action = self.js[self.js.index("function publicationAction"):]
        action = action[:action.index("\n  function reviewAction")]
        self.assertIn("Revis\\u00e3o necess\\u00e1ria", action)

    def test_smart_split_review_item_is_visible_but_not_bulk_selectable(self):
        review = self.js[self.js.index("function renderQualityReview"):]
        review = review[:review.index("\n  function visibleQualityReviewKeys")]
        self.assertIn("data-review-type", review)
        self.assertIn("Tipo: smart_split", review)
        self.assertIn("boundary:", review)
        visible_keys = self.js[self.js.index("function visibleQualityReviewKeys"):]
        visible_keys = visible_keys[:visible_keys.index("\n  function updateQualityReviewSelectionUi")]
        self.assertIn("item.dataset.reviewType !== 'smart_split'", visible_keys)

    def test_successful_source_analysis_adopts_the_draft_and_exposes_start_reasons(self):
        validate = self.js[self.js.index("async function validateSource"):]
        validate = validate[:validate.index("\n  // Single flight, same shape as refreshBootstrap")]
        self.assertIn("appState.newTranslationDraft = false", validate)
        self.assertIn("result?.policy", validate)
        self.assertIn("workspace_source_policy", validate)

        controls = self.js[self.js.index("function updateTranslationStartControls"):]
        controls = controls[:controls.index("\n  function", 20)]
        self.assertIn("translationStartDisabledReasons()", controls)
        self.assertIn("appState.lastStartDisabledReasons", controls)
        self.assertIn("start_disabled_reasons", self.js)

    def test_draft_cleanup_never_wipes_a_live_queue_created_pipeline(self):
        runtime = self.js[self.js.index("function renderRuntime(runtime)"):]
        runtime = runtime[:runtime.index("\n  function renderRunStatus")]
        self.assertIn("const draftOnly = appState.newTranslationDraft && !appState.reviewMode && !running", runtime)
        self.assertIn("const activeRecord =", runtime)
        self.assertIn("|| queuedRecord", runtime)

    def test_backend_progress_counters_are_keyed_by_real_stage_not_human_label(self):
        bridge = read("ui_bridge.py")
        self.assertIn("counter_progress_stages = {", bridge)
        self.assertIn('"download"', bridge)
        self.assertIn('"ocr"', bridge)
        self.assertIn("'tradução nvidia': 'translate'", self.js)
        self.assertNotIn('stage in {\n                        "Baixando imagens"', bridge)

    def test_current_job_artifacts_must_match_job_identity_before_result_claim(self):
        bridge = read("ui_bridge.py")
        self.assertIn("def _artifact_manifest_matches_job", bridge)
        self.assertIn("run_manifest.json", bridge)
        self.assertIn("job_manifest.json", bridge)
        self.assertIn("result_metrics = self._current_job_result_metrics(job)", bridge)

    def test_translation_provider_is_explicit_in_start_payload_and_job_command(self):
        # The payload now carries the operator's explicit form choice; the runtime default
        # is only the fallback when no provider was ever selected.
        self.assertIn("translation_provider: form.translationProvider", self.js)
        self.assertIn(
            "normalizeTranslationProvider(appState.settings?.translation_provider)", self.js)
        bridge = read("ui_bridge.py")
        helpers = read("ui_helpers.py")
        self.assertIn('"translation_provider": translation_provider', bridge)
        self.assertIn("translation_provider=normalized[\"translation_provider\"]", bridge)
        self.assertIn("command.extend([\"--translation-provider\", provider])", helpers)


class BetaControlSurfaceContracts(unittest.TestCase):
    """#84F10: the start form offers exactly the choices the Beta really has."""

    def setUp(self):
        self.js = read("static/tradutor_ui.js")
        self.html = read("ui/ui_shell.html")
        self.css = read("static/tradutor_ui.css")

    def test_processing_mode_is_a_select_like_the_other_engine_fields(self):
        self.assertIn('<select id="modeSelect">', self.html)
        field = self.html[self.html.index("Modo de processamento"):]
        field = field[:field.index("<label>Escopo</label>")]
        self.assertNotIn("choice-card", field)
        self.assertNotIn("choice-row", field)

    def test_the_three_large_mode_cards_are_gone_from_the_beta_form(self):
        self.assertNotIn("choice-card", self.html)
        self.assertNotIn("choice-row", self.html)

    def test_quality_is_the_default_and_the_internal_values_are_unchanged(self):
        field = self.html[self.html.index('<select id="modeSelect">'):]
        field = field[:field.index("</select>")]
        self.assertIn('<option value="quality" selected>Qualidade</option>', field)
        # Order is the product request: Qualidade, Rápido, Download-only.
        self.assertLess(field.index('value="quality"'), field.index('value="fast"'))
        self.assertLess(field.index('value="fast"'), field.index('value="download_only"'))
        self.assertIn("selectedMode: 'quality'", self.js)

    def test_the_mode_select_drives_the_same_state_the_cards_drove(self):
        self.assertIn("#modeSelect", self.js)
        self.assertIn("function applyProcessingMode", self.js)
        self.assertNotIn("$$('.choice-card')", self.js)

    def test_each_mode_shows_its_own_compact_help_text(self):
        self.assertIn('id="modeHint"', self.html)
        for phrase in (
            "Usa processamento mais conservador",
            "otimizações automáticas",
            "sem OCR, tradução ou PDF",
        ):
            self.assertIn(phrase, self.js)

    def test_the_single_ocr_engine_field_does_not_imply_a_dropdown(self):
        self.assertIn("#ocrEngineSelect", self.css)
        rule = self.css[self.css.index("#ocrEngineSelect"):]
        rule = rule[:rule.index("}") + 1]
        self.assertIn("appearance:none", rule)

    def test_the_displayed_ocr_engine_follows_the_configured_engine(self):
        self.assertIn("function applyOcrEngineStatus", self.js)
        block = self.js[self.js.index("function applyOcrEngineStatus"):]
        block = block[:block.index("\n  function ")]
        self.assertIn("settings.ocr_engine", self.js)
        self.assertIn("RapidOCR · Ativo", block)
        # No second engine may be offered: the label is derived from the
        # configured id, never from a hardcoded list of alternatives.
        self.assertNotIn("Paddle", self.html)
        self.assertNotIn("Tesseract", self.html)
        for literal in ("'Paddle", '"Paddle', "'Tesseract", '"Tesseract'):
            self.assertNotIn(literal, self.js)


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
