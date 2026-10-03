"""TDD #84F39 - ordinary story English residual must never ship silently.

Forensic trace on the post-#84F38 HTTP-discovery run found four distinct
mechanisms that can leave an ordinary story region (speech/thought/narration)
in English in the final render without leaving any trace a human or the
review gate would see:

1. classification-stage drop of a weak ``unknown`` label had no review
   promotion symmetric to the existing ``decorative_text`` one
   (``_ignored_decorative_requires_review``);
2. the render-time source-echo shortcut treated *any* candidate==source text
   as a clean, no-review "nothing to translate" case, even a full ordinary
   sentence, instead of only a lone name/token;
3/4. the chapter-quality-revision audit and the selective-retry group
   reconstruction both unconditionally skipped every ``ignored`` region, so a
   region flagged ``manual_review_required`` for exactly this reason still
   vanished from the gate that decides whether the job may finish.

These tests are offline and read-only against production functions; no
provider, network, Drive, or Vortex fixture is touched, and no phrase/page is
hardcoded in the functions under test - the fixtures below are just examples.
"""
from __future__ import annotations

import _test_bootstrap  # noqa: F401
from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

import chapter_quality_revision as cqr
from ocr_balloon import (
    TextGroup,
    _echo_is_name_shaped,
    _ignored_decorative_requires_review,
    _score_group_quality,
    group_has_name_only_shape,
)
from ocr_engine import OCRLine


def _line(text, box=(10, 10, 400, 60)):
    x, y, w, h = box
    return OCRLine(text=text, confidence=0.9, polygon=None, box=box, raw_text=text, engine="rapidocr", page=1)


def _weak_unknown_group(text):
    group = TextGroup(group_id="BALAO_1", lines=[_line(text)], text=text, classification="unknown")
    group.ignored = True
    group.ignore_reason = "weak_unknown_text"
    return group


class WeakUnknownReviewPromotionTests(unittest.TestCase):
    def test_long_punctuated_weak_unknown_text_is_flagged_for_review(self):
        group = _weak_unknown_group(
            "Just go into the dream realm, kill a few monsters, and come back."
        )

        _score_group_quality([group])

        self.assertTrue(group.manual_review_required)

    def test_short_weak_unknown_label_without_sentence_shape_is_not_flagged(self):
        group = _weak_unknown_group("OK")

        _score_group_quality([group])

        self.assertFalse(group.manual_review_required)

    def test_decorative_review_promotion_is_unaffected(self):
        group = TextGroup(
            group_id="BALAO_2",
            lines=[_line("Check your attributes and aspect, adventurer.")],
            text="Check your attributes and aspect, adventurer.",
            classification="decorative",
        )
        group.ignored = True
        group.ignore_reason = "decorative_text"

        _score_group_quality([group])

        self.assertTrue(group.manual_review_required)

    def test_proven_preserve_reasons_are_never_promoted(self):
        group = TextGroup(group_id="SFX_1", lines=[_line("THUNK")], text="THUNK", classification="sfx")
        group.ignored = True
        group.ignore_reason = "sfx_translation_disabled"

        _score_group_quality([group])

        self.assertFalse(group.manual_review_required)


class EchoNameShapeTests(unittest.TestCase):
    def test_lone_brand_token_echo_is_name_shaped(self):
        group = TextGroup(
            group_id="LOGO_1",
            lines=[_line("Vortexscans")],
            text="Vortexscans",
            classification="unknown",
        )
        group.translation = "Vortexscans"

        self.assertTrue(group_has_name_only_shape(group))
        self.assertTrue(_echo_is_name_shaped(group))

    def test_ordinary_full_sentence_echo_is_not_name_shaped(self):
        group = TextGroup(
            group_id="BALAO_3",
            lines=[_line("Not the cheap synthetic stuff I'm used to getting in the slums.")],
            text="Not the cheap synthetic stuff I'm used to getting in the slums.",
            classification="speech",
        )
        group.translation = "Not the cheap synthetic stuff I'm used to getting in the slums."

        self.assertFalse(_echo_is_name_shaped(group))


class QualityRevisionIgnoredRegionGateTests(unittest.TestCase):
    def _page(self, items):
        return {"page_number": 1, "debug_data": {"items": items}}

    def _engine(self):
        return object.__new__(cqr.ChapterQualityRevision)

    def test_ignored_region_flagged_for_review_is_not_dropped_from_audit(self):
        page = self._page([
            {
                "id": "BALAO_1",
                "region_id": "REGION_001",
                "classification": "unknown",
                "text": "Just go into the dream realm, kill a few monsters.",
                "translation": "",
                "ignored": True,
                "manual_review_required": True,
                "bounding_box": [10, 10, 400, 60],
            }
        ])

        regions = self._engine()._collect_regions([page])

        self.assertEqual(len(regions), 1)
        self.assertTrue(regions[0]["manual_review_required"])

    def test_ignored_region_with_no_review_flag_stays_excluded(self):
        page = self._page([
            {
                "id": "SFX_1",
                "region_id": "REGION_002",
                "classification": "sfx",
                "text": "THUNK",
                "translation": "",
                "ignored": True,
                "manual_review_required": False,
                "bounding_box": [10, 10, 90, 60],
            }
        ])

        regions = self._engine()._collect_regions([page])

        self.assertEqual(regions, [])

    def test_review_flagged_ignored_region_is_reconstructed_for_selective_retry(self):
        page = self._page([
            {
                "id": "BALAO_1",
                "region_id": "REGION_001",
                "classification": "unknown",
                "text": "Just go into the dream realm, kill a few monsters.",
                "translation": "",
                "ignored": True,
                "manual_review_required": True,
                "bounding_box": [10, 10, 400, 60],
            }
        ])

        groups = self._engine()._groups_from_page_items(page)

        self.assertEqual(len(groups), 1)
        # Reconstructed groups default to ``ignored=False`` so selective retry
        # re-runs classification/translation instead of silently re-suppressing.
        self.assertFalse(groups[0].ignored)


if __name__ == "__main__":
    unittest.main()
