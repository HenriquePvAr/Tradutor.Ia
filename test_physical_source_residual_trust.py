"""TDD #38 - the physical source-residual gate must be trustworthy.

Full E2E #9 rendered p002:BALAO_3 in PT-BR while the source word ``CONTROL``
stayed physically visible on top of it, and the physical gate still persisted
``residual_source_tokens: []`` with ``visual_validation_passed: true``.

The gate's only proof of absence was a single post-render OCR pass that never
reported the token: the surviving glyphs were merged into the neighbouring
``OCONTROLEDENOVOEQUASE`` blob, so exact token intersection could not see them.
Negative evidence from one OCR pass is not proof that source pixels are gone.

These tests pin the contract: a physical PASS needs positive removal evidence -
the cleaning mask actually covered the source text the group owns - and an
uncovered, physically unchanged source glyph can never be reported as clean.

Every engine here is a stub.  No RapidOCR/Paddle inference, no network, no
chapter, no provider.
"""

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest
from unittest.mock import patch

import cv2
import numpy as np

import ocr_balloon
from ocr_balloon import (
    OCR_UNINTELLIGIBLE_SOURCE_REASON,
    TextGroup,
    _build_text_mask,
    _post_render_source_text_check,
    reconcile_recovery_lines,
)
from ocr_engine import OCRLine

PASS = "pass"
RESIDUAL = "residual_detected"
REVIEW = "review"

LINE_BOX = (60, 100, 420, 40)
SOURCE_TEXT = "I LOST CONTROL"
TRANSLATION = "EU PERDI O CONTROLE"


def _polygon(box):
    x, y, w, h = box
    return np.array(
        [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32
    )


def _line(text, box, confidence=0.97):
    line = OCRLine(
        text=text,
        confidence=confidence,
        polygon=_polygon(box),
        box=tuple(box),
        raw_text=text,
        engine="rapidocr",
        page=1,
    )
    line.metadata = {}
    return line


class _StubLine:
    def __init__(self, text):
        self.text = text


class _StubEngine:
    """Stands in for the independent post-render RapidOCR pass."""

    observed = ""

    def __init__(self, *_args, **_kwargs):
        pass

    def _detect_with_rapidocr(self, _crop):
        return [_StubLine(_StubEngine.observed)] if _StubEngine.observed else []


def _dark_panel(shape=(320, 640)):
    image = np.full((shape[0], shape[1], 3), 14, dtype=np.uint8)
    rng = np.random.default_rng(38)
    noise = rng.integers(0, 22, size=(shape[0], shape[1], 1), dtype=np.uint8)
    return np.clip(image.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def _write_light_text(image, text, origin, scale=0.9):
    cv2.putText(
        image,
        text,
        origin,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        (243, 243, 243),
        2,
        cv2.LINE_AA,
    )


def _speech_group(lines, text, translation=TRANSLATION):
    group = TextGroup(
        group_id="BALAO_3",
        lines=list(lines),
        text=text,
        classification="speech",
    )
    group.translation = translation
    group.translation_candidate = translation
    group.translation_valid = True
    group.source_engine = "rapidocr"
    group.draw_box = (40, 80, 460, 80)
    group.safe_area = group.draw_box
    return group


def _p002_shaped_case(uncovered_word=True):
    """The real Full #9 shape: light comic speech over dark textured art.

    ``uncovered_word`` reproduces the defect - the cleaning mask covers every
    glyph except the trailing word, exactly as the stale recovery polygon did.
    """

    original = _dark_panel()
    x, y, w, h = LINE_BOX
    _write_light_text(original, SOURCE_TEXT, (x + 6, y + h - 10), scale=0.9)
    group = _speech_group([_line(SOURCE_TEXT, LINE_BOX)], SOURCE_TEXT)

    # "CONTROL" is the trailing word; the stale recovery polygon stopped just
    # before it, so the mask reaches every earlier glyph and none of its own.
    split = x + 140
    cleanup_mask = np.zeros(original.shape[:2], dtype=np.uint8)
    cleanup_mask[y - 2 : y + h + 2, x - 2 : (split if uncovered_word else x + w + 2)] = 255

    rendered = original.copy()
    rendered[cleanup_mask > 0] = (14, 14, 14)
    # The PT-BR is laid out for the region, not for the surviving glyphs: it
    # covers part of them and leaves the rest legible, exactly as on p002.
    _write_light_text(rendered, TRANSLATION, (x + 4, y + h - 12), scale=0.55)
    return original, rendered, group, cleanup_mask


def _check(rendered, group, original=None, cleanup_mask=None, observed=""):
    _StubEngine.observed = observed
    with patch.object(ocr_balloon, "OCREngine", _StubEngine):
        return _post_render_source_text_check(
            rendered,
            group,
            page_index=2,
            original_bgr=original,
            cleanup_mask=cleanup_mask,
        )


class PhysicalResidualTrustTests(unittest.TestCase):
    """21 / 23 / 45 - the exact Full #9 CONTROL false-pass class."""

    def test_uncovered_source_word_invisible_to_post_render_ocr_cannot_pass(self):
        original, rendered, group, mask = _p002_shaped_case()
        # Exactly what Full #9 observed: the merged PT-BR blob, no CONTROL token.
        result = _check(
            rendered,
            group,
            original,
            mask,
            observed="EU PERDI OCONTROLEDENOVO",
        )

        self.assertFalse(
            result["passed"],
            "physical gate passed while an uncovered source word survived",
        )
        self.assertNotEqual(result["physical_decision"], PASS)
        self.assertGreater(result["uncovered_source_text_pixels"], 0)
        self.assertLess(result["source_text_coverage"], 1.0)

    # 22 - when the independent pass does read it back, it is a residual.
    def test_post_render_ocr_reading_the_source_token_is_a_residual(self):
        original, rendered, group, mask = _p002_shaped_case()
        result = _check(
            rendered,
            group,
            original,
            mask,
            observed="EU PERDI O CONTROLE CONTROL",
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["physical_decision"], RESIDUAL)
        self.assertIn("CONTROL", result["residual_source_tokens"])

    # 23 - OCR false negative plus incomplete coverage is review, never pass.
    def test_ocr_false_negative_with_incomplete_coverage_is_review(self):
        original, rendered, group, mask = _p002_shaped_case()
        result = _check(rendered, group, original, mask, observed="EU PERDI O CONTROLE")

        self.assertEqual(result["physical_decision"], REVIEW)
        self.assertFalse(result["passed"])

    # 24 - a genuinely clean render must still pass.  No broad false positives.
    def test_fully_covered_source_with_clean_output_passes(self):
        original, rendered, group, mask = _p002_shaped_case(uncovered_word=False)
        result = _check(rendered, group, original, mask, observed="EU PERDI O CONTROLE")

        self.assertTrue(result["passed"], result.get("reason"))
        self.assertEqual(result["physical_decision"], PASS)
        self.assertEqual(result["uncovered_source_text_pixels"], 0)

    # 17 - a missing original/mask must not turn every OCR miss into a failure.
    def test_absent_removal_evidence_does_not_invent_a_residual(self):
        _original, rendered, group, _mask = _p002_shaped_case(uncovered_word=False)
        result = _check(rendered, group, None, None, observed="EU PERDI O CONTROLE")

        self.assertTrue(result["passed"], result.get("reason"))

    # 42 - one covered line plus one uncovered line is not a global pass.
    def test_one_uncovered_line_fails_the_whole_group(self):
        original = _dark_panel()
        first = (60, 60, 300, 36)
        second = (60, 130, 320, 36)
        _write_light_text(original, "I LOST", (first[0] + 6, first[1] + 26), 0.9)
        _write_light_text(original, "CONTROL", (second[0] + 6, second[1] + 26), 0.9)
        group = _speech_group(
            [_line("I LOST", first), _line("CONTROL", second)],
            "I LOST CONTROL",
        )
        mask = np.zeros(original.shape[:2], dtype=np.uint8)
        mask[first[1] - 2 : first[1] + first[3] + 2, first[0] - 2 : first[0] + first[2] + 2] = 255
        rendered = original.copy()
        rendered[mask > 0] = (14, 14, 14)
        _write_light_text(rendered, "EU PERDI", (first[0] + 4, first[1] + 24), 0.7)

        result = _check(rendered, group, original, mask, observed="EU PERDI")
        self.assertFalse(result["passed"])
        self.assertNotEqual(result["physical_decision"], PASS)

    # 25 - an authoritatively preserved entity is not ordinary source residual.
    def test_preserved_entity_read_back_is_not_ordinary_residual(self):
        original, rendered, group, mask = _p002_shaped_case(uncovered_word=False)
        group.text = "I LOST CONTROL KAEL"
        group.translation = "EU PERDI O CONTROLE KAEL"
        group.detected_proper_names = ["KAEL"]
        result = _check(
            rendered,
            group,
            original,
            mask,
            observed="EU PERDI O CONTROLE KAEL",
        )

        self.assertNotIn("KAEL", result["residual_source_tokens"])
        self.assertIn("KAEL", result["excluded_preserved_tokens"])
        self.assertTrue(result["passed"], result.get("reason"))

    # 26 - unreadable source keeps its manual-review taxonomy.
    def test_unintelligible_source_keeps_its_own_taxonomy(self):
        original, rendered, group, mask = _p002_shaped_case(uncovered_word=False)
        group.translation_final_reason = OCR_UNINTELLIGIBLE_SOURCE_REASON
        result = _check(rendered, group, original, mask, observed="EU PERDI O CONTROLE")

        self.assertTrue(result["checked"])
        self.assertEqual(result["physical_decision"], PASS)

    # 57 - a PASS has to be explainable, not merely asserted.
    def test_pass_reports_the_removal_evidence_behind_it(self):
        original, rendered, group, mask = _p002_shaped_case(uncovered_word=False)
        result = _check(rendered, group, original, mask, observed="EU PERDI O CONTROLE")

        for key in (
            "physical_decision",
            "source_text_coverage",
            "uncovered_source_text_pixels",
            "largest_uncovered_source_component",
            "expected_source_basis",
            "removal_evidence",
        ):
            self.assertIn(key, result)


class RemovalEvidenceGuardTests(unittest.TestCase):
    """38 - a known-incomplete mask is refused without needing any OCR."""

    def test_uncovered_glyph_is_refused_independently_of_the_ocr_stage(self):
        original, rendered, group, mask = _p002_shaped_case()
        removal = ocr_balloon._uncovered_source_text_evidence(
            original, rendered, group, mask
        )

        self.assertTrue(removal["measured"])
        self.assertTrue(ocr_balloon._source_removal_incomplete(group, removal))

    def test_complete_coverage_is_accepted(self):
        original, rendered, group, mask = _p002_shaped_case(uncovered_word=False)
        removal = ocr_balloon._uncovered_source_text_evidence(
            original, rendered, group, mask
        )

        self.assertFalse(ocr_balloon._source_removal_incomplete(group, removal))

    def test_unmeasurable_evidence_never_invents_a_failure(self):
        _original, rendered, group, _mask = _p002_shaped_case()
        removal = ocr_balloon._uncovered_source_text_evidence(
            None, rendered, group, None
        )

        self.assertFalse(removal["measured"])
        self.assertFalse(ocr_balloon._source_removal_incomplete(group, removal))


class NoiseForgivenessPolicyTests(unittest.TestCase):
    """28 / 29 - forgiveness must be evidence-based, never merely permissive."""

    def _case(self, source_text, translation, observed):
        original = _dark_panel()
        x, y, w, h = LINE_BOX
        _write_light_text(original, source_text, (x + 6, y + h - 10), scale=0.8)
        group = _speech_group([_line(source_text, LINE_BOX)], source_text, translation)
        mask = np.zeros(original.shape[:2], dtype=np.uint8)
        mask[y - 2 : y + h + 2, x - 2 : x + w + 2] = 255
        rendered = original.copy()
        rendered[mask > 0] = (14, 14, 14)
        _write_light_text(rendered, translation, (x + 4, y + h - 12), scale=0.66)
        return _check(rendered, group, original, mask, observed=observed)

    def test_translation_token_misread_as_english_may_be_forgiven(self):
        # The real Full #9 case: PT-BR "SÓ" folds to "SO" and the language
        # validator flags it; the expected translation explains it.
        result = self._case(
            "I LOST CONTROL",
            "NAO E SO QUE EU PERDI O CONTROLE",
            "NAO E SO QUE EU PERDI OCONTROLE",
        )
        self.assertTrue(result["passed"], result.get("reason"))
        self.assertIn("SO", result["forgiven_ocr_noise_tokens"])

    def test_expected_source_word_is_never_forgiven_as_noise(self):
        # Same token, opposite provenance: here "SO" is an English word the
        # source owned and the translation never produced.  Shortness, OCR
        # uncertainty and a convincing overall shape must not excuse it.
        result = self._case(
            "SO I LOST CONTROL",
            "ENTAO EU PERDI O CONTROLE",
            "ENTAO SO EU PERDI O CONTROLE",
        )
        self.assertNotIn("SO", result["forgiven_ocr_noise_tokens"])
        self.assertFalse(result["passed"])
        self.assertEqual(result["physical_decision"], RESIDUAL)


class RecoveryGeometryReconciliationTests(unittest.TestCase):
    """32 / 34 / 40 - reconciling a recovery read must reconcile its geometry.

    ``reconcile_recovery_lines`` re-attached the predecessor's lexical tail and
    widened ``line.box``, but left ``line.polygon`` at the narrower recovery
    read.  Every cleaning mask is built from the polygon, so the re-attached
    word owned source geometry that no mask could ever reach.
    """

    def test_reattached_fragment_keeps_its_source_geometry(self):
        predecessor = [_line("THAT I LOSTCONTROL", (260, 2075, 376, 33), 0.96)]
        candidate = [_line("THAT I LOST", (260, 2075, 218, 33), 0.99)]

        _decision, lines, _reason = reconcile_recovery_lines(predecessor, candidate)

        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertIn("CONTROL", line.text.replace(" ", ""))
        px, py, pw, ph = ocr_balloon._box_from_poly(
            np.asarray(line.polygon, dtype=np.int32)
        )
        self.assertEqual((px, py, pw, ph), tuple(line.box))

    def test_cleaning_mask_reaches_the_reattached_word(self):
        predecessor = [_line("THAT I LOSTCONTROL", (260, 2075, 376, 33), 0.96)]
        candidate = [_line("THAT I LOST", (260, 2075, 218, 33), 0.99)]
        _decision, lines, _reason = reconcile_recovery_lines(predecessor, candidate)

        group = _speech_group(lines, "THAT I LOST CONTROL")
        mask = _build_text_mask((2200, 800, 3), [group])
        # The columns the recovery read dropped are exactly where CONTROL lives.
        self.assertGreater(int(np.count_nonzero(mask[2075:2108, 500:636])), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
