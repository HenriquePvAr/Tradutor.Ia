"""RapidOCR quality gate + selective single-region RapidOCR recovery.

Every OCR engine here is a stub. No RapidOCR/Paddle inference, no network, no
real chapter: the fixtures are synthetic tokens chosen only to exercise the
policy branches.
"""

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest
from unittest.mock import patch

import cv2
import numpy as np

import config
import ocr_balloon
from ocr_balloon import (
    TextGroup,
    _should_translate_group,
    analyze_image_array,
    apply_rapidocr_region_recovery,
    enforce_rapidocr_quality_gate,
    get_translatable_groups,
    rapidocr_region_decision,
    score_group_ocr_quality,
)
from ocr_engine import OCRLine


def _line(text, box, confidence=0.92):
    x, y, width, height = box
    polygon = np.array(
        [[x, y], [x + width, y], [x + width, y + height], [x, y + height]],
        dtype=np.int32,
    )
    return OCRLine(
        text=text,
        confidence=confidence,
        polygon=polygon,
        box=box,
        raw_text=text,
        engine="rapidocr",
        page=1,
    )


def _scored_group(text, box=(160, 130, 110, 34), confidence=0.92, group_id="T001"):
    line = _line(text, box, confidence=confidence)
    group = TextGroup(
        group_id=group_id,
        lines=[line],
        text=text,
        classification="speech",
        inside_balloon_like_region=True,
        source_engine="rapidocr",
    )
    group.quality_score, group.quality_reasons = score_group_ocr_quality(group)
    return group


def _balloon_page(*boxes):
    image = np.zeros((520, 640, 3), dtype=np.uint8)
    for x, y, width, height in boxes:
        cv2.ellipse(
            image,
            (x + width // 2, y + height // 2),
            (width, height * 2),
            0,
            0,
            360,
            (255, 255, 255),
            -1,
        )
    return image


class _StubRapidOCR:
    """Stands in for ``ocr_balloon.OCREngine``; records every crop it is asked for."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.engines = []

    def __call__(self, lang, engine=None, fallback_engine=None):
        self.engines.append((engine, fallback_engine))
        return self

    def detect_lines(self, crop, page=None, **kwargs):
        self.calls.append((crop.shape, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, tuple):
            # ``(text, confidence)`` is reported centred in the crop it was
            # given, which is where a real re-read of the region would land.
            text, confidence = result
            height, width = crop.shape[:2]
            box = (width // 4, height // 2 - 17, max(20, width // 2), 34)
            return [_line(text, box, confidence)]
        return result


def _recovery_config():
    return (
        patch.object(config, "OCR_ENGINE", "rapidocr"),
        patch.object(config, "RAPIDOCR_REGION_RECOVERY", True),
    )


def _run_recovery(image, lines, groups, stub):
    enter = _recovery_config()
    with enter[0], enter[1], patch.object(ocr_balloon, "OCREngine", stub):
        return apply_rapidocr_region_recovery(image, lines, groups, "eng", 1)


class RapidOCRQualityDecisionTests(unittest.TestCase):
    def setUp(self):
        for patcher in _recovery_config():
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_corrupted_token_requests_retry(self):
        group = _scored_group("iK3H DON'T SAY THAT!")
        self.assertIn("alphanumeric_ocr_artifact", group.quality_reasons)
        self.assertEqual(rapidocr_region_decision(group), "retry")

    def test_plain_alphanumeric_rank_text_is_accepted(self):
        for text in ("S-RANK", "B2", "LEVEL 10", "HP 50"):
            with self.subTest(text=text):
                group = _scored_group(text)
                self.assertEqual(rapidocr_region_decision(group), "accept")

    def test_short_text_is_accepted(self):
        for text in ("HEY!", "NO!", "WHAT?", "?!", "...", "HA!"):
            with self.subTest(text=text):
                group = _scored_group(text)
                self.assertEqual(rapidocr_region_decision(group), "accept")

    def test_warning_only_low_score_dialogue_is_accepted_for_translation(self):
        examples = [
            "WHAT ARE YOU DOING?",
            "FOR ME, IT'S BECAUSE OUR GUILDMASTER IS STRONG.",
        ]
        for text in examples:
            with self.subTest(text=text):
                group = _scored_group(text)
                self.assertLess(group.quality_score, config.RAPIDOCR_RECOVERY_MIN_QUALITY_SCORE)
                self.assertEqual(rapidocr_region_decision(group), "accept")
                with patch.object(ocr_balloon, "record_count") as counter:
                    enforce_rapidocr_quality_gate([group])
                self.assertTrue(_should_translate_group(group))
                counted = {call.args[0] for call in counter.call_args_list}
                self.assertIn("ocr_quality_warning_accept", counted)
                self.assertNotIn("ocr_quality_blocked", counted)

    def test_pure_dictionary_near_miss_is_warning_not_retry(self):
        group = _scored_group("KAELRIN, WAIT!")
        group.quality_score = 0.2
        group.quality_reasons = ["dictionary_near_miss"]
        self.assertEqual(rapidocr_region_decision(group), "accept")

    def test_legitimate_contractions_are_warning_accepted(self):
        for text in ("YOU'RE READY?", "THEY'RE GOING NOW.", "I'D LIKE TO GO."):
            with self.subTest(text=text):
                group = _scored_group(text)
                self.assertEqual(rapidocr_region_decision(group), "accept")

    def test_stutter_is_warning_accepted(self):
        group = _scored_group("TH-THANK YOU!")
        self.assertEqual(rapidocr_region_decision(group), "accept")

    def test_word_spacing_corruption_requests_retry(self):
        group = _scored_group("AN OT HER ONE ABOVE US!!")
        group.quality_score = 0.2
        group.quality_reasons = [
            "compact_word_segmentation_candidate",
            "unknown_short_token_in_phrase",
        ]
        self.assertEqual(rapidocr_region_decision(group), "retry")

    def test_independent_corroborating_artifact_reasons_request_retry(self):
        group = _scored_group("FOR ME IT IS SAFE")
        group.quality_score = 0.2
        group.quality_reasons = [
            "unknown_short_token_in_phrase",
            "low_confidence",
        ]
        self.assertEqual(rapidocr_region_decision(group), "retry")

    def test_malformed_contraction_still_requests_retry(self):
        group = _scored_group("THEY'REDOING THIS.")
        self.assertIn("improbable_apostrophe_pattern", group.quality_reasons)
        self.assertEqual(rapidocr_region_decision(group), "retry")

    def test_proper_name_is_accepted(self):
        group = _scored_group("KAELRIN, WAIT!")
        group.detected_proper_names = ["KAELRIN"]
        group.quality_score, group.quality_reasons = score_group_ocr_quality(group)
        self.assertEqual(rapidocr_region_decision(group), "accept")

    def test_non_rapidocr_regions_are_left_alone(self):
        group = _scored_group("iK3H DON'T SAY THAT!")
        group.source_engine = "paddle"
        self.assertEqual(rapidocr_region_decision(group), "accept")


class SelectiveRecoveryTests(unittest.TestCase):
    def test_good_primary_never_calls_ocr_again(self):
        group = _scored_group("HEY! WAIT FOR ME!")
        stub = _StubRapidOCR([])
        image = _balloon_page(group.box)
        lines, records = _run_recovery(image, list(group.lines), [group], stub)
        self.assertEqual(stub.calls, [])
        self.assertEqual(records, [])
        self.assertEqual(lines, list(group.lines))

    def test_bad_primary_retries_once_and_selects_attempt_two(self):
        group = _scored_group("iK3H DON'T SAY THINGS LIKE THAT!")
        image = _balloon_page(group.box)
        stub = _StubRapidOCR([("DON'T SAY THINGS LIKE THAT!", 0.95)])
        lines, records = _run_recovery(image, list(group.lines), [group], stub)
        self.assertEqual(len(stub.calls), 1, "suspicious region must cost exactly one retry")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["selection"], "attempt_2")
        texts = {line.text for line in lines}
        self.assertIn("DON'T SAY THINGS LIKE THAT!", texts)
        self.assertNotIn("iK3H DON'T SAY THINGS LIKE THAT!", texts)

    def test_retry_uses_a_different_strategy_than_the_first_read(self):
        group = _scored_group("iK3H DON'T SAY THINGS LIKE THAT!")
        image = _balloon_page(group.box)
        stub = _StubRapidOCR([("DON'T SAY THAT!", 0.95)])
        _, records = _run_recovery(image, list(group.lines), [group], stub)
        self.assertTrue(stub.calls[0][1].get("rapidocr_upscale"))
        strategies = [item["strategy"] for item in records[0]["attempts"]]
        self.assertEqual(len(set(strategies)), 2)

    def test_telemetry_counts_every_outcome(self):
        good = _scored_group("HEY! WAIT FOR ME!", box=(60, 380, 150, 34), group_id="B")
        bad = _scored_group("iK3H DON'T SAY THAT!", box=(60, 60, 150, 34), group_id="A")
        image = _balloon_page(bad.box, good.box)
        stub = _StubRapidOCR([("DON'T SAY THAT!", 0.95)])
        with patch.object(ocr_balloon, "record_count") as counter:
            _run_recovery(image, list(bad.lines) + list(good.lines), [bad, good], stub)
        counted = {call.args[0] for call in counter.call_args_list}
        self.assertIn("rapidocr_recovery.primary_accepted", counted)
        self.assertIn("rapidocr_recovery.retry_attempted", counted)
        self.assertIn("rapidocr_recovery.retry_selected", counted)

    def test_blocked_regions_are_counted(self):
        group = _scored_group("iK3H DON'T SAY THAT!")
        with patch.object(config, "OCR_ENGINE", "rapidocr"), patch.object(
            config, "RAPIDOCR_REGION_RECOVERY", True
        ), patch.object(ocr_balloon, "record_count") as counter:
            enforce_rapidocr_quality_gate([group])
        counted = {call.args[0] for call in counter.call_args_list}
        self.assertIn("rapidocr_recovery.all_attempts_rejected", counted)
        self.assertIn("ocr_quality_blocked", counted)
        self.assertIn("translation_held_by_ocr", counted)

    def test_retry_budget_is_bounded_per_page(self):
        groups = [
            _scored_group("iK3H DON'T SAY THAT!", box=(60, 40 + index * 60, 150, 34), group_id=f"G{index}")
            for index in range(5)
        ]
        image = _balloon_page(*[group.box for group in groups])
        stub = _StubRapidOCR([("iK3H D0N'T", 0.4)] * 5)
        with patch.object(config, "RAPIDOCR_RECOVERY_MAX_REGIONS_PER_PAGE", 2):
            lines, records = _run_recovery(
                image, [line for group in groups for line in group.lines], groups, stub
            )
        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(len(records), 2)

    def test_retry_never_uses_paddle(self):
        group = _scored_group("iK3H DON'T SAY THINGS LIKE THAT!")
        image = _balloon_page(group.box)
        stub = _StubRapidOCR([("DON'T SAY THAT!", 0.95)])
        _run_recovery(image, list(group.lines), [group], stub)
        self.assertTrue(stub.engines)
        for engine, fallback in stub.engines:
            self.assertEqual(engine, "rapidocr")
            self.assertNotIn("paddle", str(fallback))

    def test_retry_reads_only_the_suspicious_region(self):
        bad = _scored_group("iK3H DON'T SAY THINGS LIKE THAT!", box=(60, 60, 150, 34), group_id="A")
        good = _scored_group("HEY! WAIT FOR ME!", box=(60, 380, 150, 34), group_id="B")
        image = _balloon_page(bad.box, good.box)
        stub = _StubRapidOCR([("DON'T SAY THAT!", 0.95)])
        _run_recovery(image, list(bad.lines) + list(good.lines), [bad, good], stub)
        self.assertEqual(len(stub.calls), 1)
        crop_shape = stub.calls[0][0]
        self.assertLess(crop_shape[0], image.shape[0], "retry must not re-read the whole page")

    def test_both_attempts_bad_fails_closed(self):
        group = _scored_group("iK3H DON'T SAY THINGS LIKE THAT!")
        image = _balloon_page(group.box)
        stub = _StubRapidOCR([("iK3H D0N'T SAY TH1NGS", 0.4)])
        lines, records = _run_recovery(image, list(group.lines), [group], stub)
        self.assertEqual(records[0]["selection"], "rejected")
        self.assertEqual(lines, list(group.lines))

    def test_retry_error_fails_closed(self):
        group = _scored_group("iK3H DON'T SAY THINGS LIKE THAT!")
        image = _balloon_page(group.box)
        stub = _StubRapidOCR([RuntimeError("engine exploded")])
        lines, records = _run_recovery(image, list(group.lines), [group], stub)
        self.assertEqual(records[0]["selection"], "rejected")
        self.assertEqual(records[0]["reason"], "rapidocr_retry_error")
        self.assertEqual(lines, list(group.lines))

    def test_retry_empty_result_fails_closed(self):
        group = _scored_group("iK3H DON'T SAY THINGS LIKE THAT!")
        image = _balloon_page(group.box)
        stub = _StubRapidOCR([[]])
        _, records = _run_recovery(image, list(group.lines), [group], stub)
        self.assertEqual(records[0]["selection"], "rejected")

    def test_record_keeps_attempt_provenance(self):
        group = _scored_group("iK3H DON'T SAY THINGS LIKE THAT!")
        image = _balloon_page(group.box)
        stub = _StubRapidOCR([("DON'T SAY THAT!", 0.95)])
        _, records = _run_recovery(image, list(group.lines), [group], stub)
        record = records[0]
        for key in (
            "region_id",
            "attempts",
            "selection",
            "reason",
            "crop_box",
        ):
            self.assertIn(key, record)
        self.assertEqual([item["attempt"] for item in record["attempts"]], [1, 2])
        for attempt in record["attempts"]:
            for key in ("strategy", "raw_text", "normalized_text", "quality_score", "quality_reasons"):
                self.assertIn(key, attempt)

    def test_recovery_is_disabled_for_non_rapidocr_runs(self):
        group = _scored_group("iK3H DON'T SAY THINGS LIKE THAT!")
        image = _balloon_page(group.box)
        stub = _StubRapidOCR([("DON'T SAY THAT!", 0.95)])
        with patch.object(config, "OCR_ENGINE", "paddle"), patch.object(
            ocr_balloon, "OCREngine", stub
        ):
            lines, records = apply_rapidocr_region_recovery(
                image, list(group.lines), [group], "eng", 1
            )
        self.assertEqual(stub.calls, [])
        self.assertEqual(records, [])


class QualityGateTests(unittest.TestCase):
    def test_unrecovered_region_is_blocked_from_translation(self):
        group = _scored_group("iK3H DON'T SAY THINGS LIKE THAT!")
        self.assertTrue(_should_translate_group(group))
        with patch.object(config, "OCR_ENGINE", "rapidocr"), patch.object(
            config, "RAPIDOCR_REGION_RECOVERY", True
        ):
            enforce_rapidocr_quality_gate([group])
        self.assertTrue(group.ocr_quality_blocked)
        self.assertTrue(group.manual_review_required)
        self.assertFalse(_should_translate_group(group))

    def test_clean_region_is_not_blocked(self):
        group = _scored_group("HEY! WAIT FOR ME!")
        with patch.object(config, "OCR_ENGINE", "rapidocr"), patch.object(
            config, "RAPIDOCR_REGION_RECOVERY", True
        ):
            enforce_rapidocr_quality_gate([group])
        self.assertFalse(group.ocr_quality_blocked)
        self.assertTrue(_should_translate_group(group))


class TranslationHandoffTests(unittest.TestCase):
    """Production-shaped ordering: OCR -> recovery -> re-analyze -> gate -> translate."""

    def _orchestrate(self, image, raw_lines, stub):
        _, groups = analyze_image_array(image, raw_lines, page_index=1)
        enter = _recovery_config()
        with enter[0], enter[1], patch.object(ocr_balloon, "OCREngine", stub):
            lines, records = apply_rapidocr_region_recovery(
                image, raw_lines, groups, "eng", 1
            )
            if any(record["selection"] == "attempt_2" for record in records):
                _, groups = analyze_image_array(image, lines, page_index=1)
            enforce_rapidocr_quality_gate(groups)
        return get_translatable_groups(groups), records

    def test_translator_only_receives_the_selected_retry_text(self):
        box = (240, 200, 150, 34)
        image = _balloon_page(box)
        raw_lines = [_line("iK3H DON'T SAY THINGS LIKE THAT!", box, 0.9)]
        stub = _StubRapidOCR([("DON'T SAY THINGS LIKE THAT!", 0.96)])
        targets, records = self._orchestrate(image, raw_lines, stub)
        self.assertEqual(len(stub.calls), 1)
        sent = [group.text for group in targets]
        self.assertTrue(sent, "the recovered region must still be translated")
        for text in sent:
            self.assertNotIn("iK3H", text)

    def test_unrecoverable_region_never_reaches_the_translator(self):
        box = (240, 200, 150, 34)
        image = _balloon_page(box)
        raw_lines = [_line("iK3H DON'T SAY THINGS LIKE THAT!", box, 0.9)]
        stub = _StubRapidOCR([("iK3H D0N'T SAY TH1NGS", 0.4)])
        targets, _ = self._orchestrate(image, raw_lines, stub)
        self.assertEqual(targets, [])


class SuspiciousButTranslatableTests(unittest.TestCase):
    """A damaged read of ordinary English is still worth translating.

    Morphology is taken from real OCR shapes (dropped spaces around a
    contraction, mixed case inside a word), never from a specific page.
    """

    def _multiline_group(self, parts, classification="narration"):
        lines = [
            _line(text, (160, 130 + 40 * index, 240, 34), confidence=confidence)
            for index, (text, confidence) in enumerate(parts)
        ]
        group = TextGroup(
            group_id="T001",
            lines=lines,
            text=" ".join(text for text, _ in parts),
            classification=classification,
            inside_balloon_like_region=True,
            source_engine="rapidocr",
        )
        group.quality_score, group.quality_reasons = score_group_ocr_quality(group)
        return group

    def _gate(self, group):
        with patch.object(config, "OCR_ENGINE", "rapidocr"), patch.object(
            config, "RAPIDOCR_REGION_RECOVERY", True
        ):
            enforce_rapidocr_quality_gate([group])
        return group

    def test_compacted_dialogue_reaches_the_translator_with_warnings_kept(self):
        for parts in (
            [("I'VETURNED INTOABOSS", 0.99), ("MONSTER.", 0.99)],
            [("THEY'REDOING THEIR BEST", 0.98), ("TO HOLD THEMONSTERSOFF..", 0.97)],
            [("I'D LIKE TO TAKE MY TIME", 0.91), ("KillIng you...", 0.90)],
            [("YOU WOULDN'T GET IT", 0.97), ("UNLESS YOu'VE SEEN...", 0.90)],
        ):
            with self.subTest(parts=parts):
                group = self._gate(self._multiline_group(parts))
                self.assertTrue(group.quality_reasons, "warning must still be raised")
                self.assertFalse(group.ocr_quality_blocked)
                self.assertTrue(_should_translate_group(group))
                self.assertTrue(
                    group.quality_evidence.get("ocr_source_suspicious"),
                    "suspicion evidence must survive the routing decision",
                )
                self.assertEqual(
                    group.quality_evidence["ocr_source_quality_reasons"],
                    group.quality_reasons,
                )

    def test_invented_characters_stay_blocked(self):
        for parts in (
            [("iK3H DON'T SAY THINGS LIKE THAT!", 0.90)],
            [("Wh4t d0 y0u m3an, my fr1end?", 0.55)],
            [("### %%% ~~~ ...", 0.40)],
        ):
            with self.subTest(parts=parts):
                group = self._gate(self._multiline_group(parts, "speech"))
                self.assertTrue(group.ocr_quality_blocked)
                self.assertTrue(group.manual_review_required)
                self.assertFalse(_should_translate_group(group))

    def test_single_unknown_token_is_never_recoverable_dialogue(self):
        for text in ("iHon", "...HUNTERHYEON.", "PAEHYEOK...", "Xqzt..."):
            with self.subTest(text=text):
                group = self._multiline_group([(text, 0.76)], "speech")
                group.quality_reasons = ["mixed_case_ocr_artifact"]
                self.assertFalse(ocr_balloon.ocr_suspicious_but_translatable(group))

    def test_sfx_classification_is_never_rescued_by_this_route(self):
        group = self._multiline_group([("THUD, THUD.", 0.95)], "sfx")
        group.quality_reasons = ["mixed_case_ocr_artifact"]
        self.assertFalse(ocr_balloon.ocr_suspicious_but_translatable(group))


if __name__ == "__main__":
    unittest.main()
