"""TDD #84F10 - a page that failed analysis may never leave quality accounting.

Real run #84F9 (job da03e778..., run 6d680aec...) ended ``review_required`` with
five pages in ``completed_with_error``: p004, p008, p019, p032, p041.  Their whole
persisted diagnosis was the string ``"str"``, and p032 - which carries ordinary
narration - reached the final PDF still in English.

Two blind spots are pinned here:

``PAGE-ERROR-OBSERVABILITY-001``
    The exception that killed the page must survive as structured, secret-free
    evidence.  ``"str"`` is the type name of the *message* the caller had already
    flattened, which is exactly no information at all.

``PAGE-ANALYSIS-FAILURE-GATE-001``
    A source page that produced zero OCR/story regions because analysis threw is
    not a page without story content; it is a page whose content was never
    verified.  It must reconcile as an explicit unverified source page and block
    the Beta quality pass, never disappear because there were no regions to count.

A third contract covers the inverse error: a region that physically rendered its
PT-BR under ``RENDER_WITH_REVIEW`` has no source English left on the page and must
stop being reported as an ordinary-story physical residual, while a region that
really did keep its English must keep counting.
"""

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

import benchmark_pipeline
import ocr_balloon


def _page(index, *, status="completed", items=(), **extra):
    state = {
        "index": index,
        "status": status,
        "debug_data": {"items": list(items)},
    }
    state.update(extra)
    return state


def _story_item(**overrides):
    item = {
        "id": "BALAO_1",
        "classification": "narration",
        "clean_text": "AND THEN THE WHOLE PRECINCT WENT QUIET.",
        "translation": "E ENTAO O DISTRITO INTEIRO SILENCIOU.",
        "translation_valid": True,
        "redrawn": True,
        "translation_final_state": "translated",
        "translation_final_reason": "ok",
        "source_completeness_status": "pass",
    }
    item.update(overrides)
    return item


class PageErrorObservabilityTests(unittest.TestCase):
    """The persisted page error must name the real failure, not ``"str"``."""

    def test_an_exception_keeps_its_class_and_stage(self):
        record = benchmark_pipeline._page_error_record(
            ValueError("nao foi possivel ler a pagina"),
            stage="ocr",
            index=32,
        )

        self.assertEqual(record["exception_type"], "ValueError")
        self.assertEqual(record["stage"], "ocr")
        self.assertEqual(record["page"], 32)
        self.assertEqual(record["page_id"], "p032")
        self.assertNotEqual(record["code"], "str")
        self.assertIn("pagina", record["message"])
        self.assertIs(record["retryable"], False)

    def test_a_flattened_message_can_no_longer_become_the_type_name_str(self):
        # The #84F9 shape: the caller had already done ``str(exc)`` before the
        # persistence layer asked the value for its type.
        record = benchmark_pipeline._page_error_record(
            str(RuntimeError("boom")),
            stage="page_analysis",
            index=4,
        )

        self.assertNotEqual(record["code"], "str")
        self.assertEqual(record["code"], "boom")

    def test_secrets_and_urls_never_reach_the_persisted_page_error(self):
        record = benchmark_pipeline._page_error_record(
            RuntimeError(
                "falha em https://api.exemplo.com/v2/translate?auth_key=SEGREDO123"
            ),
            stage="translation",
            index=8,
        )

        self.assertNotIn("SEGREDO123", record["message"])
        self.assertNotIn("auth_key", record["message"])

    def test_a_traceback_is_available_for_local_diagnosis(self):
        try:
            raise KeyError("regiao_ausente")
        except KeyError as exc:
            record = benchmark_pipeline._page_error_record(
                exc, stage="ocr", index=19
            )
            trace = benchmark_pipeline._page_error_traceback(exc)

        self.assertTrue(record["traceback_available"])
        self.assertIn("KeyError", trace)
        self.assertIn("test_page_failure_quality_gate", trace)


class SourcePageAccountingTests(unittest.TestCase):
    """PAGE-ANALYSIS-FAILURE-GATE-001: no source page may vanish silently."""

    # The exact reason #84F9 persisted for all five pages: the RapidOCR quality
    # gate refused its own result and the bounded escalation had no engine to
    # escalate to.  Kept verbatim so this stays a real-evidence regression.
    OCR_ERROR = (
        "ocr_engine_unavailable:too_few_lines_for_text_regions"
        ";paddle_error:ModuleNotFoundError"
    )

    def _84f9_shaped_states(self, structured=True):
        failed = {4, 8, 19, 32, 41}
        states = []
        for index in range(1, 43):
            if index not in failed:
                states.append(_page(index, items=[_story_item()]))
                continue
            state = _page(
                index,
                status="completed_with_error",
                ocr_error=self.OCR_ERROR,
                precheck={"skip": False, "reason": "uncertain_run_ocr"},
            )
            if structured:
                state["page_error"] = {
                    "stage": "ocr",
                    "code": self.OCR_ERROR,
                    "exception_type": "ModuleNotFoundError",
                    "message": self.OCR_ERROR,
                    "retryable": False,
                }
            states.append(state)
        return states

    def test_a_pre_84f10_run_is_still_reconciled_from_its_ocr_error_alone(self):
        # Offline replay of the real #84F9 progress.json: those states predate the
        # structured record, and must still land as explicit unverified pages.
        accounting = benchmark_pipeline._source_page_accounting(
            self._84f9_shaped_states(structured=False), 42
        )

        self.assertEqual(accounting["source_pages_unverified"], 5)
        self.assertFalse(accounting["page_gate_passed"])
        p032 = next(
            item for item in accounting["findings"] if item["page_id"] == "p032"
        )
        self.assertEqual(p032["stage"], "ocr")
        self.assertEqual(p032["error_code"], self.OCR_ERROR)
        self.assertNotEqual(p032["error_code"], "str")

    def test_a_zero_region_failed_page_is_unverified_not_clean(self):
        states = self._84f9_shaped_states()

        accounting = benchmark_pipeline._source_page_accounting(states, 42)

        self.assertEqual(accounting["source_pages_expected"], 42)
        self.assertEqual(accounting["source_pages_analyzed"], 37)
        self.assertEqual(accounting["source_pages_completed_with_error"], 5)
        self.assertEqual(accounting["source_pages_unverified"], 5)
        self.assertEqual(
            accounting["source_pages_unverified_ids"],
            ["p004", "p008", "p019", "p032", "p041"],
        )
        self.assertTrue(accounting["accounting_closed"])
        self.assertFalse(accounting["page_gate_passed"])
        self.assertFalse(accounting["story_output_verified"])

    def test_every_failed_page_produces_an_explicit_finding(self):
        states = self._84f9_shaped_states()

        accounting = benchmark_pipeline._source_page_accounting(states, 42)
        findings = accounting["findings"]

        self.assertEqual(len(findings), 5)
        for finding in findings:
            self.assertEqual(finding["code"], "page_analysis_error")
            self.assertEqual(finding["quality"], "review_required")
            self.assertFalse(finding["story_content_verified"])
        # p032 is the real leak: ordinary narration that reached the PDF in English.
        p032 = next(item for item in findings if item["page_id"] == "p032")
        self.assertEqual(p032["stage"], "ocr")
        self.assertEqual(p032["exception_type"], "ModuleNotFoundError")
        self.assertEqual(p032["error_code"], self.OCR_ERROR)

    def test_a_source_page_missing_from_the_states_is_still_reconciled(self):
        # 42 pages downloaded, only 40 states persisted.  The two that never
        # produced a state are unverified, not absent from the ledger.
        states = [_page(index, items=[_story_item()]) for index in range(1, 41)]

        accounting = benchmark_pipeline._source_page_accounting(states, 42)

        self.assertEqual(accounting["source_pages_missing"], 2)
        self.assertEqual(accounting["source_pages_unverified"], 2)
        self.assertFalse(accounting["page_gate_passed"])
        self.assertTrue(accounting["accounting_closed"])

    def test_a_fully_analysed_chapter_still_passes_the_page_gate(self):
        states = [_page(index, items=[_story_item()]) for index in range(1, 6)]

        accounting = benchmark_pipeline._source_page_accounting(states, 5)

        self.assertEqual(accounting["source_pages_unverified"], 0)
        self.assertEqual(accounting["findings"], [])
        self.assertTrue(accounting["page_gate_passed"])
        self.assertTrue(accounting["story_output_verified"])

    def test_the_physical_gate_still_refuses_a_partially_analysed_chapter(self):
        # Region-level accounting stays as it was: this is the guard that the
        # page-level ledger complements, not replaces.
        states = self._84f9_shaped_states()

        physical = benchmark_pipeline._physical_residual_accounting(states)

        self.assertEqual(physical["physical_population"]["pages_upstream_failed"], 5)
        self.assertFalse(physical["physical_gate_passed"])


class RenderedUnderReviewIsNotAResidualTests(unittest.TestCase):
    """Final physical state is authoritative; review is not English residual."""

    def _rendered_under_review_item(self, **overrides):
        item = _story_item(
            translation_final_state="manual_review",
            translation_final_reason="terminology_conflict_after_retries",
            manual_review_required=True,
            render_disposition=ocr_balloon.RENDER_WITH_REVIEW,
            render_disposition_reason="terminology_conflict_after_retries",
            art_reconstruction_status="clean",
            art_reconstruction_reason="",
        )
        item.update(overrides)
        return item

    def test_ptbr_rendered_under_review_is_not_an_ordinary_story_residual(self):
        states = [_page(1, items=[self._rendered_under_review_item()])]

        physical = benchmark_pipeline._physical_residual_accounting(states)

        self.assertEqual(physical["ordinary_story_physical_residual_ids"], [])
        self.assertEqual(physical["ordinary_story_physical_residual_count"], 0)
        self.assertEqual(physical["physical_source_residual_count"], 0)
        self.assertEqual(physical["physical_regions_rendered_with_review"], 1)
        self.assertEqual(physical["physical_regions_expected"], 1)

    def test_a_region_whose_source_lettering_survived_still_counts(self):
        states = [
            _page(
                1,
                items=[
                    self._rendered_under_review_item(
                        art_reconstruction_status="review",
                        art_reconstruction_reason=(
                            "residual_source_lettering_after_cleanup"
                        ),
                    )
                ],
            )
        ]

        physical = benchmark_pipeline._physical_residual_accounting(states)

        self.assertEqual(
            physical["ordinary_story_physical_residual_ids"], ["p001:BALAO_1"]
        )

    def test_a_withheld_region_still_counts_as_source_english(self):
        # do_not_render means the English source is still the visible pixels.
        states = [
            _page(
                1,
                items=[
                    self._rendered_under_review_item(
                        redrawn=False,
                        render_disposition=ocr_balloon.DO_NOT_RENDER,
                        render_disposition_reason="source_text_not_removed",
                    )
                ],
            )
        ]

        physical = benchmark_pipeline._physical_residual_accounting(states)

        self.assertEqual(
            physical["ordinary_story_physical_residual_ids"], ["p001:BALAO_1"]
        )

    def test_a_legacy_item_without_a_render_disposition_is_unchanged(self):
        # #84F9-era manifests carry no render_disposition; those regions were
        # never redrawn and must keep counting exactly as before.
        states = [
            _page(
                1,
                items=[
                    _story_item(
                        translation_final_state="skipped_with_reason",
                        translation_final_reason="translation_not_selected",
                        manual_review_required=True,
                        redrawn=False,
                    )
                ],
            )
        ]

        physical = benchmark_pipeline._physical_residual_accounting(states)

        self.assertEqual(
            physical["ordinary_story_physical_residual_ids"], ["p001:BALAO_1"]
        )


if __name__ == "__main__":
    unittest.main()
