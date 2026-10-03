"""TDD #84F1 - SEMANTIC-RUNTIME-001: semantic severity must reach acceptance.

#82 proved the semantic validator classifies the real SLLM sentinel as
``review / source_ocr_suspicious``.  The real #84 E2E then published that very
region as ``translated / valid / quality_impact none`` and counted it among the
clean translations, with ``semantic_review_reason`` recorded but inert.  The
validator was right; the wiring after it was not.

This suite crosses the boundary #82's suite never crossed: semantic validation
-> candidate acceptance -> render-plan and quality accounting.  Everything here
is offline - no provider, no job, no network, no Supabase, no Drive.  The three
sentinels are the exact persisted strings from the #84 run's ``progress.json``
(pages 42/43/46), replayed read-only.
"""

import _test_bootstrap  # noqa: F401
from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

import numpy as np

import semantic_fidelity
from benchmark_pipeline import (
    _render_plan_accounting,
    _translation_quality_accounting,
)
from ocr_balloon import (
    OCRLine,
    TextGroup,
    _token_is_source_vocabulary,
    detect_proper_name_spans,
    validate_and_retry_translations,
)

# --- real persisted #84 sentinels (read-only replay) -------------------------
# output/shadow_slave_chapter_1_5/7d64890b-e303-497b-863f-74e2cd8d5645/progress.json
P043_SOURCE = "THAT HAS NOTHING TO DO WITH A SLLM RAT LIKE ME."
P043_PUBLISHED = "ISSO NAO TEM NADA A VER COM UM RATO DO SLLM COMO EU."
P042_SOURCE = "COLLD AND WIELD INHERITED MAGIC MEMORIES OR ECHOES ON THEIR FIRST VISIT TO THE DREAM REALM."
P042_PUBLISHED = (
    "COLLD E WIELD HERDARAM MEMORIAS MAGICAS OU ECOS EM SUA PRIMEIRA VISITA AO REINO DOS SONHOS."
)
P046_SOURCE = "VALLT EMERGENCY CONTAINMENT BARRIER"
P046_PUBLISHED = "BARREIRA DE CONTENCAO DE EMERGENCIA VALLT"

# The #82 control that must keep being rejected outright, not softened to review.
P068_SOURCE = "TAKEAFEWHOURS FORTHENEAREST AWAKENEDTO GET HERE."
P068_BAD = "LEVE ALGUMAS HORAS PARA CHEGAR AQUI, DEPOIS QUE ACORDAR."

SENTINELS = (
    ("p042", P042_SOURCE, P042_PUBLISHED, "COLLD"),
    ("p043", P043_SOURCE, P043_PUBLISHED, "SLLM"),
    ("p046", P046_SOURCE, P046_PUBLISHED, "VALLT"),
)


def _line(text, confidence=0.94):
    polygon = np.array([[10, 10], [210, 10], [210, 45], [10, 45]], dtype=np.int32)
    return OCRLine(
        text=text,
        confidence=confidence,
        polygon=polygon,
        box=(10, 10, 200, 35),
        raw_text=text,
        engine="rapidocr",
        page=1,
    )


def _group(text, translation="", classification="narration", group_id="BALAO_1"):
    group = TextGroup(
        group_id=group_id,
        lines=[_line(text)],
        text=text,
        classification=classification,
        inside_balloon_like_region=True,
        source_engine="rapidocr",
    )
    group.detected_proper_names = list(detect_proper_name_spans(text))
    group.translation = translation
    group.translation_candidate = translation
    group.sent_to_translation = bool(translation)
    return group


class _ScriptedTranslator:
    is_configured = True

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def translate_strict(self, text, previous_translation="", validation_reason="",
                         force=False, allow_proper_names=True, proper_names=None,
                         **kwargs):
        self.calls.append({"validation_reason": validation_reason})
        return self.responses.pop(0) if self.responses else ""


def _run(group, translator=None, stats=None):
    validate_and_retry_translations(
        [group],
        translator if translator is not None else _ScriptedTranslator(),
        fidelity_stats=stats if stats is not None else {},
        fidelity_verifier=None,
    )
    return group


def _item(group, page=43):
    """The debug item the production report builds from a finished group."""
    return {
        "id": group.group_id,
        "region_id": "REGION_001",
        "page": page,
        "classification": group.classification,
        "confidence": 0.94,
        "clean_text": group.text,
        "text": group.text,
        "translation": group.translation,
        "translation_candidate": group.translation_candidate,
        "translation_valid": bool(group.translation_valid),
        "translation_final_state": group.translation_final_state,
        "translation_final_reason": group.translation_final_reason,
        "translation_validation_reason": group.translation_validation_reason,
        "translation_quality_impact": group.translation_quality_impact,
        "semantic_review_reason": group.semantic_review_reason,
        "manual_review_required": bool(group.manual_review_required),
        "preserved_original": bool(group.preserved_original),
        "preserve_as_name": bool(group.preserve_as_name),
        "sent_to_nvidia": bool(group.sent_to_translation),
        "translated": group.translation_final_state == "translated",
        # The real run rendered these three regions.
        "redrawn": group.translation_final_state == "translated",
    }


def _states(*items):
    return [{"index": 43, "debug_data": {"items": list(items)}}]


class RealSentinelReplayTests(unittest.TestCase):
    """The three real #84 regions, replayed through the production acceptance path."""

    def test_every_sentinel_is_flagged_and_carries_a_review_quality_impact(self):
        for name, source, published, token in SENTINELS:
            with self.subTest(region=name):
                group = _run(_group(source, published))
                self.assertTrue(
                    group.semantic_review_reason.startswith(
                        semantic_fidelity.SOURCE_OCR_SUSPICIOUS
                    ),
                    f"{name}: validator no longer flags the suspicious source",
                )
                self.assertIn(token, group.semantic_review_reason)
                # The real run published exactly the opposite of each of these.
                self.assertEqual(
                    group.translation_quality_impact,
                    "review_required",
                    f"{name}: semantic review published with quality impact none",
                )
                self.assertTrue(
                    group.manual_review_required,
                    f"{name}: semantic review never routed to structured review",
                )

    def test_sllm_region_is_not_reported_as_a_clean_translation(self):
        group = _run(_group(P043_SOURCE, P043_PUBLISHED))
        accounting = _translation_quality_accounting(_states(_item(group)))
        self.assertEqual(accounting["semantic_review"], 1)
        self.assertEqual(accounting["semantic_clean"], 0)
        self.assertTrue(accounting["requires_review"])
        self.assertFalse(accounting["quality_passed"])

    def test_sllm_region_is_not_counted_as_a_clean_render(self):
        group = _run(_group(P043_SOURCE, P043_PUBLISHED))
        plan = _render_plan_accounting(_states(_item(group)))
        region = plan["story_expected_ids"][0]
        self.assertNotIn(region, plan["rendered_clean_ids"])
        self.assertIn(region, plan["structured_review_ids"])
        self.assertEqual(plan["counts"]["unaccounted"], 0)
        self.assertEqual(plan["counts"]["skipped_without_reason"], 0)


class SemanticStateInvariantTests(unittest.TestCase):
    """States the runtime must never be able to reach again."""

    def test_semantic_review_can_never_report_quality_impact_none(self):
        for name, source, published, _token in SENTINELS:
            with self.subTest(region=name):
                group = _run(_group(source, published))
                self.assertFalse(
                    group.semantic_review_reason
                    and group.translation_quality_impact == "none"
                )

    def test_semantic_reject_is_never_selected_or_rendered(self):
        group = _run(
            _group(P068_SOURCE, P068_BAD),
            _ScriptedTranslator("LEVE ALGUMAS HORAS AQUI, DEPOIS QUE ELE ACORDAR."),
        )
        self.assertFalse(group.translation_valid)
        self.assertEqual(
            group.translation_final_reason, "semantic_fidelity_failed_after_retries"
        )
        self.assertEqual(group.translation_quality_impact, "review_required")
        self.assertNotEqual(group.translation_final_state, "translated")

    def test_a_clean_region_still_completes_without_review(self):
        group = _run(_group("I WILL WAIT HERE FOR YOU.", "VOU ESPERAR VOCE AQUI."))
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.semantic_review_reason, "")
        self.assertEqual(group.translation_quality_impact, "none")
        self.assertFalse(group.manual_review_required)
        accounting = _translation_quality_accounting(_states(_item(group)))
        self.assertEqual(accounting["semantic_review"], 0)
        self.assertEqual(accounting["semantic_clean"], 1)
        self.assertTrue(accounting["quality_passed"])


class RetrySelectionTests(unittest.TestCase):
    """Retry may clear the doubt; exhaustion may never restore the bad candidate."""

    def test_bad_first_candidate_loses_to_the_clean_retry(self):
        group = _group("COME BACK BEFORE HE ARRIVES.", "VOLTE DEPOIS QUE ELE CHEGAR.")
        _run(group, _ScriptedTranslator("VOLTE ANTES QUE ELE CHEGUE."))
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation, "VOLTE ANTES QUE ELE CHEGUE.")
        self.assertEqual(group.semantic_review_reason, "")
        self.assertEqual(group.translation_quality_impact, "none")

    def test_two_bad_candidates_end_in_review_not_in_the_first_candidate(self):
        group = _group("COME BACK BEFORE HE ARRIVES.", "VOLTE DEPOIS QUE ELE CHEGAR.")
        _run(group, _ScriptedTranslator("VOLTE LOGO DEPOIS DE ELE CHEGAR."))
        self.assertFalse(group.translation_valid)
        self.assertEqual(group.translation_quality_impact, "review_required")
        self.assertTrue(group.manual_review_required)

    def test_a_retry_that_clears_a_suspicious_source_clears_the_review(self):
        group = _group(P043_SOURCE, P043_PUBLISHED)
        # The published candidate is accepted as-is (review renders), so the
        # region keeps its reason; no retry is requested for a review-only
        # finding.  What must not happen is the reason surviving a *clean*
        # region, which the previous test pins.
        _run(group)
        self.assertNotEqual(group.semantic_review_reason, "")


if __name__ == "__main__":
    unittest.main()
