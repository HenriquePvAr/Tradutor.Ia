"""The screen follows the worker's stages instead of guessing.

The submit used to hold the request for 93-101s and the page showed one static message the
whole time. Now it returns a queued job, and every stage the worker reports has a label and
its own counter.

Source-level contract tests: no DOM runner in this project, so these read the shipped file.
"""

import _test_bootstrap  # noqa: F401

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JS = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")

WORKER_STAGES = ("queued", "worker_starting", "source_validation", "browser_loading",
                 "source_analysis", "source_lazy_resolution", "source_selection",
                 "downloading_pages", "validating_pages")


class StageLabelTests(unittest.TestCase):
    def test_every_worker_stage_has_a_label(self):
        block = JS[JS.index("const stageMessages = {"):]
        block = block[:block.index("};")]
        for stage in WORKER_STAGES:
            self.assertIn(f"{stage}:", block, stage)

    def test_lazy_resolution_and_selection_are_named(self):
        self.assertIn("carregando páginas do leitor", JS)
        self.assertIn("preparando a ordem das páginas", JS)

    def test_the_backend_agrees_on_the_same_stage_keys(self):
        from ui_bridge import _UI_STAGE_LABELS

        for stage in WORKER_STAGES:
            self.assertIn(stage, _UI_STAGE_LABELS, stage)


class SubmitFlowTests(unittest.TestCase):
    def test_pipeline_panel_is_rendered_before_the_submit_response(self):
        body = JS[JS.index("async function startTranslation"):]
        body = body[:body.index("\n  async function cancelTranslation")]
        self.assertLess(body.index("renderLocalPipelineState("), body.index("await api('/api/ui/run'"))

    def test_the_page_announces_source_validation_not_worker_analysis(self):
        body = JS[JS.index("async function startTranslation"):]
        body = body[:body.index("\n  async function cancelTranslation")]
        self.assertIn("validating_source", body)
        self.assertNotIn("Analisando fonte", body)

    def test_a_double_click_is_still_guarded(self):
        self.assertIn("button.dataset.busy", JS)

    def test_source_analysis_failure_stays_in_the_pipeline_card(self):
        self.assertIn("function renderLocalPipelineState", JS)
        self.assertIn("Não foi possível iniciar o processamento", JS)
        self.assertIn("chromedriver_unavailable", JS)
        self.assertIn("runStatusCard", JS)


class CounterOwnershipTests(unittest.TestCase):
    def test_the_counter_is_bound_to_the_stage_that_produced_it(self):
        self.assertIn("counter_stage", JS)
        self.assertIn("ownsCounter", JS)

    def test_a_foreign_counter_is_not_rendered(self):
        block = JS[JS.index("const counterOwner ="):]
        block = block[:block.index("\n      const elapsed")]
        # Without this the lazy 20/20 would still be on screen during the download.
        self.assertIn("contador indisponível", block)


class ReasonMessageTests(unittest.TestCase):
    def test_each_coded_failure_has_a_sentence(self):
        block = JS[JS.index("const reasonMessages = {"):]
        block = block[:block.index("};")]
        for code in ("source_not_ready", "authentication_required",
                     "incomplete_source_coverage", "no_chapter_images",
                     "incomplete_download"):
            self.assertIn(f"{code}:", block, code)

    def test_analysis_and_download_failures_read_differently(self):
        block = JS[JS.index("const reasonMessages = {"):]
        block = block[:block.index("};")]
        coverage = re.search(r"incomplete_source_coverage: '([^']+)'", block).group(1)
        download = re.search(r"incomplete_download: '([^']+)'", block).group(1)
        self.assertNotEqual(coverage, download)
        self.assertIn("leitor", coverage)
        self.assertIn("baixadas", download)

    def test_the_error_panel_uses_the_sentence(self):
        self.assertIn("reasonText(error.code)", JS)

    def test_no_traceback_or_secret_reaches_the_panel(self):
        panel = JS[JS.index("function showStartError"):]
        panel = panel[:panel.index("async function startTranslation")]
        for bad in ("traceback", "stack", "NVIDIA_API_KEY", "Authorization", ".env"):
            self.assertNotIn(bad, panel, bad)


class PollingTests(unittest.TestCase):
    def test_a_single_polling_timer_with_cleanup(self):
        self.assertEqual(JS.count("setInterval("), 1)
        self.assertIn("clearInterval(getGlobal('__tradutorUiPollingTimer'))", JS)
        self.assertIn("setGlobal('__tradutorUiPollingTimer'", JS)

    def test_review_is_opened_from_polling_not_from_the_submit(self):
        # The worker produces the review now, so the runtime payload is what opens it.
        self.assertIn("runtime.source_review", JS)
        self.assertIn("renderSourceReview(runtime.source_review)", JS)

    def test_queued_has_a_status_label(self):
        self.assertIn("queued: 'na fila'", JS)


if __name__ == "__main__":
    unittest.main()
