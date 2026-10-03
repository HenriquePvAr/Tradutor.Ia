"""TDD #84F25 - production source recovery parity and RapidOCR policy.

These tests are deliberately offline.  They prove the production validation
path can use the same local RapidOCR source recovery evidence as #84F23, and
that the Beta default does not escalate suspicious RapidOCR regions to Paddle.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest
from unittest import mock

import numpy as np

import config
import ocr_balloon as ob
import semantic_fidelity
from ocr_engine import OCRLine


P65_RAW = "..YOU BE COME AGATETHROUGHWHICH AMONSTERAPPEARSIN THEREALWORLD."
P65_CANONICAL = "YOU BECOME A GATE THROUGH WHICH A MONSTER APPEARS IN THE REAL WORLD."
P65_BAD_PTBR = "...VOCÊ SE TORNA A AGATA ATRAVÉS DA QUAL UM MONSTRO APARECE NO MUNDO REAL."
P65_GOOD_PTBR = (
    "...VOCÊ SE TORNA UM PORTAL ATRAVÉS DO QUAL UM MONSTRO APARECE NO MUNDO REAL."
)


def _line(text=P65_RAW, box=(128, 2533, 487, 208), *, confidence=0.98):
    x, y, w, h = box
    return OCRLine(
        text=text,
        raw_text=text,
        confidence=confidence,
        polygon=np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]]),
        box=box,
        engine="rapidocr",
        page=65,
    )


def _p65_group():
    group = ob.TextGroup(group_id="BALAO_2", lines=[_line()])
    group.text = P65_RAW
    group.translation = P65_BAD_PTBR
    group.translation_candidate = P65_BAD_PTBR
    group.sent_to_translation = True
    group.classification = "speech"
    group.region_id = "REGION_002"
    group.source_engine = "rapidocr"
    group.quality_score = 0.74
    group.quality_reasons = ["unknown_short_token_in_phrase"]
    return group


class _CapturingTranslator:
    def __init__(self, answer=P65_GOOD_PTBR):
        self.answer = answer
        self.calls = []

    def translate_strict(self, text, **kwargs):
        self.calls.append({"text": text, **kwargs})
        return self.answer


class ProductionSourceRecoveryParityTests(unittest.TestCase):
    def test_production_retry_invokes_source_recovery_before_translation(self):
        group = _p65_group()
        translator = _CapturingTranslator()
        source_image = np.full((2910, 800, 3), 255, dtype=np.uint8)
        seen_validation_sources = []

        def fake_validate(source, candidate, *_args, **_kwargs):
            seen_validation_sources.append(source)
            if source == P65_RAW:
                return (
                    False,
                    f"{semantic_fidelity.SOURCE_SEGMENTATION_INCOMPLETE}:AGATETHROUGHWHICH",
                )
            if source == P65_CANONICAL and candidate == P65_GOOD_PTBR:
                return True, "ok"
            return False, "unexpected_source_or_candidate"

        def fake_recover(original_bgr, recovered_group, ocr_lang, **kwargs):
            self.assertIs(original_bgr, source_image)
            self.assertIs(recovered_group, group)
            self.assertEqual(ocr_lang, "eng")
            self.assertEqual(kwargs["page_index"], 65)
            record = {
                "trusted": True,
                "engine": "rapidocr",
                "canonical_source": P65_CANONICAL,
                "agreement_count": 2,
                "average_confidence": 0.9712,
                "target_used_as_source": False,
                "attempts": [
                    {"variant": "grayscale_autocontrast", "text": P65_CANONICAL},
                    {"variant": "threshold_otsu", "text": P65_CANONICAL},
                ],
            }
            recovered_group.canonical_source_text = P65_CANONICAL
            recovered_group.source_recovery = dict(record)
            return record

        with (
            mock.patch.object(config, "TRANSLATION_VALIDATION", True),
            mock.patch.object(config, "TRANSLATION_RETRY_ON_MIXED_LANGUAGE", True),
            mock.patch.object(config, "TRANSLATION_MAX_RETRIES", 1),
            mock.patch.object(ob, "validate_translation_text", side_effect=fake_validate),
            mock.patch.object(ob, "_fidelity_reason_for", return_value=""),
            mock.patch.object(
                ob,
                "recover_source_with_rapidocr_variants",
                side_effect=fake_recover,
            ) as recover,
        ):
            records = ob.validate_and_retry_translations(
                [group],
                translator,
                source_recovery_context={
                    id(group): {
                        "original_bgr": source_image,
                        "ocr_lang": "eng",
                        "page_index": 65,
                    }
                },
            )

        recover.assert_called_once()
        self.assertEqual(translator.calls[0]["text"], P65_CANONICAL)
        self.assertEqual(seen_validation_sources[0], P65_RAW)
        self.assertEqual(seen_validation_sources[-1], P65_CANONICAL)
        self.assertEqual(records[0]["source"], P65_CANONICAL)
        self.assertEqual(records[0]["raw_source"], P65_RAW)
        self.assertEqual(records[0]["source_recovery"]["engine"], "rapidocr")
        self.assertFalse(records[0]["source_recovery"]["target_used_as_source"])
        self.assertEqual(group.translation, P65_GOOD_PTBR)
        self.assertEqual(group.translation_final_state, "translated")

    def test_target_translation_never_influences_recovered_source(self):
        image = np.full((2910, 800, 3), 255, dtype=np.uint8)
        outputs = []

        class StubEngine:
            def __init__(self):
                self.calls = 0

            def detect_lines(self, _image, **_kwargs):
                self.calls += 1
                return [_line(P65_CANONICAL, confidence=0.92)]

        for target in (P65_BAD_PTBR, "ALVO TOTALMENTE DIFERENTE."):
            group = _p65_group()
            group.translation_candidate = target
            record = ob.recover_source_with_rapidocr_variants(
                image,
                group,
                "eng",
                page_index=65,
                engine=StubEngine(),
                max_variants=3,
            )
            outputs.append(record["canonical_source"])
            self.assertFalse(record["target_used_as_source"])

        self.assertEqual(outputs, [P65_CANONICAL, P65_CANONICAL])


class RapidOCRFallbackPolicyTests(unittest.TestCase):
    def test_beta_default_does_not_call_paddle_for_suspicious_rapidocr_region(self):
        image = np.full((320, 640, 3), 255, dtype=np.uint8)
        group = ob.TextGroup(group_id="BALAO_1", lines=[_line("CAN TOO!", (80, 80, 220, 60))])
        group.text = "CAN TOO!"
        group.classification = "speech"
        group.source_engine = "rapidocr"
        group.quality_score = 0.16
        group.quality_reasons = ["unknown_short_token_in_phrase"]

        with (
            mock.patch.object(config, "OCR_QUALITY_CONTROL", True),
            mock.patch.object(config, "OCR_REGION_SELECTIVE_FALLBACK", True),
            mock.patch.object(config, "OCR_ENGINE", "rapidocr"),
            mock.patch.object(config, "OCR_HYBRID_FALLBACK", True),
            mock.patch.object(config, "OCR_FALLBACK_ENGINE", "paddle"),
            mock.patch.object(config, "OCR_LEGACY_PADDLE_FALLBACK", False, create=True),
            mock.patch("ocr_balloon.OCREngine") as engine_cls,
        ):
            _lines, records = ob.apply_selective_ocr_fallbacks(
                image,
                group.lines,
                [group],
                "eng",
                65,
            )

        self.assertEqual(records, [])
        engine_cls.assert_not_called()

    def test_legacy_paddle_fallback_requires_explicit_opt_in(self):
        image = np.full((320, 640, 3), 255, dtype=np.uint8)
        group = ob.TextGroup(group_id="BALAO_1", lines=[_line("CAN TOO!", (80, 80, 220, 60))])
        group.text = "CAN TOO!"
        group.classification = "speech"
        group.source_engine = "rapidocr"
        group.quality_score = 0.16
        group.quality_reasons = ["unknown_short_token_in_phrase"]
        fallback_line = _line("I CAN TOO!", (80, 80, 220, 60))

        with (
            mock.patch.object(config, "OCR_QUALITY_CONTROL", True),
            mock.patch.object(config, "OCR_REGION_SELECTIVE_FALLBACK", True),
            mock.patch.object(config, "OCR_ENGINE", "rapidocr"),
            mock.patch.object(config, "OCR_HYBRID_FALLBACK", True),
            mock.patch.object(config, "OCR_FALLBACK_ENGINE", "paddle"),
            mock.patch.object(config, "OCR_LEGACY_PADDLE_FALLBACK", True, create=True),
            mock.patch("ocr_balloon.OCREngine") as engine_cls,
            mock.patch(
                "ocr_balloon._candidate_groups_for_fallback",
                return_value=[
                    ob.TextGroup(
                        group_id="candidate",
                        lines=[fallback_line],
                        text="I CAN TOO!",
                        classification="speech",
                        source_engine="paddle_mobile",
                    )
                ],
            ),
        ):
            engine_cls.return_value.detect_lines.side_effect = [[fallback_line]]
            _lines, records = ob.apply_selective_ocr_fallbacks(
                image,
                group.lines,
                [group],
                "eng",
                65,
            )

        self.assertTrue(records)
        engine_cls.assert_called()


if __name__ == "__main__":
    unittest.main()
