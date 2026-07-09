import unittest
from unittest.mock import patch

import cv2
import numpy as np
import config

from ocr_balloon import (
    TextGroup,
    _assign_visual_white_regions,
    _classify_background_region,
    _classify_groups,
    _caption_overlay_mask,
    _dark_blotch_artifact_metrics,
    _detached_light_text_components_mask,
    _detached_dark_text_components_mask,
    _enforce_visual_bounds,
    _line_belongs_to_group,
    _group_lines,
    _post_render_source_text_check,
    _normalized_ocr_candidate_text,
    _outlined_light_text_mask,
    _uniform_dark_line_text_mask,
    _uniform_light_line_text_mask,
    _apply_textured_caption_overlay,
    _refine_classification_with_background,
    _should_translate_group,
    _split_groups_at_sentence_boundaries,
    _mask_shape_metrics,
    _white_patch_artifact_metrics,
    _should_skip_paddle_full_for_ignored_decorative,
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
    _preserve_selected_regional_ocr,
    _retry_layout_overflow_translations,
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

    def test_short_ocr_typo_is_suspicious_and_better_candidate_wins(self):
        bad = _scored_group("BLT")
        good = _scored_group("BUT")
        self.assertTrue(group_needs_selective_fallback(bad))
        self.assertGreater(good.quality_score, bad.quality_score)
        self.assertEqual(repair_ocr_text("BLT")[0], "BUT")

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
        self.assertEqual(len(records), 1)
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
