from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np
import config

from ocr_balloon import (
    TextCandidate,
    TextGroup,
    _assign_visual_white_regions,
    _classify_background_region,
    _classify_groups,
    _caption_overlay_mask,
    _container_text_gap_boxes,
    _content_shrink_penalty,
    _line_needs_underread_reocr,
    _recovered_text_is_acceptable,
    _underread_candidate_is_better,
    apply_speech_container_reocr,
    _cross_region_resolution_bonus,
    _dark_blotch_artifact_metrics,
    _debug_payload,
    _detached_light_text_components_mask,
    _detached_dark_text_components_mask,
    _enforce_visual_bounds,
    _enclosure_evidence,
    _english_inflection_base,
    _line_belongs_to_group,
    _line_ignore_reason,
    _looks_like_noise,
    _leading_hyphenated_fragment,
    _group_lines,
    _post_render_source_text_check,
    _normalized_ocr_candidate_text,
    _outlined_light_text_mask,
    _uniform_dark_line_text_mask,
    _uniform_container_evidence,
    _uniform_light_line_text_mask,
    _apply_textured_caption_overlay,
    _reclaim_short_lexical_lines,
    _refine_classification_with_background,
    _score_group_quality,
    _should_translate_group,
    _split_groups_at_sentence_boundaries,
    _mask_shape_metrics,
    _white_patch_artifact_metrics,
    _should_skip_paddle_full_for_ignored_decorative,
    apply_selective_ocr_fallbacks,
    apply_group_translations,
    get_translatable_groups,
    render_analyzed_image,
    group_needs_selective_fallback,
    normalize_recurring_compact_names,
    score_group_ocr_quality,
    validate_translation_text,
    validate_and_retry_translations,
)
from benchmark_pipeline import (
    _aggregate_debug_data,
    _build_quality_report,
    _grouping_fallback_reason,
    _incomplete_speech_region_coverage,
    _preserve_selected_regional_ocr,
    _retry_layout_overflow_translations,
    _translation_quality_accounting,
)
from ocr_engine import (
    OCRLine,
    assess_ocr_repair,
    repair_ocr_text,
    segment_compact_english_word,
    suggest_english_word,
)
from translator_nvidia import TranslatorNvidiaBatch


def _line(text, confidence=0.92):
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


def _boxed_line(text, box, confidence=0.92, *, slant=0):
    x, y, width, height = box
    polygon = np.array(
        [
            [x, y + max(0, slant)],
            [x + width, y],
            [x + width, y + height - max(0, slant)],
            [x, y + height],
        ],
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


def _white_balloon_fixture(text):
    image = np.zeros((320, 420, 3), dtype=np.uint8)
    cv2.ellipse(
        image,
        (210, 150),
        (100, 55),
        0,
        0,
        360,
        (255, 255, 255),
        -1,
    )
    line = _boxed_line(text, (165, 132, 90, 36), confidence=0.95)
    _assign_visual_white_regions(image, [line])
    return image, line, TextGroup(group_id="T", lines=[line], text=text)


def _editorial_art_fixture():
    image = np.full((400, 600, 3), (8, 8, 58), dtype=np.uint8)
    for offset in range(0, 600, 35):
        cv2.line(
            image,
            (offset, 300),
            (min(599, offset + 100), 399),
            (12, 12, 115),
            2,
        )
    return image


def _scored_group(text, confidence=0.92):
    group = TextGroup(
        group_id="T001",
        lines=[_line(text, confidence=confidence)],
        text=text,
        classification="speech",
        inside_balloon_like_region=True,
        source_engine="rapidocr",
    )
    score, reasons = score_group_ocr_quality(group)
    group.quality_score = score
    group.quality_reasons = reasons
    return group


def _lexically_corrupted_multiline_group():
    lines = [
        _boxed_line("JUST WHAT", (80, 80, 210, 36), confidence=0.987),
        _boxed_line("ON EARTH IS", (80, 125, 220, 36), confidence=0.985),
        _boxed_line("ENO DNIOD", (80, 170, 205, 36), confidence=0.907),
    ]
    group = TextGroup(
        group_id="T",
        lines=lines,
        text="JUST WHAT ON EARTH IS ENO DNIOD",
        classification="narration",
        inside_narration_box_like_region=True,
        source_engine="rapidocr",
    )
    group.quality_score, group.quality_reasons = score_group_ocr_quality(group)
    return group


def _regional_candidate(text, engine, confidence):
    line = _boxed_line(text, (80, 80, 230, 126), confidence=confidence)
    line.engine = engine
    group = TextGroup(
        group_id=engine,
        lines=[line],
        text=text,
        classification="narration",
        inside_narration_box_like_region=True,
        source_engine=engine,
    )
    group.quality_score, group.quality_reasons = score_group_ocr_quality(group)
    return group


def _run_fake_selective_fallback(original, candidates):
    image = np.full((360, 420, 3), 255, dtype=np.uint8)
    with (
        patch.object(config, "OCR_QUALITY_CONTROL", True),
        patch.object(config, "OCR_REGION_SELECTIVE_FALLBACK", True),
        patch.object(config, "OCR_ENGINE", "rapidocr"),
        patch.object(config, "OCR_HYBRID_FALLBACK", True),
        patch.object(config, "OCR_FALLBACK_ENGINE", "paddle"),
        patch("ocr_balloon.OCREngine") as engine_cls,
        patch(
            "ocr_balloon._candidate_groups_for_fallback",
            side_effect=[[candidate] for candidate in candidates],
        ),
    ):
        engine_cls.return_value.detect_lines.side_effect = [
            candidate.lines for candidate in candidates
        ]
        return apply_selective_ocr_fallbacks(
            image,
            original.lines,
            [original],
            "eng",
            1,
        )


class _IsolatedRetryTranslator:
    """Fake translator that answers the strict retry and the isolated retry
    differently, so the isolated attempt can be observed without any network."""

    def __init__(self, strict="", isolated=""):
        self.strict = strict
        self.isolated = isolated
        self.strict_calls = []
        self.isolated_calls = []

    def translate_strict(
        self,
        text,
        previous_translation="",
        validation_reason="",
        force=False,
        allow_proper_names=True,
        proper_names=None,
    ):
        record = {
            "text": text,
            "previous_translation": previous_translation,
            "validation_reason": validation_reason,
            "proper_names": list(proper_names or []),
        }
        if allow_proper_names:
            self.strict_calls.append(record)
            return self.strict
        self.isolated_calls.append(record)
        return self.isolated


class _StrictRetryTranslator:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def translate_strict(
        self,
        text,
        previous_translation="",
        validation_reason="",
        force=False,
        allow_proper_names=True,
        proper_names=None,
    ):
        self.calls.append(
            {
                "text": text,
                "previous_translation": previous_translation,
                "validation_reason": validation_reason,
                "force": force,
                "proper_names": list(proper_names or []),
            }
        )
        if self.responses:
            return self.responses.pop(0)
        return previous_translation


class OCRQualityRegressionTests(unittest.TestCase):
    def test_page_fallback_preserves_better_regional_ocr_winner(self):
        regional = _line("BUG", confidence=0.96)
        regional.box = (100, 100, 80, 30)
        regional.metadata = {
            "selective_fallback_used": True,
            "fallback_variant": "paddle_mobile",
        }
        full_page = _line("BLUG", confidence=0.98)
        full_page.box = (98, 98, 86, 34)
        unrelated = _line("OTHER TEXT", confidence=0.95)
        unrelated.box = (400, 500, 180, 40)

        merged, preserved = _preserve_selected_regional_ocr(
            [full_page, unrelated],
            [regional],
        )

        self.assertEqual(preserved, 1)
        self.assertEqual([line.text for line in merged], ["BUG", "OTHER TEXT"])

    def test_content_shrink_penalty_scales_with_dropped_content(self):
        # Keeping the content costs nothing.
        self.assertEqual(_content_shrink_penalty(1.0, 5), 0.0)
        # A candidate that drops a whole line of speech must be penalised, even
        # though it sits above the old hard threshold that let it through free.
        heavy = _content_shrink_penalty(0.5833, 6)
        self.assertGreater(heavy, 0.5)
        # A small trim is penalised only lightly.
        light = _content_shrink_penalty(0.8065, 8)
        self.assertGreater(light, 0.0)
        self.assertLess(light, heavy)
        # A single-token context keeps the previous no-penalty behaviour.
        self.assertEqual(_content_shrink_penalty(0.2, 1), 0.0)

    def test_cross_region_original_is_exempt_from_shrink_penalty(self):
        # When the original reading is suspected of merging text across regions,
        # dropping the crossed-over content is the intended correction, so the
        # candidate must not be penalised for shedding it.
        self.assertGreater(_content_shrink_penalty(0.6, 6), 0.0)
        self.assertEqual(
            _content_shrink_penalty(0.6, 6, cross_region_suspected=True), 0.0
        )

    def test_truncating_fallback_candidate_loses_to_complete_one(self):
        # Fixture from a real run: a fallback candidate that dropped a trailing
        # clause scored 0.9392 while the candidate keeping every line scored
        # 0.74. Penalising the dropped content must let the complete one win.
        truncating = 0.9392 - _content_shrink_penalty(0.5833, 6)
        complete = 0.74 - _content_shrink_penalty(1.0, 6)
        self.assertGreater(complete, truncating + 0.03)

    def test_high_quality_trim_still_beats_noisy_complete_candidate(self):
        # Guard: dropping a leading sound effect with a clean, high-quality read
        # must still win over a lower-quality candidate that keeps it, so the
        # penalty does not drag sound effects back into speech.
        trimmed = 1.9085 - _content_shrink_penalty(0.8065, 8)
        noisy_complete = 1.42 - _content_shrink_penalty(1.0, 8)
        self.assertGreater(trimmed, noisy_complete + 0.03)

    # ---- selective re-OCR of speech containers ----

    def test_wide_box_with_one_glyph_is_flagged_for_reocr(self):
        # The engine was sure of the glyph it returned, yet the box is far too
        # wide for it: the rest of the line was never read.
        line = _boxed_line("H", (199, 336, 93, 44), confidence=0.92)
        self.assertTrue(_line_needs_underread_reocr(line))

    def test_tight_box_is_not_flagged_for_reocr(self):
        # A box that genuinely fits its glyph is a complete reading.
        line = _boxed_line("H", (199, 336, 30, 44), confidence=0.92)
        self.assertFalse(_line_needs_underread_reocr(line))

    def test_low_confidence_wide_box_is_not_flagged_for_reocr(self):
        # A hesitant read over a wide box is more likely a phantom from art or a
        # balloon border than a partially read line.
        line = _boxed_line("MM", (232, 1129, 264, 60), confidence=0.61)
        self.assertFalse(_line_needs_underread_reocr(line))

    def test_reread_is_accepted_only_when_it_explains_the_box(self):
        box = (199, 336, 93, 44)
        # More glyphs, confident, and the box now makes sense: accept.
        self.assertTrue(_underread_candidate_is_better("H", "HEY,", 0.99, box))
        # Same glyph count adds nothing, even with perfect confidence.
        self.assertFalse(_underread_candidate_is_better("H", "X", 1.0, box))
        # Confident but still far too few glyphs for the box: reject.
        self.assertFalse(_underread_candidate_is_better("H", "HI", 0.99, (199, 336, 400, 44)))
        # A hesitant re-read never replaces a confident one.
        self.assertFalse(_underread_candidate_is_better("H", "HEY,", 0.40, box))

    def test_recovered_text_must_be_confident_and_lexical(self):
        self.assertTrue(_recovered_text_is_acceptable("TO THEM?", 0.96))
        # Glyph-shaped noise the engine is unsure about is not text.
        self.assertFalse(_recovered_text_is_acceptable("MLIOIO11", 0.59))
        # A confident smear with no real word is still not text.
        self.assertFalse(_recovered_text_is_acceptable("XXXX", 0.95))

    @staticmethod
    def _balloon_with_rows(rows):
        """White container with dark text rows; returns (image, row boxes)."""
        image = np.full((260, 420, 3), 255, dtype=np.uint8)
        boxes = []
        for text, (x, y) in rows:
            cv2.putText(image, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                        (10, 10, 10), 2)
            boxes.append((x, y - 22, 12 * len(text), 28))
        return image, boxes

    def _line_at(self, text, box, region=1):
        line = _boxed_line(text, box, confidence=0.95)
        line.metadata = {"visual_white_region_id": region,
                         "visual_white_region_enclosed": True}
        return line

    def test_uncovered_row_inside_container_is_found(self):
        # Two rows of text at normal line spacing, only the first recognised: the
        # second must be reported as a gap so it can be read on its own pixels.
        image, boxes = self._balloon_with_rows(
            [("FIRST ROW", (40, 90)), ("SECOND ROW", (40, 126))]
        )
        known = [self._line_at("FIRST ROW", boxes[0])]
        gaps = _container_text_gap_boxes(image, known)
        self.assertEqual(len(gaps), 1, gaps)
        # Each gap carries the container it belongs to, so a recovery can be
        # audited back to the balloon it came from.
        container_id, (gx, gy, gw, gh) = gaps[0]
        self.assertTrue(container_id)
        # The gap must land on the unread row, not on the row already read.
        self.assertGreater(gy, boxes[0][1])

    def test_fully_covered_container_reports_no_gap(self):
        image, boxes = self._balloon_with_rows(
            [("FIRST ROW", (40, 90)), ("SECOND ROW", (40, 150))]
        )
        known = [self._line_at("FIRST ROW", boxes[0]),
                 self._line_at("SECOND ROW", boxes[1])]
        self.assertEqual(_container_text_gap_boxes(image, known), [])

    def test_speck_of_noise_is_not_a_gap(self):
        # A couple of stray marks are not a row of text.
        image, boxes = self._balloon_with_rows([("FIRST ROW", (40, 90))])
        cv2.circle(image, (300, 200), 3, (10, 10, 10), -1)
        known = [self._line_at("FIRST ROW", boxes[0])]
        self.assertEqual(_container_text_gap_boxes(image, known), [])

    def test_lines_without_a_container_are_never_scanned(self):
        # Text drawn on open art belongs to no container, so the page is not
        # scanned for gaps at all.
        image, boxes = self._balloon_with_rows(
            [("FIRST ROW", (40, 90)), ("SECOND ROW", (40, 150))]
        )
        loose = _boxed_line("FIRST ROW", boxes[0], confidence=0.95)
        self.assertEqual(_container_text_gap_boxes(image, [loose]), [])

    def test_reocr_keeps_previous_reading_when_nothing_is_confident(self):
        # Fail-closed: with no confident candidate the line is left exactly as it
        # was and the gap is reported, never silently completed.
        image, boxes = self._balloon_with_rows([("FIRST ROW", (40, 90))])
        line = self._line_at("H", (199, 200, 93, 44))
        with patch("ocr_balloon._reocr_crop_candidates", return_value=[]):
            lines, records = apply_speech_container_reocr(image, [line], "3", 1)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].text, "H")
        self.assertTrue(records)
        self.assertFalse(records[0]["accepted"])
        self.assertEqual(records[0]["reason"],
                         "selective_reocr_no_confident_candidate")

    def test_candidate_normalization_ignores_case_and_spacing(self):
        self.assertEqual(
            _normalized_ocr_candidate_text("What'S so GrEAT?"),
            _normalized_ocr_candidate_text("WHAT'S SO GREAT?"),
        )

    def test_improbable_number_artifact_requests_regional_fallback(self):
        group = _scored_group("SURE!! INIOF 77,I")
        self.assertIn("improbable_number_token", group.quality_reasons)
        self.assertTrue(group_needs_selective_fallback(group))

    def test_ignored_decorative_group_still_allows_mobile_fallback(self):
        group = _scored_group("SURE!! INIOF 77,I")
        group.classification = "decorative"
        group.ignored = True
        group.ignore_reason = "decorative_text"

        self.assertTrue(group_needs_selective_fallback(group))
        self.assertTrue(_should_skip_paddle_full_for_ignored_decorative(group))

    def test_ignored_sfx_group_can_still_receive_selective_fallback(self):
        group = _scored_group("SURE!! INIOF 77,I")
        group.classification = "sfx"
        group.ignored = True
        group.ignore_reason = "sfx_translation_disabled"

        self.assertTrue(group_needs_selective_fallback(group))
        self.assertFalse(_should_skip_paddle_full_for_ignored_decorative(group))

    def test_ignored_translatable_group_can_still_receive_selective_fallback(self):
        group = _scored_group("SURE!! INIOF 77,I")
        group.classification = "narration"
        group.ignored = True
        group.ignore_reason = "layout_safety"

        self.assertTrue(group_needs_selective_fallback(group))
        self.assertFalse(_should_skip_paddle_full_for_ignored_decorative(group))

    def test_improbable_apostrophe_pattern_requests_regional_fallback(self):
        group = _scored_group("'SSIW'ON NO ONE CAN ENTER")
        self.assertIn("improbable_apostrophe_pattern", group.quality_reasons)
        self.assertTrue(group_needs_selective_fallback(group))

    def test_non_ascii_and_alternating_case_ocr_are_suspicious(self):
        non_ascii = _scored_group("I KNEW WE COULD COUNT ON InHIw no人人")
        alternating = _scored_group("WHAT IS hApPeNiNg HERE")
        self.assertIn("non_ascii_ocr_artifact", non_ascii.quality_reasons)
        self.assertIn("mixed_case_ocr_artifact", alternating.quality_reasons)
        self.assertTrue(group_needs_selective_fallback(non_ascii))
        self.assertTrue(group_needs_selective_fallback(alternating))

    def test_repeated_word_typo_is_generic_repair(self):
        repaired, reason = repair_ocr_text("REALLY RFALIY")
        self.assertEqual(repaired, "REALLY REALLY")
        self.assertIn("adjacent_common_word_ocr_typo", reason)

    def test_compact_word_is_segmented_with_generic_vocabulary(self):
        repaired, reason = repair_ocr_text("HAVETHESEPLNKSCOME")
        self.assertEqual(repaired, "HAVE THESE PLANKS COME")
        self.assertIn("segment_compact_english_word", reason)

        segmented, confidence = segment_compact_english_word("HAVETHESEPLNKSCOME")
        self.assertEqual(segmented.upper(), "HAVE THESE PLANKS COME")
        self.assertGreaterEqual(confidence, 0.58)

    def test_attached_contraction_and_compact_word_are_repaired_generically(self):
        repaired, reason = repair_ocr_text(
            "ICAN'T LETANYONE FIND OUT ABOUT THESE WINGS..."
        )
        self.assertEqual(repaired, "I CAN'T LET ANYONE FIND OUT ABOUT THESE WINGS...")
        self.assertIn("split_attached_pronoun_contraction", reason)
        self.assertIn("segment_compact_english_word", reason)

    def test_compact_vocative_is_preserved_as_possible_name(self):
        repaired, reason = repair_ocr_text("WAIT FOR ME, SUCHAN!")
        self.assertEqual(repaired, "WAIT FOR ME, SUCHAN!")
        self.assertEqual(reason, "")

        isolated, isolated_reason = repair_ocr_text("SUCHAN!")
        self.assertEqual(isolated, "SUCHAN!")
        self.assertEqual(isolated_reason, "")

    def test_chapter_consensus_recovers_split_name_without_runtime_dictionary(self):
        called_name = _scored_group("WAIT FOR ME, SUCHAN!")
        called_name.group_id = "G1"
        split_reference = _scored_group("SUCH AN IS COMING.")
        split_reference.group_id = "G2"
        second_reference = _scored_group("DRINKS WITH SUCH AN!")
        second_reference.group_id = "G3"
        standalone_reference = _scored_group("SUCH AN!")
        standalone_reference.group_id = "G4"

        repairs = normalize_recurring_compact_names(
            [called_name, split_reference, second_reference, standalone_reference]
        )

        self.assertEqual(split_reference.text, "SUCHAN IS COMING.")
        self.assertEqual(second_reference.text, "DRINKS WITH SUCHAN!")
        self.assertEqual(standalone_reference.text, "SUCHAN!")
        self.assertEqual(len(repairs), 3)
        self.assertTrue(standalone_reference.preserve_as_name)
        self.assertTrue(split_reference.detected_proper_names)
        self.assertTrue(
            all(
                repair["repair_reason"] == "chapter_consensus_compact_name"
                for repair in repairs
            )
        )

    def test_detected_name_is_added_to_translation_prompt_and_cache_signature(self):
        translator = TranslatorNvidiaBatch(api_key="test", enable_cache=False)
        before = translator._translation_cache_key("WAIT FOR NAME")
        translator.set_detected_names(["EXAMPLENAME"])
        after = translator._translation_cache_key("WAIT FOR NAME")

        self.assertNotEqual(before, after)
        self.assertIn("EXAMPLENAME", translator._system_prompt())
        self.assertNotIn("test", translator._system_prompt())

    def test_translation_cache_rejects_legacy_schema_and_separates_target(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(config, "CACHE_ROOT", folder):
                translator = TranslatorNvidiaBatch(api_key="test", enable_cache=True)
                text = "THE SIGNAL IS CLEAR."
                cache_path = translator._translation_cache_path(text)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps(
                        {
                            "key": translator._translation_cache_key(text),
                            "translation": "O SINAL ESTÁ LIMPO.",
                        }
                    ),
                    encoding="utf-8",
                )

                self.assertIsNone(translator._load_translation_cache(text))

                translator._save_translation_cache(text, "O SINAL ESTÁ LIMPO.")
                self.assertEqual(
                    translator._load_translation_cache(text),
                    "O SINAL ESTÁ LIMPO.",
                )

                alternate_target = TranslatorNvidiaBatch(
                    api_key="test",
                    enable_cache=True,
                    target_language="es-ES",
                )
                self.assertNotEqual(
                    translator._translation_cache_key(text),
                    alternate_target._translation_cache_key(text),
                )

    def test_vocative_name_variants_use_higher_confidence_consensus(self):
        uncertain = _scored_group("MIHUL, DO YOU WANT TO COME?")
        uncertain.lines[0].confidence = 0.71
        trusted = _scored_group("WAIT FOR ME, MIHUI!")
        trusted.lines[0].confidence = 0.98

        repairs = normalize_recurring_compact_names([uncertain, trusted])

        self.assertEqual(uncertain.text, "MIHUI, DO YOU WANT TO COME?")
        self.assertTrue(
            any(
                repair["repair_reason"] == "chapter_consensus_name_variant"
                for repair in repairs
            )
        )

    def test_inflected_english_residual_triggers_translation_retry(self):
        valid, reason = validate_translation_text(
            "DRINKS WITH EXAMPLENAME!",
            "DRINKS COM EXAMPLENAME!",
            "speech",
            ["EXAMPLENAME"],
        )
        self.assertFalse(valid)
        self.assertIn("residual_inflected_english", reason)

        name_valid, _ = validate_translation_text(
            "WAIT FOR EXAMPLENAME!",
            "ESPERE POR EXAMPLENAME!",
            "speech",
            ["EXAMPLENAME"],
        )
        self.assertTrue(name_valid)

    def test_short_stem_english_gerund_is_rejected_and_retried(self):
        group = _scored_group("THEY KEEP DOING THIS.")
        apply_group_translations([group], ["ELES CONTINUAM DOING ISSO."])

        self.assertFalse(group.translation_valid)
        self.assertTrue(
            group.translation_validation_reason.startswith(
                "residual_inflected_english:DOING"
            ),
            group.translation_validation_reason,
        )

        translator = _StrictRetryTranslator("ELES CONTINUAM FAZENDO ISSO.")
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            records = validate_and_retry_translations([group], translator)

        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["valid"], records)
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation_validation_reason, "retry_ok")
        self.assertEqual(group.translation, "ELES CONTINUAM FAZENDO ISSO.")
        self.assertFalse(group.manual_review_required)
        self.assertFalse(group.rejected_translation)

    def test_portuguese_s_final_tokens_are_not_inflected_english(self):
        cases = [
            (
                "JESus... IS THAT ALL BLOOD?!",
                "JESUS... É TODO SANGUE?!",
                [],
            ),
            ("MY GOD!", "MEU DEUS!", []),
            ("MAY GOD HELP US.", "DEUS NOS AJUDE.", []),
            ("HE USES A PENCIL.", "ELE USA LÁPIS.", []),
            ("THE BUS ARRIVED.", "O ÔNIBUS CHEGOU.", []),
            ("WE ARE HERE.", "NÓS ESTAMOS AQUI.", []),
            ("THIS IS TOO MUCH.", "ISSO É DEMAIS.", []),
            ("HE STAYED BEHIND.", "ELE FICOU PARA TRÁS.", []),
            ("THE COUNTRY IS IN DANGER.", "O PAÍS ESTÁ EM PERIGO.", []),
            ("AFTER THE RAIN, WE LEFT.", "APÓS A CHUVA, SAÍMOS.", []),
            ("CARLOS IS HERE.", "CARLOS ESTÁ AQUI.", ["CARLOS"]),
            ("MARCOS RETURNED.", "MARCOS VOLTOU.", ["MARCOS"]),
            ("LUCAS ARRIVED.", "LUCAS CHEGOU.", ["LUCAS"]),
        ]
        for source, candidate, allowed_names in cases:
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(
                    source,
                    candidate,
                    "speech",
                    allowed_names,
                )
                self.assertTrue(valid, reason)

    def test_real_english_inflections_still_require_translation(self):
        cases = [
            ("I SAW TWO TRAINS.", "EU VI DOIS TRAINS."),
            ("HE RUNS VERY FAST.", "ELE RUNS MUITO RÁPIDO."),
            ("THE DOORS ARE CLOSED.", "AS DOORS ESTÃO FECHADAS."),
            ("HE WALKED HERE.", "ELE WALKED ATÉ AQUI."),
            ("SHE IS CRYING.", "ELA ESTÁ CRYING."),
            ("WE ARE WAITING.", "NÓS ESTAMOS WAITING."),
            ("HE LOOKS TIRED.", "ELE LOOKS CANSADO."),
            ("THE LIGHTS WENT OUT.", "AS LIGHTS APAGARAM."),
            ("THE MONSTERS ARE COMING.", "OS MONSTERS ESTÃO VINDO."),
            ("THE BARS ARE CLOSED.", "OS BARS ESTÃO FECHADOS."),
            ("THE MAPS ARE HERE.", "OS MAPS ESTÃO AQUI."),
        ]
        for source, candidate in cases:
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(
                    source,
                    candidate,
                    "speech",
                )
                self.assertFalse(valid)
                self.assertTrue(
                    reason.startswith("residual_inflected_english"),
                    reason,
                )

    def test_inflected_english_requires_lexical_base_evidence(self):
        expected_bases = {
            "JESUS": "",
            "TRAINS": "TRAIN",
            "RUNS": "RUN",
            "DOORS": "DOOR",
            "DRINKS": "DRINK",
            "WALKED": "WALK",
            "CRYING": "CRY",
            "WAITING": "WAIT",
            "LOOKS": "LOOK",
            "LIGHTS": "LIGHT",
            "MONSTERS": "MONSTER",
            "BARS": "BAR",
            "MAPS": "MAP",
        }
        for token, expected in expected_bases.items():
            with self.subTest(token=token):
                self.assertEqual(_english_inflection_base(token), expected)

    def test_ambiguous_short_tokens_and_names_remain_contextual(self):
        valid_cases = [
            ("THE SIGLA IS US.", "A SIGLA US ESTÁ AQUI.", ["US"]),
            ("THE DOORS ARE CLOSED.", "AS PORTAS ESTÃO FECHADAS.", []),
            ("THE ANSWER IS NO.", "A RESPOSTA ESTÁ NO PAPEL.", []),
            ("THIS IS SO SIMPLE.", "ISSO É SÓ O COMEÇO.", []),
            ("IF NEEDED, I WILL GO.", "SE FOR PRECISO, EU VOU.", []),
            ("MILES IS HERE.", "MILES ESTÁ COM ELA.", ["MILES"]),
            ("JAMES IS HERE.", "JAMES ESTÁ COM ELA.", ["JAMES"]),
            ("WELLS IS HERE.", "WELLS ESTÁ COM ELA.", ["WELLS"]),
            ("TAKE THE BUS.", "O TERMO BUS É VÁLIDO.", ["BUS"]),
            ("USE PLUS.", "O TERMO PLUS É VÁLIDO.", ["PLUS"]),
        ]
        for source, candidate, allowed_names in valid_cases:
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(
                    source,
                    candidate,
                    "speech",
                    allowed_names,
                )
                self.assertTrue(valid, reason)

        invalid_cases = [
            ("HE IS HERE.", "ELE IS AQUI."),
            ("THIS IS SO WRONG.", "EU ESTOU SO TIRED."),
            ("FOR REAL, I DO NOT KNOW.", "FOR REAL, EU NÃO SEI."),
        ]
        for source, candidate in invalid_cases:
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(
                    source,
                    candidate,
                    "speech",
                )
                self.assertFalse(valid)
                self.assertTrue(reason)

    def test_source_comparison_preserves_names_without_hiding_english(self):
        valid_cases = [
            (
                "JESus... IS THAT ALL BLOOD?!",
                "JESUS... É TODO SANGUE?!",
                [],
            ),
            ("JAMES IS HERE.", "JAMES ESTÁ COM ELA.", ["JAMES"]),
            ("WHAT HAPPENED?", "JESUS... O QUE ACONTECEU?!", []),
        ]
        for source, candidate, allowed_names in valid_cases:
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(
                    source,
                    candidate,
                    "speech",
                    allowed_names,
                )
                self.assertTrue(valid, reason)

        valid, reason = validate_translation_text(
            "THE MONSTERS ARE COMING.",
            "OS MONSTERS ESTÃO VINDO.",
            "speech",
        )
        self.assertFalse(valid)
        self.assertTrue(reason.startswith("residual_inflected_english"), reason)

    def test_valid_portuguese_jesus_candidate_skips_retry_and_review(self):
        group = _scored_group("JESus... IS THAT ALL BLOOD?!")
        apply_group_translations([group], ["JESUS... É TODO SANGUE?!"])
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation_validation_reason, "ok")

        translator = _StrictRetryTranslator("UNUSED")
        records = validate_and_retry_translations([group], translator)

        self.assertEqual(translator.calls, [])
        self.assertEqual(records, [])
        self.assertFalse(group.manual_review_required)
        self.assertFalse(group.rejected_translation)

        debug_data = _debug_payload("", group.lines, [], [group])
        states = [
            {
                "index": 1,
                "status": "processed",
                "output_path": "",
                "image_path": "",
                "timings": {},
                "debug_data": debug_data,
            }
        ]
        quality_report = _build_quality_report({}, states, records)
        aggregate = _aggregate_debug_data(states)
        self.assertEqual(quality_report["totals"]["mixed_language_items"], 0)
        self.assertEqual(quality_report["totals"]["translations_rejected"], 0)
        self.assertEqual(quality_report["totals"]["manual_review_required_groups"], 0)
        self.assertEqual(aggregate["mixed_language_items"], 0)

    def test_english_jesus_candidate_retries_to_valid_portuguese(self):
        group = _scored_group("JESus... IS THAT ALL BLOOD?!")
        apply_group_translations([group], ["JESUS... HE IS ALL BLOOD?!"])
        self.assertFalse(group.translation_valid)

        translator = _StrictRetryTranslator(
            "JESUS... ELE ESTÁ COBERTO DE SANGUE?!"
        )
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            records = validate_and_retry_translations([group], translator)

        self.assertEqual(len(translator.calls), 1)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["valid"], records)
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation_validation_reason, "retry_ok")
        self.assertFalse(group.manual_review_required)

    def test_english_jesus_retry_still_invalid_requires_manual_review(self):
        group = _scored_group("JESus... IS THAT ALL BLOOD?!")
        apply_group_translations([group], ["JESUS... HE IS ALL BLOOD?!"])
        initial_translation = group.translation
        translator = _StrictRetryTranslator(
            "JESUS... ELE IS COBERTO DE SANGUE?!"
        )
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            records = validate_and_retry_translations([group], translator)

        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["valid"], records)
        self.assertFalse(group.translation_valid)
        self.assertTrue(group.manual_review_required)
        self.assertEqual(group.rejected_translation, initial_translation)
        self.assertEqual(group.translation, group.text)

    def test_portuguese_ambiguity_fix_preserves_multilingual_and_sfx_controls(self):
        invalid_cases = [
            ("MAYBE IT IS THIS WAY.", "QUIZÁS SEJA POR AQUI.", "speech"),
            ("Sh-She'S COMING!", "Sh- Ela está vindo!", "speech"),
            ("Sh-She'S COMING!", "Sh - Ela está vindo!", "speech"),
            ("I AM SO TIRED.", "EU ESTOU SO TIRED.", "speech"),
            ("FOR REAL, I DO NOT KNOW.", "FOR REAL, EU NÃO SEI.", "speech"),
        ]
        for source, candidate, classification in invalid_cases:
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(
                    source,
                    candidate,
                    classification,
                )
                self.assertFalse(valid)
                self.assertTrue(reason)

        valid_cases = [
            ("I MUST'VE FALLEN ASLEEP.", "DEVO TER ADORMIDO.", "speech"),
            ("OW, MY BUTT...", "AI, MEU BUMBUM..", "speech"),
            ("RUN!", "SÓ CORRA.", "speech"),
            ("IF NEEDED, I WILL GO.", "SE FOR PRECISO, EU VOU.", "speech"),
        ]
        for source, candidate, classification in valid_cases:
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(
                    source,
                    candidate,
                    classification,
                )
                self.assertTrue(valid, reason)

        for text in (
            "S***!",
            "F***!",
            "SH**!",
            "SHWOOMP",
            "THWACK",
            "SPLAT",
            "CREAK",
        ):
            with self.subTest(sfx=text):
                valid, reason = validate_translation_text(text, text, "sfx")
                self.assertTrue(valid, reason)
                self.assertEqual(reason, "sfx_preserved")

    def test_short_ocr_typo_is_suspicious_and_better_candidate_wins(self):
        bad = _scored_group("BLT")
        good = _scored_group("BUT")
        self.assertTrue(group_needs_selective_fallback(bad))
        self.assertGreater(good.quality_score, bad.quality_score)
        self.assertEqual(repair_ocr_text("BLT")[0], "BUT")

    def test_multiline_lexical_confidence_disagreement_requests_fallback(self):
        group = _lexically_corrupted_multiline_group()

        self.assertIn(
            "cross_line_lexical_confidence_disagreement",
            group.quality_reasons,
        )
        self.assertTrue(group_needs_selective_fallback(group))
        self.assertEqual([line.text for line in group.lines], [
            "JUST WHAT",
            "ON EARTH IS",
            "ENO DNIOD",
        ])

    def test_lexical_disagreement_guard_preserves_special_text_contexts(self):
        controls = []

        named_lines = [
            _boxed_line("WAIT FOR", (20, 20, 180, 36), confidence=0.99),
            _boxed_line("EXAMPLE SURNAME", (20, 65, 220, 36), confidence=0.90),
        ]
        named = TextGroup(
            group_id="NAME",
            lines=named_lines,
            text="WAIT FOR EXAMPLE SURNAME",
            classification="speech",
            inside_balloon_like_region=True,
            source_engine="rapidocr",
            detected_proper_names=["EXAMPLE", "SURNAME"],
        )
        controls.append(named)

        sfx = TextGroup(
            group_id="SFX",
            lines=[
                _boxed_line("LOUD IMPACT", (20, 20, 180, 36), confidence=0.99),
                _boxed_line("SHWOOMP", (20, 65, 180, 36), confidence=0.88),
            ],
            text="LOUD IMPACT SHWOOMP",
            classification="sfx",
            source_engine="rapidocr",
        )
        controls.append(sfx)

        for text in (
            "S***!",
            "F***!",
            "SH**!",
            "DNA GPS CPU",
            "Wi-Fi X-23",
            "SHIHAE STATION",
        ):
            controls.append(
                TextGroup(
                    group_id="SPECIAL",
                    lines=[
                        _boxed_line("WAIT FOR", (20, 20, 180, 36), confidence=0.99),
                        _boxed_line(text, (20, 65, 180, 36), confidence=0.88),
                    ],
                    text=f"WAIT FOR {text}",
                    classification="speech",
                    inside_balloon_like_region=True,
                    source_engine="rapidocr",
                )
            )

        for group in controls:
            with self.subTest(text=group.text, classification=group.classification):
                _, reasons = score_group_ocr_quality(group)
                self.assertNotIn(
                    "cross_line_lexical_confidence_disagreement",
                    reasons,
                )

    def test_coherent_lower_confidence_regional_candidate_replaces_bad_span(self):
        original = _lexically_corrupted_multiline_group()
        regional = _regional_candidate(
            "JUST WHAT ON EARTH IS GOING ON?",
            "paddle_mobile",
            0.86,
        )
        selected_lines, records = _run_fake_selective_fallback(original, [regional])

        self.assertEqual(
            " ".join(line.text for line in selected_lines),
            "JUST WHAT ON EARTH IS GOING ON?",
        )
        self.assertEqual(records[0]["fallback_variant"], "paddle_mobile")
        self.assertTrue(records[0]["fallback_used"])
        self.assertGreater(
            records[0]["attempts"][0]["selection_score"],
            records[0]["original_quality_score"],
        )

    def test_disagreeing_regional_engines_are_observable_before_full_wins(self):
        original = _lexically_corrupted_multiline_group()
        mobile = _regional_candidate("iiON", "paddle_mobile", 0.93)
        full = _regional_candidate(
            "JUST WHAT ON EARTH IS GOING ON?",
            "paddle_full",
            0.89,
        )
        selected_lines, records = _run_fake_selective_fallback(
            original,
            [mobile, full],
        )

        self.assertEqual(
            " ".join(line.text for line in selected_lines),
            "JUST WHAT ON EARTH IS GOING ON?",
        )
        self.assertEqual(records[0]["fallback_variant"], "paddle_full")
        self.assertEqual([attempt["engine"] for attempt in records[0]["attempts"]], [
            "paddle_mobile",
            "paddle_full",
        ])
        self.assertEqual(
            records[0]["attempts"][0]["peer_engine_agreement_bonus"],
            0.0,
        )
        self.assertEqual(
            records[0]["attempts"][1]["peer_engine_agreement_bonus"],
            0.0,
        )

    def test_grouping_preserves_vertical_line_order(self):
        cases = (
            (
                [
                    _boxed_line("IS GOING ON?", (20, 65, 220, 36)),
                    _boxed_line("WHAT ON EARTH", (20, 20, 220, 36)),
                ],
                "WHAT ON EARTH IS GOING ON?",
            ),
            (
                [
                    _boxed_line("SECOND LINE", (20, 65, 220, 36)),
                    _boxed_line("FIRST LINE", (20, 20, 220, 36)),
                ],
                "FIRST LINE SECOND LINE",
            ),
        )
        for lines, expected in cases:
            with self.subTest(expected=expected):
                groups = _group_lines(lines)
                self.assertEqual(len(groups), 1)
                self.assertEqual(groups[0].text, expected)

    def test_cross_engine_repair_acceptance_does_not_fabricate_text(self):
        improved = assess_ocr_repair(
            "PLNKS",
            "PLANKS",
            "dictionary_edit_distance_repair",
            confidence=0.90,
            source_engine="rapidocr",
            agreeing_engines=["paddle_mobile"],
        )
        degraded = assess_ocr_repair(
            "GOING ON",
            "ENO DNIOD",
            "dictionary_edit_distance_repair",
            confidence=0.99,
            source_engine="rapidocr",
            agreeing_engines=["paddle_mobile"],
        )

        self.assertTrue(improved["accepted"])
        self.assertFalse(degraded["accepted"])
        self.assertEqual(degraded["rejection_reason"], "edit_distance_too_large")

    def test_planks_repair_is_not_phrase_specific(self):
        repaired, reason = repair_ocr_text("PLNKS")
        self.assertEqual(repaired, "PLANKS")
        self.assertIn("dictionary_edit_distance_repair", reason)

        suggestion, score = suggest_english_word("PLNKS")
        self.assertEqual(suggestion, "planks")
        self.assertGreaterEqual(score, 0.62)

    def test_no_runtime_ready_made_translation(self):
        translated = TranslatorNvidiaBatch._postprocess_translation(
            "I WILL DO BETTER FOR MOM",
            "SENTINEL_TRANSLATION_FROM_NVIDIA",
        )
        self.assertEqual(translated, "SENTINEL_TRANSLATION_FROM_NVIDIA")

    def test_mixed_english_translation_is_rejected_generically(self):
        valid, reason = validate_translation_text(
            "I WILL DO BETTER FOR MOM",
            "EU VOU FAZER MELHOR FOR MOM",
        )
        self.assertFalse(valid)
        self.assertIn("mixed_language_tokens", reason)

        valid, reason = validate_translation_text(
            "I WILL DO BETTER FOR MOM",
            "EU VOU FAZER MELHOR PELA MINHA MAE",
        )
        self.assertTrue(valid, reason)

    def test_portuguese_for_and_accented_so_are_not_residual_english(self):
        cases = [
            "SEJA O QUE FOR. VAMOS SÓ SAIR.",
            "SÓ CORRA.",
            "Só corra.",
            "SE FOR PRECISO, EU VOU.",
            "ISSO É SÓ O COMEÇO.",
            "ISSO E\u0301 SO\u0301 O COMEÇO.",
            "FOR O QUE FOR, CONTINUE.",
            "QUANDO FOR A HORA, AVISE.",
            "QUEM FOR PRIMEIRO, ESPERE.",
            "ONDE FOR NECESSÁRIO, CORRIJA.",
            "COMO FOR MELHOR PARA VOCÊ.",
            "O QUE FOR PRECISO, EU FAREI.",
            "SE FOR POSSÍVEL, VOLTE.",
            "NÃO IMPORTA QUEM FOR.",
            "VÁ ONDE FOR MAIS SEGURO.",
            "ESCOLHA COMO FOR MELHOR.",
            "EU SÓ QUERO IR EMBORA.",
            "SÓ VOCÊ PODE FAZER ISSO.",
            "FIQUE SÓ MAIS UM POUCO.",
        ]
        for translation in cases:
            with self.subTest(translation=translation):
                valid, reason = validate_translation_text(
                    "Portuguese sentence",
                    translation,
                    "speech",
                )
                self.assertTrue(valid, reason)

    def test_real_so_and_for_english_residuals_still_fail(self):
        cases = [
            ("THIS IS SO WRONG.", "THIS IS SO WRONG."),
            ("I AM SO TIRED.", "EU ESTOU SO TIRED."),
            ("FOR REAL, I DON'T KNOW.", "FOR REAL, EU NÃO SEI."),
            ("I WILL GO HOME.", "EU VOU FOR HOME."),
            ("SO, WHAT DO WE DO NOW?", "SO, O QUE FAZEMOS AGORA?"),
            ("I'M HERE FOR YOU.", "I'M HERE FOR YOU."),
            ("I DID THIS FOR YOU.", "EU FIZ ISSO FOR YOU."),
            ("FOR NOW, WAIT.", "FOR NOW, ESPERE."),
            ("THIS IS FOR REAL.", "THIS IS FOR REAL."),
            ("I AM HERE FOR YOU.", "EU ESTOU AQUI FOR YOU."),
            ("WHAT FOR?", "WHAT FOR?"),
            ("FOR ME, IT DOESN'T MATTER.", "FOR ME, ISSO NÃO IMPORTA."),
            ("I DID IT FOR HER.", "I DID IT FOR HER."),
            ("I DID THIS FOR YOU.", "EU FIZ ISSO FOR VOCÊ."),
            ("I AM HERE FOR HER.", "EU ESTOU AQUI FOR ELA."),
            ("I CAME BACK FOR YOU.", "EU VOLTEI FOR YOU."),
            ("THIS IS FOR REAL.", "ISSO É FOR REAL."),
            ("THIS IS SO WRONG.", "SO WHAT?"),
            ("I'M SO SORRY.", "I'M SO SORRY."),
            ("I AM SO CONFUSED.", "EU ESTOU SO CONFUSED."),
            ("HELL", "HÉLL"),
        ]
        for source, translation in cases:
            with self.subTest(translation=translation):
                valid, reason = validate_translation_text(
                    source,
                    translation,
                    "speech",
                )
                self.assertFalse(valid)
                self.assertTrue(reason)

    def test_residual_pdf_mixed_pt_en_translations_are_rejected(self):
        cases = [
            ("I DON'T REMEMBER GETTING OFF THE TRAIN.", "EU NÃO LEMBRO DE TER DESCIDO DO TRAIN."),
            ("I DON'T THINK I TOOK THE WRONG ONE...", "EU NÃO ACHO QUE TENHA PEGADO O ERRADO ONE..."),
            ("WHY ARE ALL THE SCREENS OFF HERE?", "E POR QUE TODAS AS TELAS ESTÃO DESLIGADAS HERE?"),
            ("I MISSED THE LAST TRAIN!", "EU PERDI O ÚLTIMO TRAIN!"),
            ("MY TRAIN CARD WON'T READ.", "MEU CARTÃO DO TRAIN NÃO LÊ."),
            ("HELLO! ANYONE HERE?", "ALÔ! ALGUÉM HERE?"),
            ("I'VE NEVER SEEN RED FOG BEFORE...", "NUNCA VI VERMELHO FOG ANTES..."),
            ("IT'S A PERSON!!", "É UMA PERSON!!"),
            ("A C-CORPSE...", "UM C-CORPSE..."),
            ("SH-SHE'S COMING!", "Sh-She's VINDO!"),
            ("A DOOR!!", "UM DOOR!!"),
            ("LET'S GO THE OTHER WAY!!", "VAMOS PELO OUTRO WAY!!"),
            ("THIS PLACE IS HELL!!", "ESTE LUGAR É HELL!!"),
        ]
        for source, translation in cases:
            with self.subTest(translation=translation):
                valid, reason = validate_translation_text(
                    source,
                    translation,
                    "speech",
                )
                self.assertFalse(valid)
                self.assertTrue(reason)

    def test_residual_spanish_leakage_is_rejected_without_harming_ptbr_controls(self):
        invalid_cases = [
            ("MAYBE IT'S THIS WAY.", "QUIZÁS SEJA POR AQUI."),
            ("MAYBE IT'S THIS WAY.", "QUIZA\u0301S SEJA POR AQUI."),
            (
                "MAYBE THERE WAS SOME SORT OF DISASTER.",
                "QUIZÁS TIVOU ALGUM DESASTRE E ELES EVACUARAM TUDO.",
            ),
        ]
        for source, translation in invalid_cases:
            with self.subTest(translation=translation):
                valid, reason = validate_translation_text(
                    source,
                    translation,
                    "speech",
                )
                self.assertFalse(valid)
                self.assertTrue(
                    reason.startswith("residual_spanish_token"),
                    reason,
                )

        valid_cases = [
            ("I MUST'VE FALLEN ASLEEP.", "DEVO TER ADORMIDO."),
            ("OW, MY BUTT...", "AI, MEU BUMBUM.."),
            ("MAYBE IT'S THIS WAY.", "TALVEZ SEJA POR AQUI."),
            ("HE ANSWERED IN SPANISH.", "QUIZÁS MAÑANA."),
        ]
        for source, translation in valid_cases:
            with self.subTest(translation=translation):
                valid, reason = validate_translation_text(
                    source,
                    translation,
                    "speech",
                )
                self.assertTrue(valid, reason)

        name_valid, name_reason = validate_translation_text(
            "WAIT FOR QUIZAS.",
            "ESPERE POR QUIZÁS.",
            "speech",
            ["QUIZÁS"],
        )
        self.assertTrue(name_valid, name_reason)

        censorship_valid, censorship_reason = validate_translation_text(
            "S***!",
            "S***!",
            "sfx",
        )
        self.assertTrue(censorship_valid, censorship_reason)
        self.assertEqual(censorship_reason, "sfx_preserved")

        decorative_valid, decorative_reason = validate_translation_text(
            "QUIZAS",
            "QUIZÁS",
            "decorative",
        )
        self.assertTrue(decorative_valid, decorative_reason)

        localized_stutter_valid, localized_stutter_reason = validate_translation_text(
            "Sh-She'S COMING!",
            "E-Ela está vindo!",
            "speech",
        )
        self.assertTrue(localized_stutter_valid, localized_stutter_reason)

        proper_name_stutter_valid, proper_name_stutter_reason = (
            validate_translation_text(
                "Sh-Shihae is here!",
                "Sh-Shihae está aqui!",
                "speech",
                ["SHIHAE"],
            )
        )
        self.assertTrue(proper_name_stutter_valid, proper_name_stutter_reason)

    def test_runtime_retry_corrects_residual_spanish_translation(self):
        group = _scored_group("MAYBE IT'S THIS WAY.")
        apply_group_translations([group], ["QUIZÁS SEJA POR AQUI."])
        self.assertFalse(group.translation_valid)
        self.assertTrue(
            group.translation_validation_reason.startswith("residual_spanish_token"),
            group.translation_validation_reason,
        )

        translator = _StrictRetryTranslator("TALVEZ SEJA POR AQUI.")
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            records = validate_and_retry_translations([group], translator)

        self.assertEqual(len(translator.calls), 1)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["valid"], records)
        self.assertEqual(records[0]["reason"], "ok")
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation_validation_reason, "retry_ok")
        self.assertEqual(group.translation, "TALVEZ SEJA POR AQUI.")
        self.assertFalse(group.manual_review_required)

    def test_runtime_failed_spanish_retry_requires_manual_review(self):
        group = _scored_group("MAYBE IT'S THIS WAY.")
        apply_group_translations([group], ["QUIZÁS SEJA POR AQUI."])
        self.assertFalse(group.translation_valid)

        translator = _StrictRetryTranslator("QUIZÁS TALVEZ SEJA POR AQUI.")
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            records = validate_and_retry_translations([group], translator)

        # The strict retry keeps the Spanish token, so the group has no detected
        # name and earns the final isolated attempt, which fails as well.
        self.assertEqual(len(records), 2)
        self.assertFalse(records[0]["valid"], records)
        self.assertTrue(
            records[0]["reason"].startswith("residual_spanish_token"),
            records,
        )
        self.assertTrue(records[-1]["isolated"], records)
        self.assertFalse(records[-1]["valid"], records)
        self.assertFalse(group.translation_valid)
        self.assertTrue(group.manual_review_required)
        self.assertEqual(group.rejected_translation, "QUIZÁS SEJA POR AQUI.")
        self.assertEqual(group.translation, group.text)

    def test_partial_source_language_retry_is_rejected_and_reported(self):
        valid, reason = validate_translation_text(
            "Sh-She'S COMING!",
            "Sh-Ela está vindo!",
            "speech",
        )
        self.assertFalse(valid)
        self.assertTrue(
            reason.startswith("multilingual_partial_translation"),
            reason,
        )

        group = _scored_group("Sh-She'S COMING!")
        apply_group_translations([group], ["Sh-She'S VINDO!"])
        self.assertFalse(group.translation_valid)

        translator = _StrictRetryTranslator("Sh-Ela está vindo!")
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            records = validate_and_retry_translations([group], translator)

        self.assertEqual(len(translator.calls), 1)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["valid"], records)
        self.assertTrue(
            records[0]["reason"].startswith(
                "multilingual_partial_translation"
            ),
            records,
        )
        self.assertFalse(group.translation_valid)
        self.assertTrue(group.manual_review_required)
        self.assertEqual(group.rejected_translation, "Sh-She'S VINDO!")
        self.assertEqual(group.translation, group.text)

        item = {
            "id": group.group_id,
            "translation_validation_reason": group.translation_validation_reason,
            "manual_review_required": group.manual_review_required,
            "rejected_translation": group.rejected_translation,
        }
        states = [
            {
                "index": 1,
                "status": "processed",
                "output_path": "",
                "image_path": "",
                "timings": {},
                "debug_data": {
                    "items": [item],
                    "selective_ocr_fallbacks": [],
                    "classification_counts": {},
                },
            }
        ]
        quality_report = _build_quality_report({}, states, records)
        aggregate = _aggregate_debug_data(states)
        self.assertEqual(quality_report["totals"]["mixed_language_items"], 1)
        self.assertEqual(aggregate["mixed_language_items"], 1)

    def test_stutter_prefix_must_match_its_own_translated_word(self):
        # A stutter repeats the translated word's own initial. Keeping the
        # source word's stutter letter ("S-STOP" -> "S-PARA" instead of
        # "P-PARA") is a residual source-language artifact. This must not depend
        # on any specific source word.
        for source, candidate in (
            ("S-STOP!!", "S-PARA!!"),
            ("K-KILL!", "K-MATAR!"),
        ):
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(source, candidate, "speech")
                self.assertFalse(valid, candidate)
                self.assertTrue(
                    reason.startswith("multilingual_partial_translation"), reason
                )

    def test_wellformed_translated_stutter_is_accepted(self):
        # The stutter letter re-derived from the translated word is valid, and a
        # dropped/consistent stutter must not be flagged.
        for source, candidate in (
            ("S-STOP!!", "P-PARA!!"),
            ("K-KILL!", "M-MATAR!"),
            ("P-PLEASE!", "P-POR FAVOR!"),
        ):
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(source, candidate, "speech")
                self.assertTrue(valid, f"{candidate}: {reason}")

    def test_residual_spanish_reason_is_reported_as_mixed_language(self):
        states = [
            {
                "index": 1,
                "status": "processed",
                "output_path": "",
                "image_path": "",
                "timings": {},
                "debug_data": {
                    "items": [
                        {
                            "id": "T1",
                            "translation_validation_reason": (
                                "residual_spanish_token:QUIZAS"
                            ),
                        }
                    ],
                    "selective_ocr_fallbacks": [],
                    "classification_counts": {},
                },
            }
        ]

        quality_report = _build_quality_report({}, states, [])
        aggregate = _aggregate_debug_data(states)
        self.assertEqual(quality_report["totals"]["mixed_language_items"], 1)
        self.assertEqual(aggregate["mixed_language_items"], 1)

    @staticmethod
    def _coverage_state(items):
        return [{
            "index": 1, "status": "processed", "output_path": "", "image_path": "",
            "timings": {},
            "debug_data": {"items": items, "selective_ocr_fallbacks": [],
                           "classification_counts": {}},
        }]

    @staticmethod
    def _rendered_speech(id_, box, text="TRANSLATED LINE"):
        return {"id": id_, "classification": "speech", "translation_final_state":
                "translated", "redrawn": True, "bounding_box": list(box),
                "clean_text": text, "translation": text}

    @staticmethod
    def _unrendered(id_, box, text, classification="decorative", state="manual_review"):
        return {"id": id_, "classification": classification,
                "translation_final_state": state, "redrawn": False,
                "bounding_box": list(box), "clean_text": text, "translation": ""}

    def test_partial_speech_region_coverage_forces_review(self):
        # A rendered speech line stacked directly above an untranslated source
        # line in the same balloon means part of the balloon is still in the
        # source language: the region is not complete and must require review.
        items = [
            self._rendered_speech("R1", (140, 534, 430, 44)),
            self._unrendered("S1", (138, 591, 432, 43), "THINGS YOU BECOME"),
        ]
        violations = _incomplete_speech_region_coverage(self._coverage_state(items))
        self.assertEqual(len(violations), 1, violations)
        acc = _translation_quality_accounting(self._coverage_state(items))
        self.assertEqual(acc["incomplete_region_coverage"], 1)
        self.assertTrue(acc["requires_review"])
        self.assertFalse(acc["quality_passed"])

    def test_same_line_untranslated_sibling_forces_review(self):
        # An untranslated fragment on the same text line as a rendered speech
        # line (e.g. the tail of a split exclamation) is residual source text.
        items = [
            self._rendered_speech("R1", (180, 167, 277, 59)),
            self._unrendered("S1", (460, 176, 80, 46), "IT", classification="sfx",
                             state="skipped_with_reason"),
        ]
        violations = _incomplete_speech_region_coverage(self._coverage_state(items))
        self.assertEqual(len(violations), 1, violations)

    def test_dropped_short_source_line_in_balloon_forces_review(self):
        # A short source line dropped before grouping (no terminal state) that
        # sits inside the balloon of a rendered line is still visible source.
        items = [
            self._rendered_speech("R1", (255, 360, 163, 39)),
            {"id": "LINE_1", "classification": "unknown",
             "translation_final_state": None, "redrawn": False,
             "bounding_box": [303, 314, 66, 43], "clean_text": "SO"},
        ]
        violations = _incomplete_speech_region_coverage(self._coverage_state(items))
        self.assertEqual(len(violations), 1, violations)

    def test_partially_recognized_line_in_balloon_forces_review(self):
        # The engine returned a single glyph for a box wide enough to hold a
        # whole word: unread source text is still sitting in the balloon, so the
        # region must not be reported as complete.
        items = [
            self._rendered_speech("R1", (145, 382, 203, 84)),
            {"id": "LINE_1", "classification": "unknown",
             "translation_final_state": None, "redrawn": False,
             "bounding_box": [199, 336, 93, 44], "clean_text": "H",
             "confidence": 0.9202},
        ]
        violations = _incomplete_speech_region_coverage(self._coverage_state(items))
        self.assertEqual(len(violations), 1, violations)
        acc = _translation_quality_accounting(self._coverage_state(items))
        self.assertTrue(acc["requires_review"])

    def test_low_confidence_phantom_read_does_not_force_review(self):
        # Noise picked up from a balloon border comes back with low confidence.
        # It is not evidence of unread text and must not force a review.
        items = [
            self._rendered_speech("R1", (229, 1084, 250, 40)),
            {"id": "LINE_1", "classification": "unknown",
             "translation_final_state": None, "redrawn": False,
             "bounding_box": [232, 1129, 264, 60], "clean_text": "MM",
             "confidence": 0.6087},
        ]
        violations = _incomplete_speech_region_coverage(self._coverage_state(items))
        self.assertEqual(violations, [])

    def test_tight_single_glyph_does_not_force_review(self):
        # A box that genuinely fits one glyph is not evidence of unread text.
        items = [
            self._rendered_speech("R1", (145, 382, 203, 84)),
            {"id": "LINE_1", "classification": "unknown",
             "translation_final_state": None, "redrawn": False,
             "bounding_box": [199, 346, 30, 40], "clean_text": "H"},
        ]
        violations = _incomplete_speech_region_coverage(self._coverage_state(items))
        self.assertEqual(violations, [])

    def test_complete_region_does_not_force_review(self):
        # Two stacked lines both rendered: the region is fully translated.
        items = [
            self._rendered_speech("R1", (140, 534, 430, 44)),
            self._rendered_speech("R2", (138, 591, 432, 43)),
        ]
        violations = _incomplete_speech_region_coverage(self._coverage_state(items))
        self.assertEqual(violations, [])
        acc = _translation_quality_accounting(self._coverage_state(items))
        self.assertEqual(acc["incomplete_region_coverage"], 0)

    def test_distant_untranslated_text_does_not_force_review(self):
        # A legitimate untranslated element (e.g. an SFX on the art) far from any
        # rendered balloon line must not be treated as residual coverage.
        items = [
            self._rendered_speech("R1", (140, 200, 200, 40)),
            self._unrendered("S1", (80, 1400, 300, 120), "CRASH",
                             classification="sfx", state="skipped_with_reason"),
        ]
        violations = _incomplete_speech_region_coverage(self._coverage_state(items))
        self.assertEqual(violations, [])

    # ---- RC1: short lines retained inside speech containers ----

    @staticmethod
    def _speech_group_from(lines, enclosed=True):
        # Mark the group's lines as sitting in an enclosed visual container so
        # the reclaim guard (dialogue balloons only, not art SFX) is satisfied.
        for line in lines:
            meta = dict(line.metadata or {})
            meta.setdefault("visual_white_region_enclosed", enclosed)
            line.metadata = meta
        return _group_lines(lines)[0]

    def test_short_lexical_line_above_is_reclaimed_into_speech_group(self):
        # A short word dropped by a noise filter but sitting directly above a
        # larger speech line in the same balloon must join it, so it is
        # translated instead of being left as visible source text.
        big = _boxed_line("GET UP", (257, 360, 162, 42), confidence=0.99)
        short = _boxed_line("SO", (303, 314, 66, 43), confidence=0.78)
        group = self._speech_group_from([big])
        candidate = TextCandidate(line=short, ignored=True,
                                  ignore_reason="noise_like_text")
        _reclaim_short_lexical_lines([group], [candidate], (1374, 690, 3))
        self.assertIn("SO", group.text)
        self.assertEqual(len(group.lines), 2)
        self.assertFalse(candidate.ignored)

    def test_short_lexical_line_below_is_reclaimed_into_speech_group(self):
        big = _boxed_line("PLEASE", (257, 300, 162, 42), confidence=0.99)
        short = _boxed_line("NO", (300, 346, 66, 43), confidence=0.78)
        group = self._speech_group_from([big])
        candidate = TextCandidate(line=short, ignored=True,
                                  ignore_reason="noise_like_text")
        _reclaim_short_lexical_lines([group], [candidate], (1374, 690, 3))
        self.assertIn("NO", group.text)
        self.assertFalse(candidate.ignored)

    def test_distant_short_line_stays_noise(self):
        big = _boxed_line("GET UP", (257, 360, 162, 42), confidence=0.99)
        far = _boxed_line("SO", (50, 1200, 66, 43), confidence=0.78)
        group = self._speech_group_from([big])
        candidate = TextCandidate(line=far, ignored=True,
                                  ignore_reason="noise_like_text")
        _reclaim_short_lexical_lines([group], [candidate], (1374, 690, 3))
        self.assertNotIn("SO", group.text)
        self.assertTrue(candidate.ignored)

    def test_consonant_cluster_noise_is_not_reclaimed(self):
        # A vowel-less fragment ("MM") is OCR noise, never a reclaimed word.
        big = _boxed_line("SORRY", (257, 300, 162, 42), confidence=0.99)
        mm = _boxed_line("MM", (300, 346, 66, 43), confidence=0.78)
        group = self._speech_group_from([big])
        candidate = TextCandidate(line=mm, ignored=True,
                                  ignore_reason="noise_like_text")
        _reclaim_short_lexical_lines([group], [candidate], (1374, 690, 3))
        self.assertNotIn("MM", group.text)
        self.assertTrue(candidate.ignored)

    def test_single_char_fragment_is_not_merged_as_text(self):
        # A one-character OCR fragment must not be merged as lexical text (it
        # would corrupt the translation); it is left for cleanup/review.
        big = _boxed_line("YOU STILL", (145, 382, 203, 38), confidence=0.99)
        frag = _boxed_line("H", (199, 336, 93, 44), confidence=0.78)
        group = self._speech_group_from([big])
        candidate = TextCandidate(line=frag, ignored=True,
                                  ignore_reason="too_few_useful_chars")
        _reclaim_short_lexical_lines([group], [candidate], (1374, 690, 3))
        self.assertEqual(len(group.lines), 1)
        self.assertTrue(candidate.ignored)

    def test_interrupted_speech_fragment_is_reclaimed(self):
        # A one-letter utterance cut off by a dash is real speech continuing the
        # balloon; it must join the group instead of being dropped for having
        # too few letters.
        big = _boxed_line("LADY BRELOFF,", (275, 1137, 192, 29), confidence=0.96)
        frag = _boxed_line("I--", (350, 1174, 43, 31), confidence=0.73)
        group = self._speech_group_from([big])
        candidate = TextCandidate(line=frag, ignored=True,
                                  ignore_reason="too_few_useful_chars")
        _reclaim_short_lexical_lines([group], [candidate], (1500, 690, 3))
        self.assertIn("I--", group.text)
        self.assertFalse(candidate.ignored)

    def test_under_read_wide_box_fragment_is_not_reclaimed(self):
        # The box is far wider than the single recognised glyph, so the engine
        # only read part of the line. Merging that partial text would corrupt the
        # speech, so it must not be reclaimed (coverage reports it instead).
        big = _boxed_line("YOU STILL", (145, 382, 203, 38), confidence=0.99)
        frag = _boxed_line("H", (199, 336, 93, 44), confidence=0.92)
        group = self._speech_group_from([big])
        candidate = TextCandidate(line=frag, ignored=True,
                                  ignore_reason="too_few_useful_chars")
        _reclaim_short_lexical_lines([group], [candidate], (1500, 690, 3))
        self.assertEqual(len(group.lines), 1)
        self.assertTrue(candidate.ignored)

    def test_bare_single_letter_is_not_reclaimed(self):
        # A lone letter with no interrupting punctuation is ambiguous (a roman
        # numeral, an initial, a decorative glyph) and must stay out of speech.
        big = _boxed_line("HELLO THERE", (275, 1137, 192, 29), confidence=0.96)
        frag = _boxed_line("I", (350, 1174, 20, 31), confidence=0.90)
        group = self._speech_group_from([big])
        candidate = TextCandidate(line=frag, ignored=True,
                                  ignore_reason="too_few_useful_chars")
        _reclaim_short_lexical_lines([group], [candidate], (1500, 690, 3))
        self.assertEqual(len(group.lines), 1)
        self.assertTrue(candidate.ignored)

    def test_container_confirmed_by_near_total_coverage(self):
        # A balloon drawn on a light page merges with the background, so its
        # region is never marked enclosed even though the text sits almost
        # entirely inside it. Near-total coverage confirms the container.
        big = _boxed_line("LADY BRELOFF,", (275, 1137, 192, 29), confidence=0.96)
        big.metadata = {"visual_white_region_id": 1,
                        "visual_white_region_enclosed": False,
                        "visual_white_region_coverage": 0.9849}
        frag = _boxed_line("I--", (350, 1174, 43, 31), confidence=0.73)
        frag.metadata = {"visual_white_region_id": 1,
                         "visual_white_region_enclosed": False,
                         "visual_white_region_coverage": 1.0}
        group = _group_lines([big])[0]
        candidate = TextCandidate(line=frag, ignored=True,
                                  ignore_reason="too_few_useful_chars")
        _reclaim_short_lexical_lines([group], [candidate], (1500, 690, 3))
        self.assertIn("I--", group.text)

    def test_partial_coverage_shout_on_art_is_not_a_container(self):
        # Stylized shouts lettered on open art keep only partial coverage; they
        # must not count as a container, so nothing is merged into them.
        big = _boxed_line("AGH", (412, 539, 206, 111), confidence=0.99)
        big.metadata = {"visual_white_region_id": 1,
                        "visual_white_region_enclosed": False,
                        "visual_white_region_coverage": 0.7394}
        frag = _boxed_line("A--", (430, 660, 60, 100), confidence=0.90)
        frag.metadata = {"visual_white_region_id": 1,
                         "visual_white_region_enclosed": False,
                         "visual_white_region_coverage": 0.7851}
        group = _group_lines([big])[0]
        candidate = TextCandidate(line=frag, ignored=True,
                                  ignore_reason="noise_like_text")
        _reclaim_short_lexical_lines([group], [candidate], (1500, 690, 3))
        self.assertEqual(len(group.lines), 1)
        self.assertTrue(candidate.ignored)

    def test_interrupted_fragment_outside_container_stays_dropped(self):
        # The same shape drawn on open art (no confirmed container) is not speech.
        big = _boxed_line("SOME WORDS", (275, 1137, 192, 29), confidence=0.96)
        frag = _boxed_line("I--", (350, 1174, 43, 31), confidence=0.73)
        group = self._speech_group_from([big], enclosed=False)
        candidate = TextCandidate(line=frag, ignored=True,
                                  ignore_reason="too_few_useful_chars")
        _reclaim_short_lexical_lines([group], [candidate], (1500, 690, 3))
        self.assertEqual(len(group.lines), 1)
        self.assertTrue(candidate.ignored)

    def test_reclaim_respects_distinct_enclosed_container(self):
        # A short line inside a different *enclosed* balloon must not be pulled
        # into a neighbouring balloon's group.
        big = _boxed_line("GET UP", (257, 360, 162, 42), confidence=0.99)
        big.metadata = {"visual_white_region_id": 1,
                        "visual_white_region_enclosed": True}
        short = _boxed_line("SO", (303, 314, 66, 43), confidence=0.78)
        short.metadata = {"visual_white_region_id": 2,
                          "visual_white_region_enclosed": True}
        group = self._speech_group_from([big])
        candidate = TextCandidate(line=short, ignored=True,
                                  ignore_reason="noise_like_text")
        _reclaim_short_lexical_lines([group], [candidate], (1374, 690, 3))
        self.assertNotIn("SO", group.text)
        self.assertTrue(candidate.ignored)

    # ---- RC2: sibling lines split by weak visual white regions ----

    @staticmethod
    def _region_line(text, box, region_id, enclosed):
        line = _boxed_line(text, box, confidence=0.95)
        line.metadata = {
            "visual_white_region_id": region_id,
            "visual_white_region_enclosed": enclosed,
        }
        return line

    # ---- horizontal speech siblings on the same reading row ----

    def test_same_row_speech_fragment_joins_group(self):
        # A fragment sitting beside the line on the same baseline, in the same
        # container and at a comparable text height, is the tail of that speech
        # and must join it instead of becoming its own group.
        first = self._region_line("GOD D*** j", (180, 167, 277, 59), 1, False)
        second = self._region_line("It!!", (440, 176, 80, 46), 1, False)
        group = _group_lines([first])[0]
        self.assertTrue(_line_belongs_to_group(second, group))

    def test_side_by_side_balloons_are_not_merged(self):
        # Two distinct enclosed balloons on the same row stay separate.
        first = self._region_line("HELLO THERE", (180, 167, 277, 59), 1, True)
        second = self._region_line("GOODBYE NOW", (470, 176, 240, 55), 2, True)
        group = _group_lines([first])[0]
        self.assertFalse(_line_belongs_to_group(second, group))

    def test_large_sfx_beside_speech_is_not_merged(self):
        # A sound effect lettered much larger than the speech is not a sibling.
        first = self._region_line("GOD D*** j", (180, 167, 277, 59), 1, False)
        sfx = self._region_line("CRASH", (470, 120, 260, 190), 1, False)
        group = _group_lines([first])[0]
        self.assertFalse(_line_belongs_to_group(sfx, group))

    def test_open_art_text_on_same_row_is_not_merged(self):
        # Title/logo and credit elements sit on open art, inside no container, so
        # they must never be pulled together even when they share a reading row.
        logo = _boxed_line("Platfopim", (127, 1118, 265, 81), confidence=0.90)
        effect = _boxed_line("Lero", (383, 1135, 185, 71), confidence=0.90)
        group = _group_lines([logo])[0]
        self.assertFalse(_line_belongs_to_group(effect, group))

    def test_distant_same_row_text_is_not_merged(self):
        # Same row but far away across the panel: not part of this speech.
        first = self._region_line("GOD D*** j", (180, 167, 277, 59), 1, False)
        far = self._region_line("It!!", (640, 176, 80, 46), 1, False)
        group = _group_lines([first])[0]
        self.assertFalse(_line_belongs_to_group(far, group))

    def test_weak_region_lines_merge_into_one_group(self):
        # Stacked, aligned speech lines that a weak (non-enclosed) white-region
        # detector split into one region per line must still form a single group
        # so the whole balloon is translated together.
        lines = [
            self._region_line("IF YOU GET BITTEN", (167, 477, 376, 44), 1, False),
            self._region_line("BY ONE OF THOSE", (172, 534, 367, 44), 8, False),
            self._region_line("THINGS YOU BECOME", (138, 588, 433, 47), 14, False),
            self._region_line("A MONSTER TOO", (177, 644, 353, 47), 17, False),
        ]
        groups = _group_lines(lines)
        self.assertEqual(len(groups), 1, [g.text for g in groups])
        self.assertEqual(len(groups[0].lines), 4)

    def test_distinct_enclosed_regions_stay_separate(self):
        # Two confirmed enclosed balloons that happen to be stacked must not be
        # merged: a real container boundary still separates them.
        lines = [
            self._region_line("HELLO THERE FRIEND", (167, 477, 376, 44), 1, True),
            self._region_line("GOODBYE FOR NOW", (172, 534, 367, 44), 8, True),
        ]
        groups = _group_lines(lines)
        self.assertEqual(len(groups), 2)

    def test_same_region_lines_still_group(self):
        # Regression guard: lines sharing a region id keep grouping as before.
        lines = [
            self._region_line("FIRST LINE HERE", (167, 477, 376, 44), 5, False),
            self._region_line("SECOND LINE HERE", (172, 534, 367, 44), 5, False),
        ]
        groups = _group_lines(lines)
        self.assertEqual(len(groups), 1)

    def test_adversarial_spanish_leakage_and_legitimate_foreign_controls(self):
        invalid_cases = [
            ("MAYBE IT'S THIS WAY.", "QUIZÁS SEJA POR AQUI."),
            (
                "MAYBE THERE WAS A DISASTER.",
                "QUIZÁS TIVOU ALGUM DESASTRE.",
            ),
            ("MAYBE, BUT I DON'T KNOW.", "TALVEZ, PERO EU NÃO SEI."),
            (
                "I DON'T KNOW WHY THIS HAPPENED.",
                "EU NÃO SEI POR QUÉ ISSO ACONTECEU.",
            ),
            ("SHE IS VERY FAR AWAY.", "ELA ESTÁ MUY LONGE."),
            ("I WILL LEAVE, THEN.", "EU VOU EMBORA, ENTONCES."),
        ]
        for source, translation in invalid_cases:
            with self.subTest(translation=translation):
                valid, reason = validate_translation_text(
                    source,
                    translation,
                    "speech",
                )
                self.assertFalse(valid)
                self.assertTrue(reason)

        legitimate_cases = [
            "MEU NOME É DIEGO.",
            "ELA FALOU “HOLA” E FOI EMBORA.",
            "A PLACA DIZIA “SALIDA”.",
            "VAMOS AO EL DORADO.",
            "ELE ESTÁ LENDO DON QUIXOTE.",
            "O RESTAURANTE SE CHAMA CASA DEL SOL.",
            "“BUENOS DÍAS”, DISSE O TURISTA.",
            "A MÚSICA SE CHAMA HASTA SIEMPRE.",
            "SAN MARTÍN CHEGOU CEDO.",
            "MIGUEL DE CERVANTES ESCREVEU O LIVRO.",
            "EU NÃO SEI POR QUÊ.",
        ]
        for translation in legitimate_cases:
            with self.subTest(translation=translation):
                valid, reason = validate_translation_text(
                    "Portuguese sentence with a deliberate foreign term.",
                    translation,
                    "speech",
                )
                self.assertTrue(valid, reason)

        full_spanish_valid, full_spanish_reason = validate_translation_text(
            "MAYBE WE SHOULD LEAVE NOW.",
            "QUIZÁS DEBERÍAMOS IRNOS AHORA.",
            "speech",
        )
        self.assertFalse(full_spanish_valid)
        self.assertTrue(
            full_spanish_reason.startswith("residual_spanish_token"),
            full_spanish_reason,
        )

    def test_adversarial_partial_fragments_preserve_valid_ptbr_hyphenation(self):
        invalid_cases = [
            ("Sh-She'S COMING!", "Sh-Ela está vindo!"),
            ("Th-The answer is wrong!", "Th-Eu não sei!"),
            ("Wh-What do you want?", "Wh-O que você quer?"),
            ("I-I need to leave.", "I-Eu preciso sair."),
            ("She-She came back.", "She-Ela voltou."),
            ("Ru-Run now!", "Ru-Corra agora!"),
        ]
        for source, translation in invalid_cases:
            with self.subTest(translation=translation):
                valid, reason = validate_translation_text(
                    source,
                    translation,
                    "speech",
                )
                self.assertFalse(valid)
                self.assertTrue(reason)

        valid_stutters = [
            "N-NÃO!",
            "E-EU NÃO SEI...",
            "A-AQUELE HOMEM!",
            "P-POR FAVOR...",
            "M-MAS EU VI!",
            "V-VOCÊ ESTÁ BEM?",
            "M-MARIA?",
            "J-JOÃO!",
            "S-SHIHAE?",
            "D-DIEGO!",
            "M-MIGUEL...",
        ]
        for translation in valid_stutters:
            with self.subTest(translation=translation):
                valid, reason = validate_translation_text(
                    translation,
                    translation,
                    "speech",
                )
                self.assertTrue(valid, reason)

        valid_hyphenation = [
            "GUARDA-CHUVA",
            "EX-NAMORADO",
            "BEM-VINDO",
            "RECÉM-CHEGADO",
            "SEGUNDA-FEIRA",
            "EU DISSE: NÃO-ME-TOQUE.",
        ]
        for translation in valid_hyphenation:
            with self.subTest(translation=translation):
                valid, reason = validate_translation_text(
                    "Portuguese compound expression.",
                    translation,
                    "speech",
                )
                self.assertTrue(valid, reason)

    def test_leading_hyphenated_fragment_accepts_optional_whitespace(self):
        cases = [
            "Sh-Ela está vindo!",
            "Sh- Ela está vindo!",
            "Sh -Ela está vindo!",
            "Sh - Ela está vindo!",
            "Sh-\tEla está vindo!",
            "Sh-\u00a0Ela está vindo!",
            "Sh-\u2009Ela está vindo!",
        ]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.assertEqual(
                    _leading_hyphenated_fragment(candidate),
                    ("SH", "ELA"),
                )

        for candidate in (
            "Sh\u2011Ela está vindo!",
            "Sh–Ela está vindo!",
            "Sh—Ela está vindo!",
        ):
            with self.subTest(unsupported_hyphen=candidate):
                self.assertIsNone(_leading_hyphenated_fragment(candidate))

    def test_hyphen_fragment_accepts_explicit_horizontal_unicode_whitespace(self):
        horizontal_whitespace = [
            " ",
            "\t",
            "\u00a0",
            "\u1680",
            *(chr(codepoint) for codepoint in range(0x2000, 0x200B)),
            "\u202f",
            "\u205f",
            "\u3000",
        ]
        for separator in horizontal_whitespace:
            candidate = f"Sh{separator}-{separator}Ela está vindo!"
            with self.subTest(separator=ascii(separator)):
                self.assertEqual(
                    _leading_hyphenated_fragment(candidate),
                    ("SH", "ELA"),
                )

    def test_hyphen_fragment_does_not_cross_vertical_separators(self):
        vertical_separators = [
            "\v",
            "\f",
            "\u0085",
            "\u2028",
            "\u2029",
            "\n",
            "\r",
        ]
        for separator in vertical_separators:
            candidate = f"Sh{separator}-{separator}Ela está vindo!"
            with self.subTest(separator=ascii(separator)):
                self.assertIsNone(_leading_hyphenated_fragment(candidate))

    def test_spaced_partial_source_fragments_are_rejected(self):
        sources = [
            "Sh-She'S COMING!",
            "Sh - She'S COMING!",
            "Sh-\tShe'S COMING!",
            "Sh-\u00a0She'S COMING!",
        ]
        candidates = [
            "Sh-Ela está vindo!",
            "Sh- Ela está vindo!",
            "Sh -Ela está vindo!",
            "Sh - Ela está vindo!",
            "Sh-\tEla está vindo!",
            "Sh-\u00a0Ela está vindo!",
            "Sh-\u2009Ela está vindo!",
        ]
        for source in sources:
            for candidate in candidates:
                with self.subTest(source=source, candidate=candidate):
                    valid, reason = validate_translation_text(
                        source,
                        candidate,
                        "speech",
                    )
                    self.assertFalse(valid)
                    self.assertTrue(
                        reason.startswith("multilingual_partial_translation"),
                        reason,
                    )

    def test_spaced_partial_fragment_retry_accepts_fully_localized_candidate(self):
        group = _scored_group("Sh-She'S COMING!")
        apply_group_translations([group], ["Sh- Ela tá vindo!"])
        self.assertFalse(group.translation_valid)
        self.assertTrue(
            group.translation_validation_reason.startswith(
                "multilingual_partial_translation"
            )
        )

        translator = _StrictRetryTranslator("Ela está vindo!")
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            records = validate_and_retry_translations([group], translator)

        self.assertEqual(len(translator.calls), 1)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["valid"], records)
        self.assertEqual(records[0]["reason"], "ok")
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation_validation_reason, "retry_ok")
        self.assertEqual(group.translation, "Ela está vindo!")
        self.assertFalse(group.manual_review_required)

    def test_spaced_partial_fragment_failed_retries_require_manual_review(self):
        retry_candidates = [
            "Sh - Ela está vindo!",
            "Sh-Ela está vindo!",
        ]
        for retry_candidate in retry_candidates:
            with self.subTest(retry_candidate=retry_candidate):
                group = _scored_group("Sh-She'S COMING!")
                apply_group_translations([group], ["Sh- Ela tá vindo!"])
                translator = _StrictRetryTranslator(retry_candidate)
                with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
                    records = validate_and_retry_translations([group], translator)

                self.assertEqual(len(records), 1)
                self.assertFalse(records[0]["valid"], records)
                self.assertTrue(
                    records[0]["reason"].startswith(
                        "multilingual_partial_translation"
                    ),
                    records,
                )
                self.assertFalse(group.translation_valid)
                self.assertTrue(group.manual_review_required)
                self.assertEqual(group.rejected_translation, "Sh- Ela tá vindo!")
                self.assertEqual(group.translation, group.text)

        debug_data = _debug_payload("", group.lines, [], [group])
        item = debug_data["items"][0]
        self.assertTrue(
            item["translation_validation_reason"].startswith(
                "multilingual_partial_translation"
            )
        )
        self.assertTrue(item["manual_review_required"])
        self.assertEqual(item["rejected_translation"], "Sh- Ela tá vindo!")
        states = [
            {
                "index": 1,
                "status": "processed",
                "output_path": "",
                "image_path": "",
                "timings": {},
                "debug_data": debug_data,
            }
        ]
        quality_report = _build_quality_report({}, states, records)
        aggregate = _aggregate_debug_data(states)
        self.assertEqual(quality_report["totals"]["mixed_language_items"], 1)
        self.assertEqual(aggregate["mixed_language_items"], 1)

    def test_spaced_ptbr_stutters_names_and_compounds_remain_valid(self):
        valid_stutters_and_names = [
            "N-Não!",
            "N- Não!",
            "N -Não!",
            "N - Não!",
            "E-Eu não sei...",
            "E- Eu não sei...",
            "A-Aquele homem!",
            "P- Por favor...",
            "M - Mas eu vi!",
            "V - Você está bem?",
            "M-Maria?",
            "M- Maria?",
            "M - Maria?",
            "J-João!",
            "S-Shihae?",
            "S- Shihae?",
            "D- Diego!",
            "M - Miguel...",
        ]
        for candidate in valid_stutters_and_names:
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(
                    candidate,
                    candidate,
                    "speech",
                    ["MARIA", "JOÃO", "SHIHAE", "DIEGO", "MIGUEL"],
                )
                self.assertTrue(valid, reason)

        valid_compounds = [
            "GUARDA-CHUVA",
            "GUARDA - CHUVA",
            "EX-NAMORADO",
            "EX - NAMORADO",
            "BEM-VINDO",
            "BEM - VINDO",
            "RECÉM-CHEGADO",
            "SEGUNDA-FEIRA",
            "NÃO-ME-TOQUE",
        ]
        for candidate in valid_compounds:
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(
                    "Portuguese compound expression.",
                    candidate,
                    "speech",
                )
                self.assertTrue(valid, reason)

    def test_dialogue_dashes_terms_and_censorship_remain_valid(self):
        for candidate in (
            "— Ela está vindo!",
            "– Ela está vindo!",
            "X-23",
            "X - 23",
            "Wi-Fi",
            "Wi - Fi",
            "COVID-19",
            "T-800",
            "B-52",
        ):
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(
                    "Localized dialogue or technical term.",
                    candidate,
                    "speech",
                )
                self.assertTrue(valid, reason)

        for candidate in ("S***!", "F***!", "SH**!", "S-***!"):
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(
                    candidate,
                    candidate,
                    "sfx",
                )
                self.assertTrue(valid, reason)
                self.assertEqual(reason, "sfx_preserved")

    def test_hyphen_whitespace_change_does_not_affect_other_validators(self):
        controls = [
            (
                "MAYBE IT'S THIS WAY.",
                "QUIZÁS SEJA POR AQUI.",
                False,
                "residual_spanish_token",
            ),
            (
                "MAYBE THERE WAS SOME SORT OF DISASTER.",
                "QUIZÁS TIVOU ALGUM DESASTRE.",
                False,
                "residual_spanish_token",
            ),
            ("I MUST'VE FALLEN ASLEEP.", "DEVO TER ADORMIDO.", True, "ok"),
            ("OW, MY BUTT...", "AI, MEU BUMBUM..", True, "ok"),
            ("RUN!", "SÓ CORRA.", True, "ok"),
            ("IF NECESSARY, I WILL GO.", "SE FOR PRECISO, EU VOU.", True, "ok"),
            (
                "JESus... IS THAT ALL BLOOD?!",
                "JESUS... É TODO SANGUE?!",
                True,
                "ok",
            ),
        ]
        for source, candidate, expected_valid, expected_reason in controls:
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(
                    source,
                    candidate,
                    "speech",
                )
                self.assertEqual(valid, expected_valid, reason)
                if expected_reason == "ok":
                    self.assertEqual(reason, expected_reason)
                else:
                    self.assertTrue(reason.startswith(expected_reason), reason)

    def test_adversarial_censorship_tokens_remain_preservable(self):
        for text in ("S***!", "F***!", "SH**!"):
            with self.subTest(text=text):
                valid, reason = validate_translation_text(
                    text,
                    text,
                    "sfx",
                )
                self.assertTrue(valid, reason)
                self.assertEqual(reason, "sfx_preserved")

    def test_full_english_speech_or_narration_translation_is_rejected(self):
        cases = [
            ("WHERE THE HELL IS SHIHAE STATION, ANYWAY?!", "speech"),
            ("RUN!!", "speech"),
            ("RUN, RUN!!", "speech"),
            ("BLOOD...!", "narration"),
        ]
        for translation, classification in cases:
            with self.subTest(translation=translation):
                valid, reason = validate_translation_text(
                    translation,
                    translation,
                    classification,
                )
                self.assertFalse(valid)
                self.assertTrue(reason)

    def test_common_short_english_words_are_rejected_in_translatable_context(self):
        cases = [
            ("FOG?", "speech"),
            ("HEY...", "speech"),
            ("WHOA!!!", "speech"),
            ("EEK!", "speech"),
        ]
        for translation, classification in cases:
            with self.subTest(translation=translation):
                valid, reason = validate_translation_text(
                    translation,
                    translation,
                    classification,
                )
                self.assertFalse(valid)
                self.assertTrue(reason)

    def test_residual_pdf_lexical_garbage_is_flagged_as_suspicious_ocr(self):
        cases = [
            "iiON",
            "ENO DNIOD",
            "SÓ O QUE NA TERRA É ENO DNIOD",
        ]
        for text in cases:
            with self.subTest(text=text):
                group = _scored_group(text)
                self.assertTrue(
                    group_needs_selective_fallback(group),
                    group.quality_reasons,
                )

    def test_residual_english_validation_reasons_are_reported_as_mixed_items(self):
        reasons = [
            "mixed_language_tokens:HERE",
            "residual_english_token:TRAIN",
            "residual_inflected_english:DRINKS",
            "untranslated_english_text:RUN",
            "untranslated_single_english_token:FOG",
        ]
        items = [
            {
                "id": f"T{index}",
                "translation_validation_reason": reason,
            }
            for index, reason in enumerate(reasons, start=1)
        ]
        states = [
            {
                "index": 1,
                "status": "processed",
                "output_path": "",
                "image_path": "",
                "timings": {},
                "debug_data": {
                    "items": items,
                    "selective_ocr_fallbacks": [],
                    "classification_counts": {},
                },
            }
        ]

        quality_report = _build_quality_report({}, states, [])
        aggregate = _aggregate_debug_data(states)

        self.assertEqual(quality_report["totals"]["mixed_language_items"], len(reasons))
        self.assertEqual(
            len(quality_report["pages"][0]["mixed_language_items"]),
            len(reasons),
        )
        self.assertEqual(aggregate["mixed_language_items"], len(reasons))

    def test_runtime_retry_corrects_residual_english_token_translation(self):
        group = _scored_group("I MISSED THE LAST TRAIN!")
        apply_group_translations([group], ["EU PERDI O ULTIMO TRAIN!"])
        self.assertFalse(group.translation_valid)
        self.assertTrue(
            group.translation_validation_reason.startswith("residual_english_token"),
            group.translation_validation_reason,
        )

        translator = _StrictRetryTranslator("EU PERDI O ULTIMO TREM!")
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 2):
            records = validate_and_retry_translations([group], translator, force=True)

        self.assertEqual(len(translator.calls), 1)
        self.assertEqual(translator.calls[0]["previous_translation"], "EU PERDI O ULTIMO TRAIN!")
        self.assertTrue(
            translator.calls[0]["validation_reason"].startswith("residual_english_token"),
            translator.calls[0]["validation_reason"],
        )
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["valid"], records)
        self.assertEqual(records[0]["reason"], "ok")
        self.assertEqual(group.translation_retry_count, 1)
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation_validation_reason, "retry_ok")
        self.assertEqual(group.translation, "EU PERDI O ULTIMO TREM!")
        self.assertFalse(group.manual_review_required)
        self.assertFalse(group.rejected_translation)

    def test_runtime_retry_corrects_mixed_language_translation(self):
        group = _scored_group("SH-SHE'S COMING!")
        apply_group_translations([group], ["Sh-She's VINDO!"])
        self.assertFalse(group.translation_valid)
        self.assertTrue(group.translation_validation_reason)

        translator = _StrictRetryTranslator("ELA ESTA VINDO!")
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 2):
            records = validate_and_retry_translations([group], translator)

        self.assertEqual(len(translator.calls), 1)
        self.assertEqual(translator.calls[0]["previous_translation"], "SH-SHE'S VINDO!")
        self.assertTrue(translator.calls[0]["validation_reason"])
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["valid"], records)
        self.assertEqual(group.translation_retry_count, 1)
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation_validation_reason, "retry_ok")
        self.assertEqual(group.translation, "ELA ESTA VINDO!")
        self.assertFalse(group.manual_review_required)

    def test_runtime_failed_retry_preserves_original_and_reports_manual_review(self):
        group = _scored_group("THIS PLACE IS HELL!!")
        apply_group_translations([group], ["ESTE LUGAR E HELL!!"])
        self.assertFalse(group.translation_valid)
        initial_reason = group.translation_validation_reason
        self.assertTrue(initial_reason)

        translator = _StrictRetryTranslator("THIS PLACE E HELL!!")
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            records = validate_and_retry_translations([group], translator)

        self.assertEqual(len(translator.calls), 1)
        self.assertEqual(translator.calls[0]["validation_reason"], initial_reason)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["valid"], records)
        self.assertTrue(records[0]["reason"])
        self.assertFalse(group.translation_valid)
        self.assertTrue(group.manual_review_required)
        self.assertEqual(group.rejected_translation, "ESTE LUGAR E HELL!!")
        self.assertEqual(group.translation, group.text)
        self.assertFalse(_should_translate_group(group))

        item = {
            "id": group.group_id,
            "translation_validation_reason": group.translation_validation_reason,
            "manual_review_required": group.manual_review_required,
            "rejected_translation": group.rejected_translation,
        }
        states = [
            {
                "index": 1,
                "status": "processed",
                "output_path": "",
                "image_path": "",
                "timings": {},
                "debug_data": {
                    "items": [item],
                    "selective_ocr_fallbacks": [],
                    "classification_counts": {},
                },
            }
        ]
        quality_report = _build_quality_report({}, states, records)
        aggregate = _aggregate_debug_data(states)
        self.assertEqual(quality_report["totals"]["manual_review_required_groups"], 1)
        self.assertEqual(quality_report["totals"]["translations_rejected"], 1)
        self.assertEqual(quality_report["totals"]["mixed_language_items"], 1)
        self.assertEqual(len(quality_report["pages"][0]["mixed_language_items"]), 1)
        self.assertEqual(aggregate["manual_review_required_groups"], 1)
        self.assertEqual(aggregate["translation_rejections"], 1)
        self.assertEqual(aggregate["mixed_language_items"], 1)

    def test_missing_translation_candidate_is_explicit_manual_review(self):
        group = _scored_group("THE SIGNAL IS CLEAR.")

        apply_group_translations([group], [])

        self.assertTrue(group.sent_to_translation)
        self.assertFalse(group.translation_valid)
        self.assertEqual(group.translation_validation_reason, "missing_translation_candidate")
        self.assertEqual(group.translation_candidate, "")
        self.assertEqual(group.translation_final_state, "manual_review")
        self.assertTrue(group.manual_review_required)
        self.assertTrue(group.preserved_original)
        self.assertEqual(group.translation, group.text)
        debug_data = _debug_payload("", group.lines, [], [group])
        self.assertEqual(debug_data["translated_group_count"], 0)
        self.assertFalse(debug_data["items"][0]["translated"])

    def test_isolated_retry_recovers_speech_left_in_source_language(self):
        # The strict retry invites the model to keep proper names, so a source
        # word it mistakes for a name survives and the candidate keeps failing.
        # When the group has no detected proper name, nothing justifies leaving a
        # source-language word, so a final isolated attempt demands a full
        # translation and recovers the line.
        group = _scored_group("SHUT IT, Will YoU?")
        apply_group_translations([group], ["Cala a boca, Will!"])
        self.assertFalse(group.translation_valid)
        self.assertTrue(
            group.translation_validation_reason.startswith("mixed_language_tokens")
        )

        translator = _IsolatedRetryTranslator(
            strict="Cala a boca, Will!",
            isolated="Cala a boca, ta bom?",
        )
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            validate_and_retry_translations([group], translator)

        self.assertTrue(translator.isolated_calls, "isolated retry was not attempted")
        self.assertTrue(group.translation_valid, group.translation_validation_reason)
        self.assertEqual(group.translation, "Cala a boca, ta bom?")
        self.assertEqual(group.translation_final_state, "translated")
        self.assertFalse(group.manual_review_required)

    def test_isolated_retry_failure_keeps_manual_review(self):
        # If the isolated attempt still leaves source language, the original is
        # preserved and the region goes to manual review. Nothing is invented.
        group = _scored_group("SHUT IT, Will YoU?")
        apply_group_translations([group], ["Cala a boca, Will!"])

        translator = _IsolatedRetryTranslator(
            strict="Cala a boca, Will!",
            isolated="Cala a boca, Will!",
        )
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            validate_and_retry_translations([group], translator)

        self.assertTrue(translator.isolated_calls)
        self.assertFalse(group.translation_valid)
        self.assertTrue(group.manual_review_required)
        self.assertTrue(group.preserved_original)
        self.assertEqual(group.translation, group.text)

    def test_isolated_retry_is_not_used_when_a_proper_name_is_known(self):
        # A detected proper name legitimately stays in the translation, so the
        # isolated "translate every word" attempt must not run and erase it.
        line = _line("ORION VaLE")
        line.metadata = {"original_text": "ORION VaLE"}
        group = TextGroup(
            group_id="T", lines=[line], text="ORION VALE", classification="speech",
        )
        group.detected_proper_names = ["ORION VALE"]
        apply_group_translations([group], ["ORION VALE"])

        translator = _IsolatedRetryTranslator(strict="ORION VALE", isolated="X")
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            validate_and_retry_translations([group], translator)

        self.assertEqual(translator.isolated_calls, [])

    def test_source_echo_after_retries_has_explicit_terminal_reason(self):
        group = _scored_group("THE SIGNAL IS CLEAR.")
        apply_group_translations([group], [group.text])

        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            records = validate_and_retry_translations(
                [group],
                _StrictRetryTranslator(group.text),
            )

        self.assertEqual(len(records), 1)
        self.assertFalse(group.translation_valid)
        self.assertEqual(
            group.translation_final_reason,
            "untranslated_source_after_retries",
        )
        self.assertEqual(group.translation_final_state, "manual_review")
        self.assertTrue(group.manual_review_required)
        self.assertTrue(group.preserved_original)
        self.assertEqual(group.translation, group.text)

    def test_source_echo_translation_preserves_original_art_without_redraw(self):
        # A group whose accepted translation only echoes the source (e.g. a
        # stylized vocalization or a branding token kept as a name) must not be
        # redrawn: redrawing identical glyphs erases the original art for no
        # benefit and risks corrupting logos/titles.
        group = _scored_group("AGH...")
        group.bounding_box = [40, 80, 200, 80]
        apply_group_translations([group], ["AGH..."])
        self.assertTrue(group.translation_valid, group.translation_validation_reason)
        self.assertIn(group, get_translatable_groups([group]))

        image = np.full((220, 420, 3), 255, dtype=np.uint8)
        cv2.putText(image, "AGH", (50, 140), cv2.FONT_HERSHEY_SIMPLEX, 2,
                    (0, 0, 0), 4)
        original = image.copy()
        final, _ = render_analyzed_image(original, [], [], [group])

        self.assertFalse(group.redrawn)
        self.assertTrue(group.preserved_original)
        self.assertEqual(group.translation_final_state, "preserved_original")
        self.assertTrue(np.array_equal(final, original))

    def test_real_translation_is_still_redrawn(self):
        # Guard against over-preserving: a genuine translation (different from
        # the source once folded) must still be redrawn.
        group = _scored_group("GET UP!")
        group.bounding_box = [40, 80, 200, 80]
        apply_group_translations([group], ["LEVANTA!"])
        self.assertTrue(group.translation_valid, group.translation_validation_reason)

        image = np.full((220, 420, 3), 255, dtype=np.uint8)
        cv2.putText(image, "GET UP", (50, 140), cv2.FONT_HERSHEY_SIMPLEX, 2,
                    (0, 0, 0), 4)
        original = image.copy()
        render_analyzed_image(original, [], [], [group])

        self.assertTrue(group.redrawn)
        self.assertFalse(group.preserved_original)
        self.assertEqual(group.translation_final_state, "translated")

    def test_accented_translation_is_not_treated_as_source_echo(self):
        # 'No...' -> 'Não...' is a real translation; ascii-folding must keep the
        # base letters so it is not mistaken for an untouched source echo.
        group = _scored_group("No...")
        group.bounding_box = [40, 80, 200, 80]
        apply_group_translations([group], ["Não..."])
        self.assertTrue(group.translation_valid, group.translation_validation_reason)

        image = np.full((220, 420, 3), 255, dtype=np.uint8)
        cv2.putText(image, "No", (50, 140), cv2.FONT_HERSHEY_SIMPLEX, 2,
                    (0, 0, 0), 4)
        render_analyzed_image(image.copy(), [], [], [group])
        self.assertTrue(group.redrawn)

    def test_unknown_source_echo_is_rejected_before_retry(self):
        valid, reason = validate_translation_text("QELON", "QELON", "speech")

        self.assertFalse(valid)
        self.assertEqual(reason, "candidate_equals_source")

    def test_unknown_source_echo_normalizes_case_spacing_punctuation_and_quotes(self):
        for candidate in (
            "qelon",
            " QELON ",
            '"QELON"',
            "\u201cQELON\u201d",
            "QELON!",
        ):
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(
                    "QELON", candidate, "speech"
                )
                self.assertFalse(valid)
                self.assertEqual(reason, "candidate_equals_source")

    def test_explicit_proper_name_tokens_are_allowed_in_translation(self):
        valid, reason = validate_translation_text(
            "ASTRA VALE ARRIVED.",
            "ASTRA VALE CHEGOU.",
            "speech",
            ["ASTRA VALE"],
        )

        self.assertTrue(valid, reason)

    def test_mixed_case_ocr_name_evidence_preserves_matching_candidate(self):
        line = _line("ORION VaLE")
        line.metadata = {"original_text": "ORION VaLE"}
        group = TextGroup(
            group_id="T",
            lines=[line],
            text="ORION VALE",
            classification="speech",
        )

        apply_group_translations([group], ["ORION VALE"])

        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation_validation_reason, "ok")

    def test_nonlexical_vocalization_is_preserved_without_a_translation_retry(self):
        valid, reason = validate_translation_text("OOH!", "OOH!", "speech")

        self.assertTrue(valid, reason)
        self.assertEqual(reason, "ok")

    def test_linguistic_decorative_text_requires_manual_review(self):
        group = TextGroup(
            group_id="T",
            lines=[_line("A BRIGHT MESSAGE!")],
            text="A BRIGHT MESSAGE!",
            ignored=True,
            ignore_reason="decorative_text",
            classification="decorative",
        )
        _score_group_quality([group])

        self.assertTrue(group.manual_review_required)
        self.assertIn("ignored_decorative_linguistic_content", group.quality_reasons)
        debug_data = _debug_payload("", group.lines, [], [group])
        item = debug_data["items"][0]
        self.assertEqual(item["translation_final_state"], "manual_review")
        self.assertEqual(item["translation_quality_impact"], "review_required")

    def test_cross_region_resolution_bonus_requires_clean_high_quality_candidate(self):
        original = _scored_group("ONE TWO THREE FOUR FIVE")
        original.quality_reasons.append("possible_cross_region_group")
        clean = _scored_group("TWO THREE FOUR FIVE")
        clean.quality_score = 0.90
        clean.quality_reasons = []

        self.assertGreater(
            _cross_region_resolution_bonus(original, clean, 0.75),
            0.0,
        )
        clean.quality_score = 0.81
        self.assertEqual(_cross_region_resolution_bonus(original, clean, 0.75), 0.0)
        clean.quality_score = 0.90
        self.assertEqual(_cross_region_resolution_bonus(original, clean, 0.69), 0.0)
        clean.quality_reasons = ["possible_cross_region_group"]
        self.assertEqual(_cross_region_resolution_bonus(original, clean, 0.75), 0.0)

    def test_clean_regional_reading_beats_cross_region_source_agreement(self):
        original = TextGroup(
            group_id="T",
            lines=[_boxed_line("ALPHA BRAVO CHARLIE DELTA", (80, 80, 230, 126))],
            text="ALPHA BRAVO CHARLIE DELTA",
            classification="narration",
            inside_narration_box_like_region=True,
            source_engine="rapidocr",
        )
        original.quality_score = 0.65
        original.quality_reasons = ["possible_cross_region_group"]
        mobile = _regional_candidate(
            "ALPHA BRAVO CHARLIE DELTA",
            "paddle_mobile",
            0.90,
        )
        mobile.quality_score = 0.75
        full = _regional_candidate(
            "BRAVO CHARLIE DELTA",
            "paddle_full",
            0.95,
        )
        full.quality_score = 0.95

        selected_lines, records = _run_fake_selective_fallback(
            original,
            [mobile, full],
        )

        self.assertEqual(
            " ".join(line.text for line in selected_lines),
            "BRAVO CHARLIE DELTA",
        )
        self.assertEqual(records[0]["fallback_variant"], "paddle_full")
        self.assertGreater(
            records[0]["attempts"][1]["cross_region_resolution_bonus"],
            0.0,
        )

    def test_quality_report_accounts_for_every_translatable_terminal_state(self):
        states = [
            {
                "index": 1,
                "status": "completed",
                "output_path": "",
                "image_path": "",
                "timings": {},
                "debug_data": {
                    "group_count": 3,
                    "items": [
                        {
                            "id": "T1",
                            "classification": "speech",
                            "sent_to_nvidia": True,
                            "translation_candidate": "SINAL LIMPO.",
                            "translation_final_state": "translated",
                            "translation_final_reason": "ok",
                            "translation_valid": True,
                            "redrawn": True,
                            "preserved_original": False,
                            "manual_review_required": False,
                        },
                        {
                            "id": "T2",
                            "classification": "narration",
                            "sent_to_nvidia": True,
                            "translation_candidate": "THE SIGNAL IS CLEAR.",
                            "translation_final_state": "manual_review",
                            "translation_final_reason": "untranslated_source_after_retries",
                            "translation_valid": False,
                            "redrawn": False,
                            "preserved_original": True,
                            "manual_review_required": True,
                        },
                        {
                            "id": "T3",
                            "classification": "sfx",
                            "sent_to_nvidia": False,
                            "translation_final_state": "skipped_with_reason",
                            "translation_final_reason": "sfx_preserved",
                            "translation_valid": True,
                            "redrawn": False,
                            "preserved_original": True,
                            "manual_review_required": False,
                        },
                    ],
                },
            }
        ]

        quality_report = _build_quality_report(
            {"quality_validation": {"passed": False}}, states, []
        )
        accounting = quality_report["totals"]["translation_accounting"]

        self.assertEqual(accounting["detected_translatable"], 2)
        self.assertEqual(accounting["sent_to_translation"], 2)
        self.assertEqual(accounting["translated"], 1)
        self.assertEqual(accounting["manual_review"], 1)
        self.assertEqual(accounting["missing_terminal_state"], 0)
        self.assertTrue(accounting["accounting_closed"])
        self.assertTrue(accounting["requires_review"])

    def test_single_english_token_rejected_only_in_translatable_context(self):
        valid, reason = validate_translation_text("TRAIN", "TRAIN", "speech")
        self.assertFalse(valid)
        self.assertTrue(
            reason.startswith("untranslated_single_english_token"),
            reason,
        )

        sfx_valid, sfx_reason = validate_translation_text("BOOM", "BOOM", "sfx")
        self.assertTrue(sfx_valid, sfx_reason)
        self.assertEqual(sfx_reason, "sfx_preserved")

        decorative_valid, decorative_reason = validate_translation_text(
            "TRAIN",
            "TRAIN",
            "decorative",
        )
        self.assertTrue(decorative_valid, decorative_reason)

    def test_short_malformed_case_ocr_artifact_requests_runtime_fallback(self):
        group = _scored_group("iiON")

        self.assertIn("short_malformed_case_ocr_artifact", group.quality_reasons)
        self.assertTrue(group_needs_selective_fallback(group))

    def test_spelling_repair_requires_engine_agreement_at_runtime(self):
        assessment = assess_ocr_repair(
            "PLNKS",
            "PLANKS",
            "dictionary_edit_distance_repair",
            confidence=0.94,
            source_engine="rapidocr",
        )
        self.assertFalse(assessment["accepted"])
        self.assertEqual(
            assessment["rejection_reason"],
            "dictionary_change_requires_engine_agreement",
        )

    def test_strict_visual_guard_rejects_old_broad_mask_behavior(self):
        original = np.full((120, 120, 3), 255, dtype=np.uint8)
        damaged = original.copy()
        damaged[20:100, 55:65] = 0
        allowed = np.zeros((120, 120), dtype=np.uint8)
        allowed[45:75, 45:75] = 255
        restored, summary = _enforce_visual_bounds(
            original,
            damaged,
            allowed,
            mask_metrics={"mask_to_text_area_ratio": 5.0},
        )
        self.assertFalse(summary["visual_validation_passed"])
        self.assertIn("mask_area_exceeds_text_area_limit", summary["reason"])
        self.assertIn("outside_component_too_large", summary["reason"])
        self.assertTrue(np.array_equal(restored, original))

    def test_visual_guard_ignores_cleaned_text_edges_near_safe_boundary(self):
        original = np.full((120, 260, 3), 255, dtype=np.uint8)
        cv2.putText(
            original,
            "STORY",
            (70, 42),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        line = OCRLine(
            text="STORY",
            confidence=0.95,
            polygon=np.array([[65, 16], [190, 16], [190, 48], [65, 48]]),
            box=(65, 16, 125, 32),
            raw_text="STORY",
            engine="rapidocr",
        )
        group = TextGroup(group_id="T", lines=[line], text="STORY")
        group.safe_area = (55, 8, 150, 50)
        final = original.copy()
        final[12:52, 60:195] = 255
        allowed = np.zeros(original.shape[:2], dtype=np.uint8)
        allowed[8:58, 55:205] = 255
        _, summary = _enforce_visual_bounds(
            original,
            final,
            allowed,
            group=group,
            mask_metrics={"mask_to_text_area_ratio": 1.5},
        )
        self.assertTrue(summary["visual_validation_passed"], summary["reason"])

    def test_visual_guard_still_rejects_real_structural_border_damage(self):
        original = np.full((120, 260, 3), 255, dtype=np.uint8)
        cv2.line(original, (40, 20), (220, 20), (0, 0, 0), 3)
        line = OCRLine(
            text="STORY",
            confidence=0.95,
            polygon=np.array([[90, 55], [170, 55], [170, 82], [90, 82]]),
            box=(90, 55, 80, 27),
            raw_text="STORY",
            engine="rapidocr",
        )
        group = TextGroup(group_id="T", lines=[line], text="STORY")
        group.safe_area = (40, 20, 180, 80)
        final = original.copy()
        final[18:24, 40:221] = 255
        allowed = np.zeros(original.shape[:2], dtype=np.uint8)
        allowed[18:24, 40:221] = 255
        restored, summary = _enforce_visual_bounds(
            original,
            final,
            allowed,
            group=group,
            mask_metrics={"mask_to_text_area_ratio": 1.0},
        )
        self.assertFalse(summary["visual_validation_passed"])
        self.assertIn("possible_balloon_border_damage", summary["reason"])
        self.assertTrue(np.array_equal(restored, original))

    def test_uniform_dark_narration_allows_bounded_rectangular_cleanup(self):
        original = np.zeros((120, 240, 3), dtype=np.uint8)
        rendered = original.copy()
        rendered[35:85, 45:195] = 35
        allowed = np.zeros((120, 240), dtype=np.uint8)
        allowed[30:90, 40:200] = 255
        _, summary = _enforce_visual_bounds(
            original,
            rendered,
            allowed,
            mask_metrics={
                "mask_to_text_area_ratio": 1.5,
                "broad_rectangular_mask": True,
                "background_type": "dark_balloon",
            },
        )
        self.assertTrue(summary["visual_validation_passed"], summary["reason"])

    def test_large_graphic_line_does_not_merge_into_speech_group(self):
        lines = [
            OCRLine(
                text="SMALL",
                confidence=0.95,
                polygon=np.array([[100, 100], [220, 100], [220, 126], [100, 126]]),
                box=(100, 100, 120, 26),
                raw_text="SMALL",
                engine="rapidocr",
            ),
            OCRLine(
                text="TEXT",
                confidence=0.95,
                polygon=np.array([[110, 128], [210, 128], [210, 154], [110, 154]]),
                box=(110, 128, 100, 26),
                raw_text="TEXT",
                engine="rapidocr",
            ),
        ]
        group = TextGroup(group_id="T", lines=lines, text="SMALL TEXT")
        graphic = OCRLine(
            text="SFX",
            confidence=0.9,
            polygon=np.array([[0, 170], [320, 170], [320, 370], [0, 370]]),
            box=(0, 170, 320, 200),
            raw_text="SFX",
            engine="rapidocr",
        )
        self.assertFalse(_line_belongs_to_group(graphic, group))

    def test_lines_in_separate_white_regions_do_not_merge(self):
        image = np.zeros((220, 400, 3), dtype=np.uint8)
        cv2.ellipse(image, (200, 84), (105, 20), 0, 0, 360, (255, 255, 255), -1)
        cv2.ellipse(image, (200, 140), (105, 20), 0, 0, 360, (255, 255, 255), -1)
        first = OCRLine(
            text="FIRST THOUGHT",
            confidence=0.95,
            polygon=np.array([[130, 72], [270, 72], [270, 97], [130, 97]]),
            box=(130, 72, 140, 25),
            raw_text="FIRST THOUGHT",
            engine="rapidocr",
        )
        second = OCRLine(
            text="SECOND THOUGHT",
            confidence=0.95,
            polygon=np.array([[125, 128], [275, 128], [275, 153], [125, 153]]),
            box=(125, 128, 150, 25),
            raw_text="SECOND THOUGHT",
            engine="rapidocr",
        )
        _assign_visual_white_regions(image, [first, second])
        self.assertNotEqual(
            first.metadata.get("visual_white_region_id"),
            second.metadata.get("visual_white_region_id"),
        )
        self.assertEqual(len(_group_lines([first, second])), 2)

    def test_multisentence_group_splits_at_punctuated_visual_gaps(self):
        texts = ["FIRST", "THOUGHT.", "SECOND", "THOUGHT.", "THIRD", "THOUGHT."]
        y_positions = [10, 36, 78, 104, 146, 172]
        lines = []
        for text, y in zip(texts, y_positions):
            lines.append(
                OCRLine(
                    text=text,
                    confidence=0.95,
                    polygon=np.array([[50, y], [250, y], [250, y + 20], [50, y + 20]]),
                    box=(50, y, 200, 20),
                    raw_text=text,
                    engine="rapidocr",
                )
            )
        group = TextGroup(
            group_id="T",
            lines=lines,
            text=" ".join(texts),
        )
        split = _split_groups_at_sentence_boundaries([group])
        self.assertEqual(len(split), 3)
        self.assertEqual([item.text for item in split], [
            "FIRST THOUGHT.",
            "SECOND THOUGHT.",
            "THIRD THOUGHT.",
        ])

    def test_speed_lines_are_not_classified_as_white_balloon(self):
        image = np.full((240, 320, 3), 218, dtype=np.uint8)
        for offset in range(-120, 320, 18):
            cv2.line(image, (offset, 0), (offset + 180, 239), (90, 90, 90), 2)
        line = OCRLine(
            text="TEXT",
            confidence=0.95,
            polygon=np.array([[60, 80], [250, 80], [250, 140], [60, 140]]),
            box=(60, 80, 190, 60),
            raw_text="TEXT",
            engine="rapidocr",
        )
        group = TextGroup(
            group_id="T",
            lines=[line],
            text="TEXT",
            classification="speech",
            inside_balloon_like_region=True,
        )
        background_type, _ = _classify_background_region(image, group)
        self.assertIn(background_type, {"speed_lines", "textured_art"})

    def test_white_balloon_with_dark_styled_border_remains_white(self):
        image = np.full((240, 320, 3), 255, dtype=np.uint8)
        cv2.ellipse(image, (160, 120), (112, 72), 0, 0, 360, (0, 0, 0), 10)
        cv2.putText(
            image,
            "STORY TEXT",
            (82, 128),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.72,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        line = OCRLine(
            text="STORY TEXT",
            confidence=0.95,
            polygon=np.array([[78, 92], [242, 92], [242, 142], [78, 142]]),
            box=(78, 92, 164, 50),
            raw_text="STORY TEXT",
            engine="rapidocr",
        )
        group = TextGroup(
            group_id="T",
            lines=[line],
            text="STORY TEXT",
            classification="speech",
            inside_balloon_like_region=True,
        )
        background_type, metrics = _classify_background_region(image, group)
        self.assertEqual(background_type, "white_balloon")
        self.assertTrue(metrics["dominant_white_enclosure"])

    def test_small_stylized_white_balloon_is_not_treated_as_speed_lines(self):
        image = np.zeros((240, 320, 3), dtype=np.uint8)
        cv2.ellipse(image, (160, 120), (86, 68), 0, 0, 360, (255, 255, 255), -1)
        cv2.ellipse(image, (160, 120), (86, 68), 0, 0, 360, (15, 15, 15), 18)
        cv2.putText(
            image,
            "I CAN!",
            (112, 130),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.72,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        line = OCRLine(
            text="I CAN!",
            confidence=0.95,
            polygon=np.array([[100, 80], [220, 80], [220, 155], [100, 155]]),
            box=(100, 80, 120, 75),
            raw_text="I CAN!",
            engine="rapidocr",
        )
        group = TextGroup(
            group_id="T",
            lines=[line],
            text="I CAN!",
            classification="speech",
            inside_balloon_like_region=True,
        )
        background_type, metrics = _classify_background_region(image, group)
        self.assertEqual(background_type, "white_balloon")
        self.assertTrue(metrics["stylized_white_enclosure"])

    def test_low_quality_mixed_group_can_receive_regional_fallback(self):
        group = _scored_group("CAN TOO!")
        group.source_engine = "mixed"
        group.quality_score = 0.16
        group.quality_reasons = ["ignored_line_inside_text_region"]
        self.assertTrue(group_needs_selective_fallback(group))

    def test_incomplete_low_quality_group_requests_page_fallback(self):
        group = _scored_group("CAN TOO!")
        group.quality_score = 0.16
        group.cleanup_lines = [_line("I CAN DEFEAT")]
        with (
            patch.object(config, "OCR_ENGINE", "rapidocr"),
            patch.object(config, "OCR_HYBRID_FALLBACK", True),
            patch.object(config, "OCR_FALLBACK_ENGINE", "paddle"),
        ):
            self.assertEqual(
                _grouping_fallback_reason({"raw_lines": group.lines}, [group]),
                "incomplete_group_after_selective_fallback",
            )

    def test_open_white_narration_is_detected_without_balloon_flag(self):
        image = np.full((180, 640, 3), 255, dtype=np.uint8)
        cv2.putText(
            image,
            "I DID NOT CHANGE MYSELF",
            (90, 100),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        line = OCRLine(
            text="I DID NOT CHANGE MYSELF",
            confidence=0.95,
            polygon=np.array([[80, 70], [560, 70], [560, 110], [80, 110]]),
            box=(80, 70, 480, 40),
            raw_text="I DID NOT CHANGE MYSELF",
            engine="rapidocr",
        )
        group = TextGroup(
            group_id="T",
            lines=[line],
            text="I DID NOT CHANGE MYSELF",
            classification="unknown",
        )
        background_type, metrics = _classify_background_region(image, group)
        self.assertEqual(background_type, "narration_box")
        self.assertTrue(metrics["open_white_narration"])

    def test_short_text_embedded_in_saturated_art_is_preserved(self):
        group = _scored_group("5DOLLARS")
        group.classification = "speech"
        group.background_type = "textured_art"
        group.background_metrics = {
            "white_pixel_ratio": 0.0,
            "saturation_mean": 77.0,
        }
        _refine_classification_with_background(group)
        self.assertEqual(group.classification, "decorative")

    def test_large_white_patch_on_textured_art_is_rejected(self):
        original = np.full((180, 240, 3), 135, dtype=np.uint8)
        cleaned = original.copy()
        cleaned[45:135, 50:190] = 255
        mask = np.zeros((180, 240), dtype=np.uint8)
        mask[45:135, 50:190] = 255
        group = TextGroup(group_id="T", lines=[_line("TEXT")], text="TEXT")
        group.lines[0].box = (50, 45, 140, 90)
        metrics = _white_patch_artifact_metrics(
            original,
            cleaned,
            group,
            mask,
            "textured_art",
        )
        self.assertTrue(metrics["white_patch_rejected"])

    def test_outlined_light_text_mask_stays_tight_on_textured_art(self):
        image = np.full((180, 360, 3), (95, 125, 155), dtype=np.uint8)
        for x in range(0, 360, 18):
            cv2.line(image, (x, 0), (min(359, x + 80), 179), (50, 70, 90), 2)
        cv2.putText(
            image, "SPOKEN TEXT", (40, 100), cv2.FONT_HERSHEY_SIMPLEX,
            1.0, (0, 0, 0), 7, cv2.LINE_AA,
        )
        cv2.putText(
            image, "SPOKEN TEXT", (40, 100), cv2.FONT_HERSHEY_SIMPLEX,
            1.0, (255, 255, 255), 2, cv2.LINE_AA,
        )
        line = OCRLine(
            text="SPOKEN TEXT",
            confidence=0.95,
            polygon=np.array([[35, 65], [330, 65], [330, 112], [35, 112]]),
            box=(35, 65, 295, 47),
            raw_text="SPOKEN TEXT",
            engine="rapidocr",
        )
        group = TextGroup(group_id="T", lines=[line], text="SPOKEN TEXT")
        maximum = np.zeros(image.shape[:2], dtype=np.uint8)
        cv2.fillPoly(maximum, [line.polygon.astype(np.int32)], 255)

        mask, metrics = _outlined_light_text_mask(image, group, maximum)
        shape = _mask_shape_metrics(mask, group.box)

        self.assertGreater(metrics["accepted_text_components"], 3)
        self.assertGreater(np.count_nonzero(mask), 0)
        self.assertFalse(shape["broad_rectangular_mask"])

    def test_dark_blotch_from_cleanup_is_rejected_on_textured_art(self):
        original = np.full((180, 360, 3), (125, 155, 185), dtype=np.uint8)
        cleaned = original.copy()
        mask = np.zeros(original.shape[:2], dtype=np.uint8)
        cv2.rectangle(mask, (45, 65), (315, 118), 255, -1)
        for x in range(65, 305, 38):
            cv2.circle(cleaned, (x, 90), 11, (0, 0, 0), -1)
        group = TextGroup(group_id="T", lines=[_line("TEXT OVER ART")], text="TEXT OVER ART")
        group.lines[0].box = (45, 65, 270, 53)
        metrics = _dark_blotch_artifact_metrics(
            original,
            cleaned,
            group,
            mask,
            "textured_art",
            "glyph_overlay",
        )
        self.assertTrue(metrics["dark_blotch_rejected"])
        self.assertGreater(metrics["largest_new_dark_component_area"], 120)

    def test_caption_overlay_is_limited_to_separate_line_polygons(self):
        image = np.full((180, 360, 3), (110, 145, 175), dtype=np.uint8)
        cv2.line(image, (0, 160), (350, 10), (40, 70, 90), 3)
        cv2.putText(
            image, "TEXT OVER", (48, 72), cv2.FONT_HERSHEY_SIMPLEX,
            0.75, (0, 0, 0), 6, cv2.LINE_AA,
        )
        cv2.putText(
            image, "TEXT OVER", (48, 72), cv2.FONT_HERSHEY_SIMPLEX,
            0.75, (255, 255, 255), 2, cv2.LINE_AA,
        )
        cv2.putText(
            image, "TEXTURE", (75, 126), cv2.FONT_HERSHEY_SIMPLEX,
            0.75, (0, 0, 0), 6, cv2.LINE_AA,
        )
        cv2.putText(
            image, "TEXTURE", (75, 126), cv2.FONT_HERSHEY_SIMPLEX,
            0.75, (255, 255, 255), 2, cv2.LINE_AA,
        )
        first = OCRLine(
            text="TEXT OVER",
            confidence=0.95,
            polygon=np.array([[40, 45], [315, 30], [318, 70], [43, 85]]),
            box=(40, 30, 278, 55),
            raw_text="TEXT OVER",
            engine="rapidocr",
        )
        second = OCRLine(
            text="TEXTURE",
            confidence=0.95,
            polygon=np.array([[65, 100], [290, 88], [293, 128], [68, 140]]),
            box=(65, 88, 228, 52),
            raw_text="TEXTURE",
            engine="rapidocr",
        )
        group = TextGroup(group_id="T", lines=[first, second], text="TEXT OVER TEXTURE")
        maximum = np.zeros(image.shape[:2], dtype=np.uint8)
        for line in group.lines:
            cv2.fillPoly(maximum, [line.polygon.astype(np.int32)], 255)

        mask, metrics = _caption_overlay_mask(image, group, maximum)
        overlaid = _apply_textured_caption_overlay(image, mask)
        changed = np.any(overlaid != image, axis=2)

        self.assertEqual(metrics["caption_overlay_line_count"], 2)
        self.assertGreater(np.count_nonzero(mask), 0)
        self.assertEqual(np.count_nonzero(changed & (mask == 0)), 0)
        self.assertGreater(float(np.std(overlaid[mask > 0])), 0.0)

    def test_generic_production_credit_is_preserved_as_decorative(self):
        image = np.full((300, 400, 3), 255, dtype=np.uint8)
        group = _scored_group("ARTBY CREATORNAME")
        _classify_groups([group], image)
        self.assertEqual(group.classification, "decorative")

    def test_short_unknown_text_with_white_context_uses_light_region_cleanup(self):
        image = np.full((180, 360, 3), 255, dtype=np.uint8)
        cv2.putText(
            image,
            "WAIT",
            (138, 99),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        line = OCRLine(
            text="WAIT",
            confidence=0.95,
            polygon=np.array([[132, 70], [228, 70], [228, 108], [132, 108]]),
            box=(132, 70, 96, 38),
            raw_text="WAIT",
            engine="rapidocr",
        )
        group = TextGroup(
            group_id="T",
            lines=[line],
            text="WAIT",
            classification="unknown",
        )
        background_type, metrics = _classify_background_region(image, group)
        self.assertEqual(background_type, "narration_box")
        self.assertTrue(metrics["open_white_narration"])
        self.assertTrue(metrics["short_text_white_context"])

    def test_short_embedded_label_on_art_is_decorative(self):
        group = _scored_group("LOCAL SHOP OPEN TODAY")
        group.classification = "speech"
        group.background_type = "speed_lines"
        group.background_metrics = {
            "image_width": 800,
            "image_height": 1800,
            "white_pixel_ratio": 0.48,
            "saturation_mean": 24.0,
            "edge_density": 0.06,
            "local_texture_mean": 9.0,
            "open_white_narration": False,
            "open_dark_narration": False,
        }
        group.angle_degrees = 6.0

        _refine_classification_with_background(group)

        self.assertEqual(group.classification, "decorative")
        self.assertTrue(group.ignored)
        self.assertEqual(group.ignore_reason, "decorative_text")

    def test_recurring_compact_name_repairs_single_spaced_variant(self):
        compact_a = _scored_group("Suchan, are you coming?")
        compact_a.group_id = "A"
        compact_b = _scored_group("Hey, Suchan!")
        compact_b.group_id = "B"
        spaced = _scored_group("SUCH AN is coming.")
        spaced.group_id = "C"

        repairs = normalize_recurring_compact_names([compact_a, compact_b, spaced])

        self.assertEqual(spaced.text, "SUCHAN is coming.")
        self.assertTrue(
            any(
                item["group_id"] == "C"
                and item["repair_reason"] == "chapter_consensus_compact_name"
                for item in repairs
            )
        )

    def test_generic_produced_by_credit_is_preserved_as_decorative(self):
        image = np.full((300, 400, 3), 255, dtype=np.uint8)
        group = _scored_group("PRODUCED BY CREATORNAME")
        _classify_groups([group], image)
        self.assertEqual(group.classification, "decorative")
        self.assertTrue(group.ignored)

    def test_short_speech_uses_enclosed_visual_region_when_contour_is_weak(self):
        cases = (
            "HI...",
            "BLOOD...!",
            "RUN, RUN!!",
            "HELP!",
            "NO!",
            "WAIT!",
            "MOM...",
            "WHY?",
        )
        for text in cases:
            with self.subTest(text=text):
                image, _, group = _white_balloon_fixture(text)

                with patch(
                    "ocr_balloon._enclosure_evidence",
                    return_value=(False, False),
                ):
                    _classify_groups([group], image)

                self.assertEqual(group.classification, "speech")
                self.assertFalse(group.ignored)
                self.assertTrue(_should_translate_group(group))
                self.assertTrue(
                    group.classification_reason.startswith(
                        "enclosed_visual_region_over_"
                    )
                )
                self.assertGreaterEqual(group.classification_confidence, 0.5)

    def test_visual_white_region_records_closed_container_geometry(self):
        _, line, _ = _white_balloon_fixture("WAIT!")

        self.assertTrue(line.metadata["visual_white_region_enclosed"])
        self.assertEqual(line.metadata["visual_white_region_touches_edge"], 0)
        self.assertGreater(line.metadata["visual_white_region_coverage"], 0.5)
        self.assertGreater(line.metadata["visual_white_region_height_ratio"], 1.5)

    def test_white_region_touching_page_edge_is_not_strong_container_evidence(self):
        image = np.zeros((320, 420, 3), dtype=np.uint8)
        cv2.rectangle(image, (0, 70), (260, 235), (255, 255, 255), -1)
        line = _boxed_line("WAIT!", (85, 132, 90, 36), confidence=0.95)

        _assign_visual_white_regions(image, [line])

        self.assertGreater(line.metadata["visual_white_region_touches_edge"], 0)
        self.assertFalse(line.metadata["visual_white_region_enclosed"])

    def test_exterior_dark_field_spanning_roi_is_not_container_evidence(self):
        image = np.zeros((360, 500, 3), dtype=np.uint8)

        balloon_like, narration_like = _enclosure_evidence(
            image,
            (230, 150, 120, 50),
        )

        self.assertFalse(balloon_like)
        self.assertFalse(narration_like)

    def test_dense_component_touching_roi_edge_remains_container_evidence(self):
        image = np.full((300, 360, 3), 255, dtype=np.uint8)
        cv2.rectangle(image, (0, 80), (250, 220), (0, 0, 0), -1)

        evidence = _uniform_container_evidence(image, (80, 120, 100, 40))

        self.assertTrue(evidence["enclosed"])

    def test_internal_dark_container_remains_enclosure_evidence(self):
        image = np.full((360, 500, 3), 255, dtype=np.uint8)
        cv2.ellipse(image, (250, 180), (150, 85), 0, 0, 360, (0, 0, 0), -1)

        balloon_like, narration_like = _enclosure_evidence(
            image,
            (210, 155, 100, 45),
        )

        self.assertTrue(balloon_like)
        self.assertTrue(narration_like)

    def test_stylized_short_text_outside_balloon_remains_sfx(self):
        cases = (
            "CRUNCH",
            "WHAM",
            "WHOOSH",
            "STEP",
            "CREAK",
            "SCREECH",
            "SHWOOMP",
            "SPLAT",
            "JOLT",
            "SHUDDER",
            "SQUEAK",
            "GRIND",
            "THWACK",
            "SLAM",
            "DASH",
        )
        for text in cases:
            with self.subTest(text=text):
                image = np.full((320, 420, 3), 96, dtype=np.uint8)
                line = _boxed_line(text, (2, 120, 155, 50), confidence=0.95, slant=12)
                group = TextGroup(group_id="T", lines=[line], text=text)

                with patch(
                    "ocr_balloon._enclosure_evidence",
                    return_value=(False, False),
                ):
                    _classify_groups([group], image)

                self.assertEqual(group.classification, "sfx")
                self.assertTrue(group.ignored)
                self.assertEqual(group.ignore_reason, "sfx_translation_disabled")

    def test_nonlexical_vocalization_inside_white_region_remains_sfx(self):
        for text in ("AAAAAH!!", "SKR!!"):
            with self.subTest(text=text):
                image, _, group = _white_balloon_fixture(text)

                with patch(
                    "ocr_balloon._enclosure_evidence",
                    return_value=(False, False),
                ):
                    _classify_groups([group], image)

                self.assertEqual(group.classification, "sfx")
                self.assertTrue(group.ignored)
                self.assertEqual(group.ignore_reason, "sfx_translation_disabled")

    def test_same_token_changes_classification_with_container_geometry(self):
        balloon_image, _, balloon_group = _white_balloon_fixture("RUN!!")

        effect_image = np.full((320, 420, 3), 96, dtype=np.uint8)
        effect_line = _boxed_line(
            "RUN!!",
            (2, 120, 155, 50),
            confidence=0.95,
            slant=12,
        )
        effect_group = TextGroup(group_id="EFFECT", lines=[effect_line], text="RUN!!")

        with patch("ocr_balloon._enclosure_evidence", return_value=(False, False)):
            _classify_groups([balloon_group], balloon_image)
            _classify_groups([effect_group], effect_image)

        self.assertEqual(balloon_group.classification, "speech")
        self.assertEqual(effect_group.classification, "sfx")

    def test_repeated_onomatopoeia_in_false_enclosure_stays_sfx(self):
        # A sound effect lettered on bright art (e.g. footsteps "STEP STEP") can
        # trip a false narration/white-region enclosure. A caption/balloon whose
        # entire content is one short token repeated is never real prose, so it
        # must stay a sound effect: not translated, not redrawn over the art.
        image = np.full((400, 420, 3), 210, dtype=np.uint8)
        lines = [
            _boxed_line("STEP", (120, 120, 150, 60), confidence=0.90),
            _boxed_line("Step", (200, 240, 150, 60), confidence=0.75),
        ]
        group = TextGroup(group_id="SFX", lines=lines, text="STEP Step")

        with patch("ocr_balloon._enclosure_evidence", return_value=(False, True)), \
                patch("ocr_balloon._visual_white_container_evidence", return_value={}):
            _classify_groups([group], image)

        self.assertEqual(group.classification, "sfx")
        self.assertTrue(group.ignored)
        self.assertEqual(group.ignore_reason, "sfx_translation_disabled")

    def test_repeated_word_inside_real_balloon_stays_speech(self):
        # Guard: a genuine repeated exclamation inside a real balloon contour is
        # speech, not a sound effect.
        image, _, group = _white_balloon_fixture("RUN RUN")

        with patch("ocr_balloon._enclosure_evidence", return_value=(True, False)):
            _classify_groups([group], image)

        self.assertEqual(group.classification, "speech")

    def test_deliberate_censorship_remains_filtered_before_translation(self):
        line = _boxed_line("S***!", (135, 126, 150, 48), confidence=0.95)
        self.assertEqual(
            _line_ignore_reason(line, (320, 420, 3)),
            "too_few_useful_chars",
        )

    def test_censored_word_beside_real_words_survives_noise_filter(self):
        # A masked expletive next to real words is translatable speech; the
        # censorship marks must not push the line into the noise filter and
        # block the translation entirely.
        line = _boxed_line("DAMN H***!", (120, 120, 200, 48), confidence=0.95)
        self.assertEqual(_line_ignore_reason(line, (320, 420, 3)), "")
        self.assertFalse(_looks_like_noise("DAMN H***!"))

    def test_symbol_soup_without_words_is_still_noise(self):
        self.assertTrue(_looks_like_noise("@#%^~<>|"))

    def test_saturated_editorial_graphic_is_decorative_despite_false_enclosure(self):
        fixtures = (
            "PLATFORM ZERO",
            "SERIES TITLE",
            "EPISODE 1",
            "CHAPTER TITLE",
        )
        for text in fixtures:
            with self.subTest(text=text):
                image = _editorial_art_fixture()
                line = _boxed_line(text, (150, 330, 300, 46), confidence=0.95)
                group = TextGroup(group_id="T", lines=[line], text=text)

                with patch(
                    "ocr_balloon._enclosure_evidence",
                    return_value=(True, False),
                ):
                    _classify_groups([group], image)

                self.assertEqual(group.classification, "decorative")
                self.assertTrue(group.ignored)
                self.assertEqual(group.ignore_reason, "decorative_text")
                self.assertEqual(
                    group.classification_reason,
                    "editorial_graphic_layout",
                )
                self.assertGreater(group.classification_confidence, 0.5)

    def test_split_editorial_graphic_cluster_is_preserved_as_decorative(self):
        image = _editorial_art_fixture()
        first_line = _boxed_line("SERIES", (95, 330, 245, 46), confidence=0.95)
        second_line = _boxed_line("TITLE", (360, 332, 145, 44), confidence=0.95)
        groups = [
            TextGroup(group_id="LEFT", lines=[first_line], text="SERIES"),
            TextGroup(group_id="RIGHT", lines=[second_line], text="TITLE"),
        ]

        with patch(
            "ocr_balloon._enclosure_evidence",
            side_effect=((True, False), (False, False)),
        ):
            _classify_groups(groups, image)

        self.assertEqual(
            [group.classification for group in groups],
            ["decorative", "decorative"],
        )
        self.assertTrue(all(group.ignored for group in groups))
        self.assertTrue(
            all(group.ignore_reason == "decorative_text" for group in groups)
        )
        self.assertTrue(
            all(
                group.classification_reason == "editorial_graphic_layout"
                for group in groups
            )
        )

    def test_midpage_relevant_signage_is_not_preserved_as_editorial_branding(self):
        image = _editorial_art_fixture()
        line = _boxed_line("DANGER KEEP OUT", (150, 175, 300, 46), confidence=0.95)
        group = TextGroup(group_id="T", lines=[line], text=line.text)

        with patch("ocr_balloon._enclosure_evidence", return_value=(True, False)):
            _classify_groups([group], image)

        self.assertIn(group.classification, {"speech", "narration"})
        self.assertFalse(group.ignored)
        self.assertTrue(_should_translate_group(group))

    def test_short_text_without_container_or_styling_remains_weak_unknown(self):
        image = np.full((320, 420, 3), 150, dtype=np.uint8)
        line = _boxed_line("HELP!", (165, 132, 90, 36), confidence=0.95)
        group = TextGroup(group_id="T", lines=[line], text=line.text)

        with patch("ocr_balloon._enclosure_evidence", return_value=(False, False)):
            _classify_groups([group], image)

        self.assertEqual(group.classification, "unknown")
        self.assertTrue(group.ignored)
        self.assertEqual(group.ignore_reason, "weak_unknown_text")

    def test_rectangular_narration_remains_translatable(self):
        image = np.zeros((400, 600, 3), dtype=np.uint8)
        cv2.rectangle(image, (90, 80), (510, 250), (255, 255, 255), -1)
        lines = [
            _boxed_line("THE NIGHT WAS", (170, 115, 260, 35), confidence=0.95),
            _boxed_line("VERY QUIET.", (185, 165, 230, 35), confidence=0.95),
        ]
        _assign_visual_white_regions(image, lines)
        group = TextGroup(
            group_id="T",
            lines=lines,
            text="THE NIGHT WAS VERY QUIET.",
        )

        with patch("ocr_balloon._enclosure_evidence", return_value=(True, True)):
            _classify_groups([group], image)

        self.assertEqual(group.classification, "narration")
        self.assertFalse(group.ignored)
        self.assertTrue(_should_translate_group(group))

    def test_contextual_classification_evidence_is_persisted_in_debug_payload(self):
        image, line, group = _white_balloon_fixture("WAIT!")
        with patch("ocr_balloon._enclosure_evidence", return_value=(False, False)):
            _classify_groups([group], image)

        item = _debug_payload("fixture.png", [line], [], [group])["items"][0]

        self.assertEqual(item["classification"], "speech")
        self.assertEqual(
            item["classification_reason"],
            "enclosed_visual_region_over_weak_text",
        )
        self.assertGreaterEqual(item["classification_confidence"], 0.5)
        self.assertEqual(
            item["classification_evidence"]["conflict_resolved"],
            "unknown_weak",
        )

    def test_elongated_vocalization_with_punctuation_is_sfx(self):
        image = np.full((300, 400, 3), 180, dtype=np.uint8)
        group = _scored_group("AAHHHK!!!")
        _classify_groups([group], image)
        self.assertEqual(group.classification, "sfx")
        self.assertTrue(group.ignored)

    def test_short_consonant_vocalization_is_sfx(self):
        image = np.full((300, 400, 3), 255, dtype=np.uint8)
        group = _scored_group("GGV")
        _classify_groups([group], image)
        self.assertEqual(group.classification, "sfx")
        self.assertTrue(group.ignored)

    def test_layout_overflow_retry_accepts_only_shorter_valid_translation(self):
        group = _scored_group("BOOK AN APPOINTMENT AT THE HAIR SHOP")
        group.sent_to_translation = True
        group.translation = "MARQUE UM HORARIO NO SALAO DE CABELO"
        group.text_overflow_ratio = 1.0

        class ShortTranslator:
            def translate_strict(self, *args, **kwargs):
                return "AGENDE NO SALAO"

        records = []
        count = _retry_layout_overflow_translations(
            [group],
            ShortTranslator(),
            records,
            force=True,
            attempt=1,
        )
        self.assertEqual(count, 1)
        self.assertEqual(group.translation, "AGENDE NO SALAO")
        self.assertEqual(records[0]["retry_type"], "layout_overflow")

    def test_invalid_translation_is_not_sent_to_renderer(self):
        group = _scored_group("I WILL RETURN")
        group.sent_to_translation = True
        group.translation = "EU WILL RETURN"
        group.translation_valid = False
        self.assertFalse(_should_translate_group(group))

    def test_failed_translation_retry_preserves_original_region_for_review(self):
        group = _scored_group("I WILL RETURN")
        group.sent_to_translation = True
        group.translation = "EU WILL RETURN"

        class InvalidTranslator:
            def translate_strict(self, *args, **kwargs):
                return "EU WILL RETURN"

        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            records = validate_and_retry_translations([group], InvalidTranslator())
        # The strict retry runs, then the isolated attempt (no proper name is
        # known, so a full translation is demanded). Both fail here, and the
        # region is still preserved for review rather than rendered.
        self.assertEqual(len(records), 2)
        self.assertTrue(records[-1]["isolated"])
        self.assertFalse(any(record["valid"] for record in records))
        self.assertFalse(group.translation_valid)
        self.assertTrue(group.manual_review_required)
        self.assertEqual(group.translation, group.text)

    def test_post_render_ocr_rejects_residual_source_english(self):
        group = _scored_group("WHAT IS THIS")
        group.safe_area = (0, 0, 220, 80)
        residual_line = _line("WHAT IS THIS")
        image = np.full((100, 240, 3), 255, dtype=np.uint8)
        with patch(
            "ocr_balloon.OCREngine._detect_with_rapidocr",
            return_value=[residual_line],
        ):
            result = _post_render_source_text_check(image, group, page_index=7)
        self.assertFalse(result["passed"])
        self.assertIn("WHAT", result["residual_source_tokens"])

    def test_post_render_ocr_allows_token_intentionally_kept_in_translation(self):
        group = _scored_group("DRINK ON STARGRAM")
        group.translation = "BEBIDA NO STARGRAM"
        group.safe_area = (0, 0, 220, 80)
        image = np.full((100, 240, 3), 255, dtype=np.uint8)
        with patch(
            "ocr_balloon.OCREngine._detect_with_rapidocr",
            return_value=[_line("BEBIDA NO STARGRAM")],
        ):
            result = _post_render_source_text_check(image, group, page_index=7)
        self.assertTrue(result["passed"])
        self.assertNotIn("STARGRAM", result.get("residual_source_tokens", []))

    def test_translation_fragment_duplication_is_rejected_for_retry(self):
        valid, reason = validate_translation_text(
            "A COLLEGE IN THE PROVINCE",
            "UMA FACULDADE NA PROVDE NA PROVINCIA",
            "narration",
        )
        self.assertFalse(valid)
        self.assertIn("repeated_translation_fragment", reason)

    def test_nvidia_retries_invalid_json_without_retranslating_other_batches(self):
        translator = TranslatorNvidiaBatch(api_key="test-key", enable_cache=False)
        responses = ["not-json", '{"BALAO_1": "Volte agora"}']
        with patch.object(translator, "_request_with_retry", side_effect=responses):
            parsed = translator._request_json_with_retry(
                [{"role": "user", "content": "translate"}],
                ["BALAO_1"],
            )
        self.assertEqual(parsed["BALAO_1"], "Volte agora")
        self.assertEqual(translator.stats["invalid_json_retries"], 1)
        self.assertEqual(translator.stats["invalid_json_failures"], 0)

    def test_sentence_boundary_split_keeps_single_styled_line_separate(self):
        lines = []
        for index, text in enumerate(("THAT'S TWO", "NIGHTS IN", "A ROW!", "GODDAMN")):
            line = _line(text)
            line.box = (420, 120 + index * 42 + (34 if index == 3 else 0), 220, 32)
            lines.append(line)
        group = TextGroup(
            group_id="T",
            lines=lines,
            text=" ".join(line.text for line in lines),
        )
        split = _split_groups_at_sentence_boundaries([group])
        self.assertEqual(len(split), 2)
        self.assertEqual(split[0].text, "THAT'S TWO NIGHTS IN A ROW!")
        self.assertEqual(split[1].text, "GODDAMN")

    def test_open_dark_semantic_text_is_a_safe_narration_region(self):
        image = np.zeros((220, 700, 3), dtype=np.uint8)
        cv2.putText(
            image,
            "CONTENT WARNING FOR READERS",
            (80, 120),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        group = TextGroup(
            group_id="T",
            lines=[_line("CONTENT WARNING FOR READERS")],
            text="CONTENT WARNING FOR READERS",
            classification="unknown",
        )
        group.lines[0].box = (70, 80, 500, 50)
        background_type, metrics = _classify_background_region(image, group)
        self.assertEqual(background_type, "narration_box")
        self.assertTrue(metrics["open_dark_narration"])

    def test_short_phrase_on_clean_white_context_is_not_treated_as_art(self):
        image = np.full((220, 500, 3), 255, dtype=np.uint8)
        cv2.putText(
            image,
            "GO NOW",
            (160, 120),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        group = TextGroup(
            group_id="T",
            lines=[_line("GO NOW")],
            text="GO NOW",
            classification="unknown",
        )
        group.lines[0].box = (150, 80, 180, 50)
        background_type, metrics = _classify_background_region(image, group)
        self.assertEqual(background_type, "narration_box")
        self.assertTrue(metrics["open_white_narration"])

    def test_uniform_white_short_region_without_enclosure_is_narration_box(self):
        image = np.full((180, 360, 3), 255, dtype=np.uint8)
        line = OCRLine(
            text="WAIT",
            confidence=0.95,
            polygon=np.array([[130, 72], [230, 72], [230, 108], [130, 108]]),
            box=(130, 72, 100, 36),
            raw_text="WAIT",
            engine="rapidocr",
        )
        group = TextGroup(
            group_id="T",
            lines=[line],
            text="WAIT",
            classification="unknown",
        )
        background_type, metrics = _classify_background_region(image, group)
        self.assertEqual(background_type, "narration_box")
        self.assertTrue(metrics["strongly_uniform_white"])

    def test_detached_light_glyph_is_recovered_on_uniform_dark_region(self):
        image = np.zeros((120, 260, 3), dtype=np.uint8)
        source_mask = np.zeros((120, 260), dtype=np.uint8)
        source_mask[42:76, 82:220] = 255
        cv2.rectangle(image, (66, 46), (70, 72), (255, 255, 255), -1)
        cv2.rectangle(image, (84, 46), (216, 72), (255, 255, 255), -1)
        line = OCRLine(
            text="SMELL PREY",
            confidence=0.94,
            polygon=np.array([[82, 42], [220, 42], [220, 76], [82, 76]]),
            box=(82, 42, 138, 34),
            raw_text="SMELL PREY",
            engine="rapidocr",
        )
        group = TextGroup(
            group_id="T",
            lines=[line],
            text="SMELL PREY",
            classification="speech",
            inside_balloon_like_region=True,
        )
        group.background_metrics = {
            "dark_context": True,
            "context_dark_pixel_ratio": 0.99,
            "context_saturation_mean": 0.0,
        }
        detached, metrics = _detached_light_text_components_mask(
            image,
            group,
            source_mask,
        )
        self.assertGreater(metrics["detached_text_components"], 0)
        self.assertGreater(np.count_nonzero(detached[44:75, 63:74]), 0)

    def test_uniform_dark_line_mask_covers_light_antialias_pixels(self):
        image = np.zeros((120, 260, 3), dtype=np.uint8)
        cv2.putText(
            image,
            "STORY",
            (72, 72),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (220, 220, 220),
            2,
            cv2.LINE_AA,
        )
        line = OCRLine(
            text="STORY",
            confidence=0.94,
            polygon=np.array([[66, 38], [190, 38], [190, 82], [66, 82]]),
            box=(66, 38, 124, 44),
            raw_text="STORY",
            engine="rapidocr",
        )
        group = TextGroup(group_id="T", lines=[line], text="STORY")
        group.background_metrics = {"dark_context": True}
        mask, metrics = _uniform_dark_line_text_mask(image, group)
        text_pixels = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) >= 115
        self.assertGreater(metrics["uniform_dark_line_pixels"], 0)
        self.assertEqual(np.count_nonzero(text_pixels & (mask == 0)), 0)

    def test_uniform_light_line_mask_covers_entire_ocr_polygon(self):
        image = np.full((120, 260, 3), 255, dtype=np.uint8)
        line = OCRLine(
            text="STORY",
            confidence=0.94,
            polygon=np.array([[66, 38], [190, 38], [190, 82], [66, 82]]),
            box=(66, 38, 124, 44),
            raw_text="STORY",
            engine="rapidocr",
        )
        group = TextGroup(group_id="T", lines=[line], text="STORY")
        group.background_type = "narration_box"
        group.background_metrics = {
            "strongly_uniform_white": True,
            "uniform_light": True,
            "white_pixel_ratio": 1.0,
            "saturation_mean": 0.0,
        }
        mask, metrics = _uniform_light_line_text_mask(image, group)
        self.assertEqual(metrics["uniform_light_line_count"], 1)
        self.assertTrue(np.all(mask[39:82, 67:190] > 0))

    def test_detached_dark_glyph_is_recovered_on_uniform_white_region(self):
        image = np.full((120, 260, 3), 255, dtype=np.uint8)
        source_mask = np.zeros((120, 260), dtype=np.uint8)
        source_mask[42:76, 82:220] = 255
        cv2.rectangle(image, (66, 46), (70, 72), (0, 0, 0), -1)
        line = OCRLine(
            text="STORY",
            confidence=0.94,
            polygon=np.array([[82, 42], [220, 42], [220, 76], [82, 76]]),
            box=(82, 42, 138, 34),
            raw_text="STORY",
            engine="rapidocr",
        )
        group = TextGroup(group_id="T", lines=[line], text="STORY")
        group.background_type = "narration_box"
        group.background_metrics = {
            "strongly_uniform_white": True,
            "uniform_light": True,
            "white_pixel_ratio": 1.0,
            "saturation_mean": 0.0,
        }
        detached, metrics = _detached_dark_text_components_mask(
            image,
            group,
            source_mask,
        )
        self.assertGreater(metrics["detached_dark_text_components"], 0)
        self.assertGreater(np.count_nonzero(detached[44:75, 63:74]), 0)

    def test_small_multiline_screen_text_is_preserved_as_decorative(self):
        group = _scored_group("VIDEO TITLE SHARE FOLLOWERS COMMENTS SAVE")
        group.lines = [_line("VIDEO TITLE"), _line("SHARE FOLLOWERS"), _line("COMMENTS SAVE")]
        for index, line in enumerate(group.lines):
            line.box = (220, 1500 + index * 45, 260, 38)
        group.classification = "narration"
        group.background_type = "speed_lines"
        group.background_metrics = {
            "image_width": 800,
            "image_height": 1800,
            "white_pixel_ratio": 0.08,
            "saturation_mean": 80.0,
            "edge_density": 0.12,
            "local_texture_mean": 10.0,
        }
        _refine_classification_with_background(group)
        self.assertEqual(group.classification, "decorative")
        self.assertTrue(group.ignored)

    def test_unknown_short_graphic_on_high_edge_art_becomes_sfx(self):
        group = _scored_group("THROR")
        group.classification = "speech"
        group.background_type = "textured_art"
        group.background_metrics = {
            "image_width": 800,
            "image_height": 1800,
            "white_pixel_ratio": 0.65,
            "saturation_mean": 50.0,
            "edge_density": 0.07,
            "local_texture_mean": 7.0,
        }
        _refine_classification_with_background(group)
        self.assertEqual(group.classification, "sfx")
        self.assertTrue(group.ignored)

    def test_character_metadata_overlay_on_art_is_preserved_without_inpainting(self):
        group = _scored_group("PERSON NAME (20) COLLEGE STUDENT")
        group.lines = [_line("PERSON NAME (20)"), _line("COLLEGE STUDENT")]
        for index, line in enumerate(group.lines):
            line.box = (150, 1300 + index * 46, 500, 40)
        group.classification = "unknown"
        group.inside_balloon_like_region = False
        group.inside_narration_box_like_region = False
        group.background_type = "textured_art"
        group.background_metrics = {
            "image_width": 800,
            "image_height": 1800,
            "white_pixel_ratio": 0.3,
            "saturation_mean": 40.0,
            "edge_density": 0.02,
            "local_texture_mean": 2.0,
        }
        _refine_classification_with_background(group)
        self.assertEqual(group.classification, "decorative")
        self.assertTrue(group.ignored)


if __name__ == "__main__":
    unittest.main()
