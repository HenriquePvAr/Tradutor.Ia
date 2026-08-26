"""TDD #84F14 - final quality reconciliation.

Two independent defects, both proven by the persisted #84F13 run
(job b7b28a3d7a814a3ba64afbc1a9bb896d, run b6e25192-460e-4581-8f76-23af7dfddce3):

**Physical.**  The only three physically retained source regions in the whole
chapter were ``p011:BALAO_1`` (``TAK``), ``p011:BALAO_2`` (``TUR``) and
``p013:BALAO_3`` (``TRNDGE``) - onomatopoeia the balloon detector labelled
``speech``.  The ordinary-story subgate already excluded all three
(``ordinary_story_physical_residual_count: 0``), yet ``physical_gate_passed``
was still ``False``, because the gate's ``regions_accounted`` invariant keyed on
the *total* residual count.  An SFX residual therefore hard-failed the chapter
with exactly the same force as untranslated dialogue.  The physical ledger must
keep every residual and must still name the class of each one, so that ordinary
story English keeps failing hard while an SFX remnant stays a review item.

**Semantic.**  ``REVIEW`` is currently a single bucket, and every member of it
renders.  That is right for a region whose Portuguese a reader can actually use,
and wrong for the three regions this run shipped under
``source_ocr_suspicious``: that finding fires *only* when the corrupt source
token survives verbatim into the candidate, so by construction the shipped
Portuguese still shows the garbage ("UM RATO DO SLLM", "COLLD E WIELD
HERDARAM").  Flagging such output for review must not also let it count as
Setup-ready output.

Offline by construction: no job, no provider, no network, no chapter.
"""

import _test_bootstrap  # noqa: F401

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

import benchmark_pipeline
import semantic_fidelity


def _sfx_residual(group_id, text):
    """A #84F13 onomatopoeia residual, exactly as the run persisted it."""
    return {
        "id": group_id,
        "classification": "speech",
        "region_type": "speech",
        "raw_text": text,
        "clean_text": text,
        "confidence": 0.53,
        "manual_review_required": True,
        "preserved_original": True,
        "translation": "",
        "translation_valid": False,
        "translation_final_state": "manual_review",
        "translation_final_reason": "untranslated_source_after_retries",
        "redrawn": False,
        "bounding_box": [10, 10, 40, 20],
    }


def _story_residual(group_id, text):
    """Ordinary dialogue that stayed English - the defect that must keep failing."""
    item = _sfx_residual(group_id, text)
    item["classification"] = "narration"
    item["region_type"] = "narration"
    return item


def _page(index, items):
    return {
        "index": index,
        "status": "completed",
        "debug_data": {"items": items},
    }


class SfxPhysicalGateAccounting(unittest.TestCase):
    """#84F14-1/2/3 - the physical gate must know what class each residual is."""

    def test_sfx_only_residual_does_not_hard_fail_the_physical_gate(self):
        report = benchmark_pipeline._physical_residual_accounting(
            [
                _page(11, [_sfx_residual("BALAO_1", "TAK"),
                           _sfx_residual("BALAO_2", "TUR")]),
                _page(13, [_sfx_residual("BALAO_3", "TRNDGE")]),
            ]
        )
        # Still fully accounted for physically: nothing is hidden.
        self.assertEqual(report["physical_source_residual_count"], 3)
        self.assertEqual(report["ordinary_story_physical_residual_count"], 0)
        # ...but an onomatopoeia remnant is not an ordinary-story product blocker.
        self.assertTrue(report["physical_gate_passed"])
        self.assertEqual(report["physical_decision"], "pass")

    def test_residual_classes_are_named_not_implied(self):
        report = benchmark_pipeline._physical_residual_accounting(
            [_page(11, [_sfx_residual("BALAO_1", "TAK")])]
        )
        classes = report["physical_residual_classes"]
        self.assertEqual(classes["sfx_effect"], 1)
        self.assertEqual(classes["ordinary_story"], 0)
        self.assertEqual(
            report["physical_residual_class_ids"]["sfx_effect"], ["p011:BALAO_1"]
        )

    def test_ordinary_story_english_still_hard_fails(self):
        report = benchmark_pipeline._physical_residual_accounting(
            [
                _page(11, [_sfx_residual("BALAO_1", "TAK")]),
                _page(20, [_story_residual(
                    "BALAO_1",
                    "THE NIGHTMARE SPELL TOOK EVERYTHING FROM THEM.",
                )]),
            ]
        )
        self.assertEqual(report["ordinary_story_physical_residual_count"], 1)
        self.assertEqual(
            report["physical_residual_classes"]["ordinary_story"], 1
        )
        self.assertFalse(report["physical_gate_passed"])
        self.assertEqual(report["physical_decision"], "review")


class ReviewUsabilityModel(unittest.TestCase):
    """#84F14-5/6/10 - ``review`` is two different verdicts, not one."""

    def test_review_reasons_are_split_into_renderable_and_unusable(self):
        # By construction ``source_ocr_suspicious`` means the corrupt source
        # token is visible in the shipped Portuguese: "UM RATO DO SLLM".
        self.assertEqual(
            semantic_fidelity.review_usability(
                semantic_fidelity.SOURCE_OCR_SUSPICIOUS + ":SLLM"
            ),
            semantic_fidelity.REVIEW_UNUSABLE,
        )
        self.assertEqual(
            semantic_fidelity.review_usability(
                semantic_fidelity.GRAMMAR_MALFORMED + ":de de"
            ),
            semantic_fidelity.REVIEW_UNUSABLE,
        )
        # A review a reader can still use stays renderable.
        self.assertEqual(
            semantic_fidelity.review_usability("translation_review_required"),
            semantic_fidelity.REVIEW_RENDERABLE,
        )

    def test_sllm_class_output_is_review_unusable_not_merely_review(self):
        finding = semantic_fidelity.evaluate_local_fidelity(
            "THAT HAS NOTHING TO DO WITH A SLLM RAT LIKE ME.",
            "ISSO NAO TEM NADA A VER COM UM RATO DO SLLM COMO EU.",
            classification="narration",
            is_source_word=lambda token: token in {"THAT", "HAS", "NOTHING", "RAT"},
        )
        self.assertEqual(finding.status, semantic_fidelity.REVIEW)
        self.assertEqual(
            semantic_fidelity.review_usability(finding.reason()),
            semantic_fidelity.REVIEW_UNUSABLE,
        )

    def test_source_whose_word_boundaries_were_never_recovered_is_unusable(self):
        """#84F14-9 - p065:BALAO_2, ``AGATETHROUGHWHICH`` -> "UMA GATA".

        The segmenter ran (``repair_reason``) and still handed the provider runs
        it could not split.  Word boundaries the pipeline itself failed to
        recover are not a source any candidate can be verified against, so the
        output renders under review but is never counted as usable.
        """
        finding = semantic_fidelity.evaluate_local_fidelity(
            "..YOU BE COME AGATETHROUGHWHICH AMONSTERAPPEARSIN THEREALWORLD.",
            "...VOCE SE TORNA UMA GATA ATRAVES DA QUAL UM MONSTRO APARECE NO MUNDO REAL.",
            classification="speech",
            source_repair_reason="segment_compact_english_word",
        )
        self.assertEqual(finding.status, semantic_fidelity.REVIEW)
        self.assertEqual(
            finding.primary_reason, semantic_fidelity.SOURCE_SEGMENTATION_INCOMPLETE
        )
        self.assertEqual(
            semantic_fidelity.review_usability(finding.reason()),
            semantic_fidelity.REVIEW_UNUSABLE,
        )

    def test_recovered_source_is_not_flagged(self):
        """The same repair that finished its job proves nothing is wrong."""
        finding = semantic_fidelity.evaluate_local_fidelity(
            "...AND HAD BE COME AWAKENED.",
            "...E TINHA SE TORNADO UM DESPERTADO.",
            classification="speech",
            source_repair_reason="segment_compact_english_word",
        )
        self.assertTrue(finding.faithful)


class SetupReadinessAccounting(unittest.TestCase):
    """#84F14-11 - a nonsensical review output cannot count as Setup-ready."""

    def _accounting(self, items):
        return benchmark_pipeline._user_visible_output_accounting(
            [_page(43, items)]
        )

    def test_review_unusable_output_is_a_setup_blocker(self):
        report = self._accounting([
            {
                "id": "BALAO_1",
                "classification": "narration",
                "clean_text": "THAT HAS NOTHING TO DO WITH A SLLM RAT LIKE ME.",
                "translation": "ISSO NAO TEM NADA A VER COM UM RATO DO SLLM COMO EU.",
                "translation_valid": True,
                "translation_final_state": "translated",
                "translation_final_reason": "ok",
                "semantic_review_reason": "source_ocr_suspicious:SLLM",
                "render_disposition": "render_with_review",
                "redrawn": True,
            }
        ])
        self.assertEqual(report["semantic_review_unusable"], 1)
        self.assertEqual(report["semantic_review_renderable"], 0)
        self.assertFalse(report["setup_ready"])
        self.assertIn("p043:BALAO_1", report["semantic_review_unusable_ids"])

    def test_clean_story_output_is_setup_ready(self):
        report = self._accounting([
            {
                "id": "BALAO_1",
                "classification": "speech",
                "clean_text": "BUT IF YOU DIE...",
                "translation": "MAS SE VOCE MORRER...",
                "translation_valid": True,
                "translation_final_state": "translated",
                "translation_final_reason": "ok",
                "render_disposition": "render_clean",
                "redrawn": True,
            }
        ])
        self.assertEqual(report["semantic_clean"], 1)
        self.assertEqual(report["semantic_review_unusable"], 0)
        self.assertEqual(report["ordinary_story_english_visible"], 0)
        self.assertEqual(report["missing_ptbr"], 0)
        self.assertTrue(report["setup_ready"])

    def test_renderable_review_may_remain(self):
        report = self._accounting([
            {
                "id": "BALAO_1",
                "classification": "speech",
                "clean_text": "BUT IF YOU DIE...",
                "translation": "MAS SE VOCE MORRER...",
                "translation_valid": True,
                "translation_final_state": "manual_review",
                "translation_final_reason": "translation_review_required",
                "render_disposition": "render_with_review",
                "redrawn": True,
            }
        ])
        self.assertEqual(report["semantic_review_renderable"], 1)
        self.assertEqual(report["semantic_review_unusable"], 0)
        self.assertTrue(report["setup_ready"])

    def test_ordinary_story_english_left_visible_blocks_setup(self):
        report = self._accounting([_story_residual("BALAO_9", "WHAT DID YOU DO TO HIM?")])
        self.assertEqual(report["ordinary_story_english_visible"], 1)
        self.assertFalse(report["setup_ready"])


if __name__ == "__main__":
    unittest.main()
