"""Regression: an empty quality retry must not discard a valid PT-BR translation.

Real artifact this pins down (Absolute Regression - Episode 104): six speech
balloons had a correct first-pass PT-BR translation, but a quality retry returned
an empty candidate.  The empty result used to overwrite the first-pass reason with
``empty_translation``, which is not a quality-only reason, so the finalize rescue
refused to ship the Portuguese and the English source was rendered instead.

The fix: an empty (or errored) retry casts no verdict, so it keeps the first-pass
reason.  A quality-only hold then still ships its Portuguese under review rather
than leaving English on the page; a genuinely empty translation (no prior PT-BR)
still preserves the source.  Everything runs against a scripted translator.
"""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import unittest
from unittest import mock

import numpy as np

import config
import ocr_balloon
from ocr_balloon import OCRLine, TextGroup, validate_and_retry_translations


ENGLISH = "ON THE CONTRARY, SEE HOW FAR YOU'VE REACHED ALREADY."
PTBR = "PELO CONTRÁRIO, VEJA O QUÃO LONGE VOCÊ JÁ CHEGOU."


def _line(text):
    polygon = np.array([[10, 10], [210, 10], [210, 45], [10, 45]], dtype=np.int32)
    return OCRLine(text=text, confidence=0.95, polygon=polygon, box=(10, 10, 200, 35),
                   raw_text=text, engine="rapidocr", page=1)


def _group(text, translation, *, classification="speech", group_id="R001"):
    group = TextGroup(group_id=group_id, lines=[_line(text)], text=text,
                      classification=classification, inside_balloon_like_region=True,
                      source_engine="rapidocr")
    group.translation = translation
    group.translation_candidate = translation
    group.sent_to_translation = True
    return group


class _EmptyRetryTranslator:
    """translate_strict always returns an empty candidate (the observed failure)."""

    is_configured = True

    def __init__(self):
        self.calls = []

    def translate_strict(self, text, previous_translation="", validation_reason="", **_kw):
        self.calls.append({"text": text, "reason": validation_reason,
                           "previous": previous_translation})
        return ""


def _scoped_validator(quality_only_for):
    """Fail the named PT-BR candidate for a quality-only reason; report a truly
    empty candidate as empty_translation; defer everything else to the real gate."""
    real = ocr_balloon.validate_translation_text

    def fake(source, candidate, classification, allowed, **kwargs):
        text = (candidate or "").strip()
        if not text:
            return False, "empty_translation"
        if candidate == quality_only_for:
            return False, "stray_ocr_fragment"  # a listed quality-only reason
        return real(source, candidate, classification, allowed, **kwargs)

    return fake


class EmptyRetryPrefersTranslation(unittest.TestCase):
    def setUp(self):
        self._cfg = mock.patch.multiple(
            config, TRANSLATION_VALIDATION=True,
            TRANSLATION_RETRY_ON_MIXED_LANGUAGE=True, TRANSLATION_MAX_RETRIES=1)
        self._cfg.start()

    def tearDown(self):
        self._cfg.stop()

    def test_quality_only_hold_ships_ptbr_under_review_not_english(self):
        group = _group(ENGLISH, PTBR)
        translator = _EmptyRetryTranslator()
        with mock.patch.object(ocr_balloon, "validate_translation_text",
                               _scoped_validator(PTBR)):
            records = validate_and_retry_translations([group], translator)
        self.assertEqual(len(translator.calls), 1, "exactly one quality retry")
        self.assertEqual(records[-1]["candidate_translation"], "", "retry came back empty")
        # The Portuguese is rendered (under review), never the English source.
        self.assertEqual(group.translation, PTBR)
        self.assertNotEqual(group.translation, ENGLISH)
        self.assertFalse(group.preserved_original)
        self.assertTrue(group.manual_review_required)

    def test_genuinely_empty_translation_still_preserves_source(self):
        # No prior PT-BR ever existed: the source must stay, not a fabricated render.
        group = _group(ENGLISH, "")
        translator = _EmptyRetryTranslator()
        with mock.patch.object(ocr_balloon, "validate_translation_text",
                               _scoped_validator(PTBR)):
            validate_and_retry_translations([group], translator)
        self.assertEqual(group.translation, "")
        self.assertTrue(group.preserved_original)


if __name__ == "__main__":
    unittest.main()
