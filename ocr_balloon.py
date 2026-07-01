import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import config
from json_utils import dump_json
from ocr_engine import (
    OCREngine,
    OCRLine,
    assess_ocr_repair,
    clean_ocr_text,
    repair_ocr_text,
    segment_compact_english_word,
    suggest_english_word,
)

try:
    from config import FONT_PATH, TEMP_FOLDER, TEMP_OUT
except Exception:
    FONT_PATH = None
    TEMP_FOLDER = "capitulo_temp"
    TEMP_OUT = TEMP_FOLDER + "_out"


SHORT_REAL_TEXTS = {
    "A",
    "NO",
    "NO!",
    "GO",
    "GO!",
    "RUN",
    "RUN!",
    "AH",
    "AH!",
    "HUH",
    "HUH?",
    "HE",
    "HE...",
    "HE..",
    "HIS",
    "HIS?",
    "HA",
    "HA!",
}

SFX_WORDS = {
    "BANG",
    "BOOM",
    "BUMP",
    "CLANG",
    "CRASH",
    "DRIP",
    "GONG",
    "GRR",
    "GULP",
    "HISS",
    "KNOCK",
    "MMEE",
    "MEE",
    "PLUNK",
    "PLOP",
    "SLAM",
    "SNIFF",
    "SNIFFLE",
    "SOB",
    "THUD",
    "UGH",
    "WHEW",
    "WHAM",
    "WHOOSH",
}

COMMON_ENGLISH_WORDS = {
    "ABOUT",
    "AFTER",
    "AGAIN",
    "ALMOST",
    "AND",
    "BECAUSE",
    "BEFORE",
    "BETTER",
    "BUT",
    "COME",
    "COULD",
    "DID",
    "DO",
    "DISAPPEARED",
    "EYES",
    "FACED",
    "FOR",
    "FRIEND",
    "GOING",
    "HAVE",
    "HEAD",
    "HE",
    "HER",
    "HIM",
    "HIS",
    "I",
    "MET",
    "MOM",
    "MY",
    "OR",
    "OF",
    "ON",
    "PLANKS",
    "REALLY",
    "RESONATED",
    "SHE",
    "SO",
    "SOON",
    "THE",
    "THESE",
    "TODAY",
    "UP",
    "VOICE",
    "WAKE",
    "WHAT",
    "WHEN",
    "WHY",
    "WILL",
    "WITH",
    "YEAH",
    "YOU",
}

PORTUGUESE_MARKERS = {
    "A",
    "AS",
    "AO",
    "AOS",
    "COM",
    "CONTRA",
    "CUIDAR",
    "DA",
    "DAS",
    "DE",
    "DERROTAR",
    "DO",
    "DOS",
    "E",
    "ELA",
    "ELE",
    "EM",
    "EU",
    "FAZER",
    "FOI",
    "JEITO",
    "LUTAR",
    "MAE",
    "MÃE",
    "ME",
    "MEU",
    "MELHOR",
    "MINHA",
    "MANDOU",
    "NA",
    "NAS",
    "NO",
    "NOS",
    "O",
    "OS",
    "PARA",
    "PATETICO",
    "PATÉTICO",
    "POR",
    "POSSO",
    "QUE",
    "ROUPAS",
    "SE",
    "SUA",
    "TAMBEM",
    "TAMBÉM",
    "TUDO",
    "UM",
    "UMA",
    "VAI",
    "VOCE",
    "VOCE",
    "VOCÊ",
    "VOU",
}


@dataclass
class TextCandidate:
    line: OCRLine
    ignored: bool = False
    ignore_reason: str = ""


@dataclass(frozen=True)
class TextStyle:
    name: str
    fill: tuple[int, int, int]
    stroke_fill: tuple[int, int, int]
    stroke_width: int
    shadow_fill: tuple[int, int, int] | None
    shadow_offset: tuple[int, int]
    brightness: float
    saturation: float
    hue: float


@dataclass
class TextGroup:
    group_id: str
    lines: list[OCRLine] = field(default_factory=list)
    cleanup_lines: list[OCRLine] = field(default_factory=list)
    text: str = ""
    translation: str = ""
    ignored: bool = False
    ignore_reason: str = ""
    sent_to_translation: bool = False
    redrawn: bool = False
    color_name: str = ""
    font_size: int = 0
    region_brightness: float = 0.0
    region_saturation: float = 0.0
    region_hue: float = 0.0
    background_type: str = "unknown"
    background_metrics: dict = field(default_factory=dict)
    classification: str = "unknown"
    inside_balloon_like_region: bool = False
    inside_narration_box_like_region: bool = False
    main_text_score: float = 0.0
    angle_degrees: float = 0.0
    near_image_edge: bool = False
    alignment_score: float = 0.0
    original_text: str = ""
    repaired_text: str = ""
    repair_reason: str = ""
    region_id: str = ""
    region_type: str = "unknown"
    parent_balloon_id: str = ""
    source_engine: str = ""
    quality_score: float = 1.0
    quality_reasons: list[str] = field(default_factory=list)
    fallback_used: bool = False
    fallback_reason: str = ""
    fallback_chain: list[dict] = field(default_factory=list)
    translation_valid: bool = True
    translation_retry_count: int = 0
    translation_validation_reason: str = ""
    rejected_translation: str = ""
    text_overflow_ratio: float = 0.0
    draw_box: tuple | None = None
    safe_area: tuple | None = None
    translation_box: tuple | None = None
    allowed_modification_box: tuple | None = None
    visual_validation: dict = field(default_factory=dict)
    visual_attempts: list[dict] = field(default_factory=list)
    mask_metrics: dict = field(default_factory=dict)
    manual_review_required: bool = False

    @property
    def confidence(self):
        if not self.lines:
            return 0.0
        return sum(line.confidence for line in self.lines) / len(self.lines)

    @property
    def box(self):
        return _union_boxes([line.box for line in self.lines])


def process_image_file(
    image_path,
    ocr_lang,
    translator,
    font_path=None,
    save_out=True,
    debug_folder=None,
    page_index=None,
    return_debug=False,
):
    original = cv2.imread(image_path)
    if original is None:
        if return_debug:
            return None, {"error": "image_load_failed", "image_path": image_path}
        return None

    page_index = page_index or _page_index_from_path(image_path)
    result_img, debug_data = process_image_array(
        original,
        ocr_lang,
        translator,
        font_path=font_path or FONT_PATH,
        debug_folder=debug_folder,
        page_index=page_index,
        image_path=image_path,
    )

    if save_out:
        out_path = _output_path_for(image_path)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        cv2.imwrite(out_path, result_img)
    else:
        out_path = result_img

    if return_debug:
        return out_path, debug_data

    return out_path


def process_image_array(
    original_bgr,
    ocr_lang,
    translator,
    font_path=None,
    debug_folder=None,
    page_index=1,
    image_path=None,
):
    original = original_bgr.copy()
    engine = OCREngine(ocr_lang)
    raw_lines = engine.detect_lines(original)
    candidates, groups = analyze_image_array(original, raw_lines)
    valid_groups = get_translatable_groups(groups)
    translations = _translate_texts(translator, [group.text for group in valid_groups])
    apply_group_translations(valid_groups, translations)
    return render_analyzed_image(
        original,
        raw_lines,
        candidates,
        groups,
        font_path=font_path,
        debug_folder=debug_folder,
        page_index=page_index,
        image_path=image_path,
    )


def analyze_image_array(original_bgr, raw_lines):
    original = original_bgr
    _assign_visual_white_regions(original, raw_lines)
    candidates = [_candidate_from_line(line, original.shape) for line in raw_lines]
    usable_lines = [candidate.line for candidate in candidates if not candidate.ignored]
    groups = _group_lines(usable_lines)
    groups = _split_groups_at_sentence_boundaries(groups)
    _associate_ignored_cleanup_lines(groups, candidates, original.shape)
    _repair_group_texts(groups)
    _filter_groups(groups, original.shape)
    _classify_groups(groups, original)
    _score_group_quality(groups)
    _assign_region_metadata(groups)
    return candidates, groups


def get_translatable_groups(groups):
    return [group for group in groups if _should_translate_group(group)]


def apply_group_translations(groups, translations):
    for group, translation in zip(groups, translations):
        translated = clean_ocr_text(translation) or group.text
        group.translation = _match_source_case(group.text, translated)
        group.sent_to_translation = True
        valid, reason = validate_translation_text(group.text, group.translation, group.classification)
        group.translation_valid = valid
        group.translation_validation_reason = reason


def render_analyzed_image(
    original_bgr,
    raw_lines,
    candidates,
    groups,
    font_path=None,
    debug_folder=None,
    page_index=1,
    image_path=None,
    stage_timings=None,
):
    original = original_bgr.copy()
    valid_groups = get_translatable_groups(groups)
    final = original.copy()
    inpainted = original.copy()
    text_mask = np.zeros(original.shape[:2], dtype=np.uint8)
    allowed_mask = np.zeros(original.shape[:2], dtype=np.uint8)
    inpaint_seconds = 0.0
    redraw_seconds = 0.0

    for group in valid_groups:
        before_group = final.copy()
        group.visual_attempts = []
        group.manual_review_required = False
        accepted = False
        accepted_cleanup_mask = None
        accepted_strategy = ""
        accepted_allowed_mask = None

        for strategy in ("primary", "conservative"):
            inpaint_started = time.perf_counter()
            cleaned, cleanup_mask, mask_metrics = _remove_text_for_group(
                before_group,
                original,
                group,
                strategy=strategy,
            )
            inpaint_seconds += time.perf_counter() - inpaint_started
            group.mask_metrics = mask_metrics

            if not mask_metrics.get("mask_valid", False):
                attempt = {
                    "strategy": strategy,
                    **mask_metrics,
                    "visual_validation_passed": False,
                    "reason": mask_metrics.get("reason", "invalid_cleanup_mask"),
                }
                group.visual_attempts.append(attempt)
                continue

            redraw_started = time.perf_counter()
            rendered = _draw_group_translation(cleaned, group, font_path)
            redraw_seconds += time.perf_counter() - redraw_started
            group_allowed = _draw_allowed_group_mask(
                original.shape,
                group,
                cleanup_mask=cleanup_mask,
            )
            rendered, visual_summary = _enforce_visual_bounds(
                before_group,
                rendered,
                group_allowed,
                group=group,
                mask_metrics=mask_metrics,
            )
            visual_summary["strategy"] = strategy
            group.visual_attempts.append(visual_summary)
            if (
                visual_summary.get("visual_validation_passed")
                and group.text_overflow_ratio <= config.MAX_TEXT_OVERFLOW_RATIO
            ):
                final = rendered
                accepted = True
                accepted_cleanup_mask = cleanup_mask
                accepted_strategy = strategy
                accepted_allowed_mask = group_allowed
                group.visual_validation = visual_summary
                group.redrawn = True
                break

        if accepted:
            text_mask = cv2.bitwise_or(text_mask, accepted_cleanup_mask)
            allowed_mask = cv2.bitwise_or(allowed_mask, accepted_allowed_mask)
            inpainted = _apply_cleanup_mask(
                inpainted,
                original,
                group,
                accepted_cleanup_mask,
                strategy=accepted_strategy,
            )
        else:
            final = before_group
            group.redrawn = False
            group.manual_review_required = True
            group.visual_validation = (
                group.visual_attempts[-1]
                if group.visual_attempts
                else {
                    "visual_validation_passed": False,
                    "reason": "no_safe_render_attempt",
                }
            )

    if stage_timings is not None:
        stage_timings["inpainting"] = stage_timings.get("inpainting", 0.0) + inpaint_seconds
        stage_timings["redraw"] = stage_timings.get("redraw", 0.0) + redraw_seconds

    page_visual_summary = {}
    if config.VISUAL_DIFF_VALIDATION:
        validated, page_visual_summary = _enforce_visual_bounds(
            original,
            final,
            allowed_mask,
        )
        if not page_visual_summary.get("visual_validation_passed", True):
            final = original.copy()
            for group in valid_groups:
                if group.redrawn:
                    group.redrawn = False
                    group.manual_review_required = True
                    group.visual_validation = {
                        **group.visual_validation,
                        "visual_validation_passed": False,
                        "reason": "page_level_visual_guard_rollback",
                    }
        else:
            final = validated

    debug_data = _debug_payload(image_path, raw_lines, candidates, groups)
    debug_data["visual_validation"] = page_visual_summary

    if debug_folder:
        _write_debug_images(
            debug_folder,
            page_index,
            original,
            final,
            raw_lines,
            candidates,
            groups,
            text_mask,
            inpainted,
            debug_data,
            allowed_mask=allowed_mask,
        )

    return final, debug_data


def get_font(font_path, size, role="regular"):
    candidates = []
    if font_path:
        candidates.append(font_path)

    role_candidates = {
        "decorative": [
            r"C:\Windows\Fonts\georgia.ttf",
            r"C:\Windows\Fonts\georgiab.ttf",
            r"C:\Windows\Fonts\calibril.ttf",
        ],
        "shout": [
            r"C:\Windows\Fonts\arialbi.ttf",
            r"C:\Windows\Fonts\ariali.ttf",
            r"C:\Windows\Fonts\segoeuii.ttf",
        ],
        "regular": [
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\calibri.ttf",
            r"C:\Windows\Fonts\arial.ttf",
        ],
    }
    candidates.extend(role_candidates.get(role, role_candidates["regular"]))

    for candidate in candidates:
        try:
            if candidate and os.path.exists(candidate):
                return ImageFont.truetype(candidate, size)
        except Exception:
            pass

    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _candidate_from_line(line, image_shape):
    repaired, reason = repair_ocr_text(line.text)
    assessment = assess_ocr_repair(
        line.text,
        repaired,
        reason,
        confidence=line.confidence,
        source_engine=line.engine,
    )
    if repaired != line.text:
        line.metadata = {
            **(line.metadata or {}),
            "repair_candidate": {
                "original_text": line.text,
                "repaired_text": repaired,
                "repair_reason": reason,
                **assessment,
            },
            "repair_accepted": bool(assessment["accepted"]),
        }
    if repaired != line.text and assessment["accepted"]:
        line.original_text = line.original_text or line.raw_text or line.text
        line.repaired_text = repaired
        line.repair_reason = ";".join(
            part for part in (line.repair_reason, reason) if part
        )
        line.text = repaired
        line.metadata = {
            **(line.metadata or {}),
            "original_text": line.original_text,
            "repaired_text": repaired,
            "repair_reason": line.repair_reason,
        }
    reason = _line_ignore_reason(line, image_shape)
    return TextCandidate(line=line, ignored=bool(reason), ignore_reason=reason)


def _line_ignore_reason(line, image_shape):
    text = clean_ocr_text(line.text)
    if not text:
        return "empty_text"

    upper = text.upper()
    useful_chars = _letters(text)
    h_img, w_img = image_shape[:2]
    x, y, w, h = line.box

    if upper not in SHORT_REAL_TEXTS:
        if len(useful_chars) < 2:
            return "too_few_useful_chars"
        if re.fullmatch(r"[\W\d_]+", text):
            return "number_or_symbols_only"

    if line.confidence < 0.42:
        return "low_confidence"

    if w < 12 or h < 8 or w * h < 80:
        return "box_too_small"

    if w > w_img * 0.96 and h > h_img * 0.35:
        return "box_too_large"

    semantic_span = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", text)
    alpha_ratio = len(useful_chars) / max(
        1,
        len(re.sub(r"\s+", "", semantic_span)),
    )
    if upper not in SHORT_REAL_TEXTS and alpha_ratio < 0.45:
        return "low_alpha_ratio"

    if _looks_like_noise(text):
        return "noise_like_text"

    return ""


def _looks_like_noise(text):
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return True

    if len(compact) <= 2 and compact.upper() not in SHORT_REAL_TEXTS:
        return True

    odd = sum(
        1
        for char in text
        if not (char.isalnum() or char in "'!?.,:; -")
    )
    if odd / max(1, len(text)) > 0.28:
        return True

    letters = _letters(text)
    normalized = unicodedata.normalize("NFKD", text)
    vowels = sum(char in "AEIOUaeiou" for char in normalized)
    if len(letters) >= 5 and vowels == 0:
        return True

    return False


def _group_lines(lines):
    groups = []

    for line in sorted(lines, key=lambda item: (item.box[1], item.box[0])):
        target = None
        for group in groups:
            if _line_belongs_to_group(line, group):
                target = group
                break

        if target is None:
            target = TextGroup(group_id=f"BALAO_{len(groups) + 1}")
            groups.append(target)

        target.lines.append(line)
        target.lines.sort(key=lambda item: (item.box[1], item.box[0]))
        target.text = clean_ocr_text(" ".join(item.text for item in target.lines))

    return groups


def _repair_group_texts(groups):
    for group in groups:
        group.original_text = group.text
        repaired, reason = repair_ocr_text(group.text)
        assessment = assess_ocr_repair(
            group.text,
            repaired,
            reason,
            confidence=group.confidence,
            source_engine=_group_engine(group),
        )
        if repaired != group.text:
            for line in group.lines:
                line.metadata = {
                    **(line.metadata or {}),
                    "group_repair_candidate": {
                        "original_text": group.text,
                        "repaired_text": repaired,
                        "repair_reason": reason,
                        **assessment,
                    },
                }
        if assessment["accepted"]:
            group.text = repaired
            group.repaired_text = repaired
            group.repair_reason = reason
        else:
            group.repaired_text = group.text
            group.repair_reason = ""


def _line_belongs_to_group(line, group):
    gx, gy, gw, gh = group.box
    lx, ly, lw, lh = line.box
    group_bottom = gy + gh
    vertical_gap = ly - group_bottom
    existing_heights = [item.box[3] for item in group.lines if item.box[3] > 0]
    existing_areas = [item.box[2] * item.box[3] for item in group.lines]
    base_height = float(np.median(existing_heights)) if existing_heights else max(1, gh)
    base_area = float(np.median(existing_areas)) if existing_areas else max(1, gw * gh)
    line_letters = len(re.sub(r"[^A-Za-z]", "", line.text or ""))
    line_region_id = int((line.metadata or {}).get("visual_white_region_id") or 0)
    group_region_ids = {
        int((item.metadata or {}).get("visual_white_region_id") or 0)
        for item in group.lines
    }
    group_region_ids.discard(0)
    if line_region_id and group_region_ids and line_region_id not in group_region_ids:
        return False
    if (
        line_letters <= 6
        and (
            lh > base_height * 3.0
            or lw * lh > base_area * 6.0
        )
    ):
        return False
    avg_height = max(base_height, gh / max(1, len(group.lines)))

    if vertical_gap < -max(lh, avg_height) * 0.8:
        return False

    if vertical_gap > max(14, avg_height * 1.35):
        return False

    overlap = max(0, min(gx + gw, lx + lw) - max(gx, lx))
    min_width = max(1, min(gw, lw))
    center_distance = abs((gx + gw / 2) - (lx + lw / 2))
    max_center_distance = min(max(gw, lw) * 0.48, max(72, min_width * 1.35))

    if overlap / min_width >= 0.28:
        return True

    if center_distance <= max_center_distance and vertical_gap <= max(12, avg_height):
        return True

    return False


def _filter_groups(groups, image_shape):
    for group in groups:
        text = clean_ocr_text(group.text)
        group.text = text
        x, y, w, h = group.box
        h_img, w_img = image_shape[:2]

        if not text:
            group.ignored = True
            group.ignore_reason = "empty_group"
        elif group.confidence < 0.48:
            group.ignored = True
            group.ignore_reason = "low_group_confidence"
        elif w * h < 130:
            group.ignored = True
            group.ignore_reason = "group_too_small"
        elif w > w_img * 0.96 and h > h_img * 0.25:
            group.ignored = True
            group.ignore_reason = "group_too_large"
        elif _looks_like_noise(text):
            group.ignored = True
            group.ignore_reason = "noise_like_group"


def _classify_groups(groups, image_bgr):
    h_img, w_img = image_bgr.shape[:2]

    for group in groups:
        if group.ignored:
            group.classification = "unknown"
            continue

        x, y, w, h = group.box
        words = re.findall(r"[A-Za-zÀ-ÿ]+", group.text.upper())
        normalized = re.sub(r"[^A-Z]", "", _ascii_fold(group.text).upper())
        one_short_word = len(words) == 1 and len(words[0]) <= 14
        multiline = len(group.lines) >= 2
        reading_phrase = (
            len(words) >= 2
            and len(group.text) >= 8
            and abs(_group_angle_degrees(group)) < 7
        )

        group.angle_degrees = _group_angle_degrees(group)
        group.near_image_edge = (
            x <= w_img * 0.035
            or y <= h_img * 0.025
            or x + w >= w_img * 0.965
            or y + h >= h_img * 0.975
        )
        group.alignment_score = _group_alignment_score(group)

        balloon_like, narration_like = _enclosure_evidence(image_bgr, group.box)
        group.inside_balloon_like_region = balloon_like
        group.inside_narration_box_like_region = narration_like
        external_narration = _external_narration_evidence(
            group,
            image_bgr,
            words,
            reading_phrase,
        )

        score = 0.0
        score += 0.38 if narration_like else 0.0
        score += 0.32 if balloon_like else 0.0
        score += 0.24 if external_narration else 0.0
        score += 0.18 if multiline else 0.0
        score += 0.12 if multiline and group.alignment_score >= 0.68 else 0.0
        score += 0.12 if group.confidence >= 0.8 else 0.06
        score += 0.08 if len(words) >= 4 else 0.0
        score += 0.24 if reading_phrase else 0.0
        score -= 0.28 if abs(group.angle_degrees) >= 12 else 0.0
        score -= 0.2 if group.near_image_edge else 0.0
        score -= 0.2 if one_short_word and not (balloon_like or narration_like) else 0.0
        score -= 0.12 if w >= w_img * 0.3 or h >= h_img * 0.18 else 0.0
        group.main_text_score = max(0.0, min(1.0, score))

        is_known_sfx = normalized in SFX_WORDS
        diagonal = abs(group.angle_degrees) >= 12
        strongly_styled = diagonal or group.near_image_edge
        region_area_ratio = (w * h) / max(1, w_img * h_img)
        oversized_short_graphic = (
            len(words) <= 2
            and len(normalized) <= 8
            and region_area_ratio >= 0.05
            and (w * h) / max(1, len(normalized)) >= 2500
            and (strongly_styled or len(group.lines) == 1)
        )
        repeated_short_outside_region = (
            len(words) in {2, 3}
            and len(set(words)) == 1
            and len(words[0]) <= 8
            and not (balloon_like or narration_like or external_narration)
        )

        if oversized_short_graphic:
            group.inside_balloon_like_region = False
            group.inside_narration_box_like_region = False
            group.classification = "sfx"
        elif is_known_sfx and len(words) <= 2 and not narration_like:
            group.inside_balloon_like_region = False
            group.inside_narration_box_like_region = False
            group.classification = "sfx"
        elif repeated_short_outside_region:
            group.inside_balloon_like_region = False
            group.inside_narration_box_like_region = False
            group.classification = "sfx"
        elif is_known_sfx and strongly_styled and not narration_like:
            group.inside_balloon_like_region = False
            group.inside_narration_box_like_region = False
            group.classification = "sfx"
        elif narration_like and len(group.lines) == 1 and len(words) <= 3:
            group.classification = "speech"
        elif narration_like:
            group.classification = "narration"
        elif external_narration:
            group.inside_narration_box_like_region = True
            group.classification = "narration"
        elif balloon_like:
            group.classification = "speech"
        elif one_short_word and (is_known_sfx or strongly_styled):
            group.classification = "sfx"
        elif diagonal and len(words) <= 3:
            group.classification = "decorative"
        else:
            group.classification = "unknown"

        _apply_classification_policy(group)
        group.background_type, group.background_metrics = _classify_background_region(
            image_bgr,
            group,
        )
        _refine_classification_with_background(group)


def _refine_classification_with_background(group):
    metrics = group.background_metrics or {}
    if (
        group.background_type == "narration_box"
        and metrics.get("open_white_narration")
        and group.classification in {"speech", "narration", "unknown"}
    ):
        group.classification = "narration"
        group.region_type = "narration"
        group.inside_narration_box_like_region = True
        group.ignored = False
        group.ignore_reason = ""
        return

    if group.classification not in {"speech", "narration", "unknown"}:
        return
    if group.background_type not in {"textured_art", "speed_lines", "unknown"}:
        return

    white_ratio = float(metrics.get("white_pixel_ratio", 1.0))
    saturation = float(metrics.get("saturation_mean", 0.0))
    edge_density = float(metrics.get("edge_density", 0.0))
    local_texture = float(metrics.get("local_texture_mean", 0.0))
    folded = _ascii_fold(group.text).upper()
    words = re.findall(r"[A-Z0-9']+", folded)
    compact = re.sub(r"[^A-Z0-9]", "", folded)
    colored_sign_or_label = (
        1 <= len(words) <= 3
        and 1 <= len(compact) <= 24
        and white_ratio < 0.65
        and saturation >= 25.0
    )
    compact_graphic = (
        len(words) == 1
        and 1 <= len(compact) <= 10
        and compact not in COMMON_ENGLISH_WORDS
        and white_ratio < 0.25
        and (edge_density >= 0.05 or local_texture >= 6.0)
    )
    embedded_in_art = colored_sign_or_label or compact_graphic
    if not embedded_in_art:
        return

    letters_only = compact.isalpha()
    known_sfx = compact in SFX_WORDS
    visually_styled = compact_graphic or (
        abs(group.angle_degrees) >= 8
        or group.background_type == "speed_lines"
    )
    group.classification = (
        "sfx"
        if len(words) == 1 and letters_only and (known_sfx or visually_styled)
        else "decorative"
    )
    group.region_type = group.classification
    group.inside_balloon_like_region = False
    group.inside_narration_box_like_region = False
    group.parent_balloon_id = ""
    if group.classification == "sfx":
        group.background_type = "sfx_area"
        group.background_metrics = {
            **metrics,
            "background_type": "sfx_area",
            "reason": "short_stylized_text_embedded_in_art",
        }
    _apply_classification_policy(group)


def _apply_classification_policy(group):
    if group.classification == "sfx" and not config.TRANSLATE_SFX:
        group.ignored = True
        group.ignore_reason = "sfx_translation_disabled"
    elif group.classification == "decorative":
        group.ignored = True
        group.ignore_reason = "decorative_text"
    elif group.classification == "unknown":
        words = re.findall(r"[A-Za-zÀ-ÿ]+", group.text)
        reading_phrase = (
            len(words) >= 2
            and len(group.text) >= 8
            and abs(group.angle_degrees) < 7
        )
        if config.PRIORITIZE_ENCLOSED_TEXT:
            strong_unknown = (
                group.confidence >= 0.78
                and group.alignment_score >= 0.62
                and (
                    (
                        group.main_text_score >= 0.58
                        and len(group.lines) >= 2
                    )
                    or (
                        group.main_text_score >= 0.15
                        and reading_phrase
                    )
                )
            )
        else:
            strong_unknown = group.confidence >= 0.65 and abs(group.angle_degrees) < 12
        if not strong_unknown:
            group.ignored = True
            group.ignore_reason = "weak_unknown_text"


def _should_translate_group(group):
    if group.ignored:
        return False
    if group.classification in ("speech", "narration"):
        return True
    if group.classification == "sfx":
        return bool(config.TRANSLATE_SFX)
    if group.classification != "unknown":
        return False
    return not group.ignored


def _score_group_quality(groups):
    for group in groups:
        score, reasons = score_group_ocr_quality(group)
        group.quality_score = score
        group.quality_reasons = reasons


def _assign_region_metadata(groups):
    for index, group in enumerate(groups, start=1):
        group.region_id = f"REGION_{index:03}"
        group.region_type = group.classification
        group.parent_balloon_id = group.region_id if group.classification == "speech" else ""
        group.source_engine = _group_engine(group)


def score_group_ocr_quality(group):
    text = clean_ocr_text(group.text)
    reasons = []
    score = 1.0
    if not text:
        return 0.0, ["empty_text"]

    if group.cleanup_lines:
        reasons.append("ignored_line_inside_text_region")
        score -= min(0.45, 0.22 + len(group.cleanup_lines) * 0.1)

    folded = _ascii_fold(text).upper()
    compact = re.sub(r"[^A-Z0-9]", "", folded)
    tokens = re.findall(r"[A-Za-z0-9']+", folded)
    token_letters = [re.sub(r"[^A-Z]", "", token) for token in tokens]

    repair_candidate, repair_reason = repair_ocr_text(text)
    if repair_reason:
        if "segment_compact_english_word" in repair_reason:
            reasons.append("compact_word_segmentation_candidate")
            score -= 0.28
        if "dictionary_edit_distance_repair" in repair_reason:
            reasons.append("dictionary_near_miss")
            score -= 0.24
        if "adjacent_common_word_ocr_typo" in repair_reason:
            reasons.append("adjacent_repeated_word_near_miss")
            score -= 0.2
        if repair_candidate and repair_candidate != text:
            reasons.append("generic_ocr_repair_available")
            score -= 0.08

    for token in token_letters:
        if len(token) < 2 or token in COMMON_ENGLISH_WORDS or token in SFX_WORDS:
            continue
        suggestion, suggestion_score = suggest_english_word(token)
        if suggestion and suggestion_score >= 0.62:
            reasons.append("dictionary_near_miss")
            score -= 0.18
        segmented, segment_score = segment_compact_english_word(token)
        if segmented and segment_score >= 0.58:
            reasons.append("compact_word_segmentation_candidate")
            score -= 0.2
        has_no_vowel = not re.search(r"[AEIOUY]", token)
        has_consonant_run = bool(re.search(r"[BCDFGHJKLMNPQRSTVWXYZ]{3,}", token))
        if len(token) <= 4 and (has_no_vowel or has_consonant_run):
            reasons.append("short_improbable_caps_token")
            score -= 0.18
        elif (
            len(token) in {2, 3}
            and len(token_letters) >= 2
            and group.inside_balloon_like_region
        ):
            reasons.append("unknown_short_token_in_phrase")
            score -= 0.26

    if re.search(r"[A-Z]+[0-9]+[A-Z]*|[0-9]+[A-Z]+", folded):
        reasons.append("alphanumeric_ocr_artifact")
        score -= 0.16

    short_context_tokens = {"HE", "SHE", "HIS", "HER", "THEIR", "THEM"}
    if (
        1 <= len(token_letters) <= 2
        and any(token in short_context_tokens for token in token_letters)
        and len(compact) <= 8
        and group.inside_balloon_like_region
    ):
        reasons.append("short_context_word_needs_region_check")
        score -= 0.42

    terminal_punctuation = bool(re.search(r"[.?!…]\s*$", text))
    source_engine = group.source_engine or _group_engine(group)
    if (
        source_engine == "rapidocr"
        and group.inside_balloon_like_region
        and group.classification in {"speech", "unknown", "narration"}
        and 2 <= len(token_letters) <= 4
        and len(compact) >= 8
        and not terminal_punctuation
    ):
        reasons.append("short_balloon_text_needs_region_context")
        score -= 0.34

    if len(compact) >= 14 and not re.search(r"\s", text):
        reasons.append("long_token_without_spaces")
        score -= 0.24

    consonant_runs = re.findall(r"[BCDFGHJKLMNPQRSTVWXYZ]{5,}", folded)
    if consonant_runs:
        reasons.append("long_consonant_run")
        score -= 0.2

    odd = sum(
        1
        for char in text
        if not (char.isalnum() or char.isspace() or char in "'!?.,:;+-&/()")
    )
    if odd / max(1, len(text)) > 0.14:
        reasons.append("many_improbable_characters")
        score -= 0.18

    if re.search(r"\b\d{2,}[?!]*\b", text) and not re.search(r"\bage\s*\d+", text, re.I):
        reasons.append("improbable_number_token")
        score -= 0.12

    if group.confidence < config.RAPIDOCR_MIN_CONFIDENCE:
        reasons.append("low_confidence")
        score -= 0.28
    elif group.confidence < 0.7:
        reasons.append("medium_low_confidence")
        score -= 0.12

    improbable_tokens = 0
    for token in tokens:
        normalized = re.sub(r"[^A-Z]", "", token.upper())
        if len(normalized) >= 5:
            vowels = sum(char in "AEIOUY" for char in normalized)
            if vowels == 0:
                improbable_tokens += 1
    if improbable_tokens:
        reasons.append("improbable_tokens")
        score -= min(0.26, improbable_tokens * 0.09)

    if _group_seems_cross_region(group):
        reasons.append("possible_cross_region_group")
        score -= 0.28

    if group.ignored and len(tokens) >= 4 and len(compact) >= 18:
        reasons.append("ignored_high_content_text")
        score -= 0.34

    return max(0.0, min(1.0, score)), reasons


def _group_seems_cross_region(group):
    if len(group.lines) < 2:
        return False
    centers = [line.box[0] + line.box[2] / 2 for line in group.lines]
    widths = [max(1, line.box[2]) for line in group.lines]
    heights = [max(1, line.box[3]) for line in group.lines]
    if np.std(centers) > max(120, np.median(widths) * 1.35):
        return True
    sorted_lines = sorted(group.lines, key=lambda line: line.box[1])
    gaps = [
        sorted_lines[i + 1].box[1] - (sorted_lines[i].box[1] + sorted_lines[i].box[3])
        for i in range(len(sorted_lines) - 1)
    ]
    return bool(gaps and max(gaps) > max(30, np.median(heights) * 2.2))


def group_needs_selective_fallback(group):
    if group.source_engine and group.source_engine not in {"rapidocr", "mixed"}:
        return False
    if group.fallback_used:
        return False
    if group.quality_score < config.OCR_GROUP_MIN_QUALITY_SCORE:
        return True
    return bool(
        group.quality_reasons
        and any(
            reason in {
                "dictionary_near_miss",
                "compact_word_segmentation_candidate",
                "adjacent_repeated_word_near_miss",
                "generic_ocr_repair_available",
                "long_token_without_spaces",
                "long_consonant_run",
                "short_improbable_caps_token",
                "short_balloon_text_needs_region_context",
                "alphanumeric_ocr_artifact",
                "possible_cross_region_group",
                "ignored_high_content_text",
                "ignored_line_inside_text_region",
                "unknown_short_token_in_phrase",
            }
            for reason in group.quality_reasons
        )
    )


def apply_selective_ocr_fallbacks(original_bgr, raw_lines, groups, ocr_lang, page_index):
    if not (
        config.OCR_QUALITY_CONTROL
        and config.OCR_REGION_SELECTIVE_FALLBACK
        and config.OCR_ENGINE == "rapidocr"
        and config.OCR_HYBRID_FALLBACK
        and config.OCR_FALLBACK_ENGINE == "paddle"
    ):
        return raw_lines, []

    suspects = [
        group
        for group in groups
        if group_needs_selective_fallback(group)
    ][: config.OCR_GROUP_FALLBACK_MAX_GROUPS]
    if not suspects:
        return raw_lines, []

    updated_lines = list(raw_lines)
    records = []
    consumed_line_ids = set()
    for group in suspects:
        current_quality = group.quality_score
        crop_box = _fallback_crop_box(group, original_bgr.shape)
        x, y, w, h = crop_box
        crop = original_bgr[y : y + h, x : x + w]
        if crop.size == 0:
            continue

        best_lines = []
        best_quality = current_quality
        best_selection_score = current_quality
        best_engine = ""
        attempts = []
        repair_proposal_tokens = _repair_proposal_tokens(group)
        context_text = " ".join(
            [group.text]
            + [line.text for line in group.cleanup_lines if line.text]
        )
        context_tokens = set(
            re.findall(r"[A-Z]+", _ascii_fold(context_text).upper())
        )
        context_compact = re.sub(
            r"[^A-Z0-9]",
            "",
            _ascii_fold(context_text).upper(),
        )
        for engine_name, variant in (("paddle_mobile", "paddle_mobile"), ("paddle", "paddle_full")):
            started = time.perf_counter()
            try:
                engine = OCREngine(ocr_lang, engine=engine_name, fallback_engine="")
                crop_lines = engine.detect_lines(crop, page=page_index)
                elapsed = time.perf_counter() - started
                crop_lines = [_offset_line(line, x, y, variant, group.group_id) for line in crop_lines]
                candidate_groups = _candidate_groups_for_fallback(original_bgr, crop_lines)
                best_candidate = _best_candidate_group(candidate_groups, group.box)
                candidate_lines = (
                    _cleanup_lines_for_group(best_candidate)
                    if best_candidate
                    else []
                )
                candidate_quality = _candidate_quality(best_candidate)
                candidate_text = best_candidate.text if best_candidate else ""
                candidate_tokens = set(
                    re.findall(r"[A-Z]+", _ascii_fold(candidate_text).upper())
                )
                proposal_support = len(candidate_tokens & repair_proposal_tokens)
                context_coverage = (
                    len(candidate_tokens & context_tokens) / max(1, len(context_tokens))
                )
                candidate_compact = re.sub(
                    r"[^A-Z0-9]",
                    "",
                    _ascii_fold(candidate_text).upper(),
                )
                content_retention = min(
                    1.0,
                    len(candidate_compact) / max(1, len(context_compact)),
                )
                shrink_penalty = (
                    0.75
                    if len(context_tokens) >= 2 and content_retention < 0.55
                    else 0.0
                )
                expansion_penalty = (
                    0.35
                    if len(context_compact) >= 8
                    and len(candidate_compact) > len(context_compact) * 1.8
                    else 0.0
                )
                selection_score = (
                    candidate_quality
                    + min(0.12, proposal_support * 0.06)
                    + context_coverage * 0.35
                    + content_retention * 0.35
                    - shrink_penalty
                    - expansion_penalty
                )
                attempts.append(
                    {
                        "engine": variant,
                        "detected_line_count": len(crop_lines),
                        "selected_line_count": len(candidate_lines),
                        "elapsed_seconds": round(elapsed, 6),
                        "quality_score": round(candidate_quality, 4),
                        "selection_score": round(selection_score, 4),
                        "repair_proposal_token_support": proposal_support,
                        "context_token_coverage": round(context_coverage, 4),
                        "content_retention": round(content_retention, 4),
                        "shrink_penalty": round(shrink_penalty, 4),
                        "expansion_penalty": round(expansion_penalty, 4),
                        "text": candidate_text,
                    }
                )
                if candidate_lines and selection_score > best_selection_score + 0.03:
                    best_lines = candidate_lines
                    best_quality = candidate_quality
                    best_selection_score = selection_score
                    best_engine = variant
                severe_original_suspicion = current_quality < 0.45
                candidate_still_suspicious = bool(
                    best_candidate
                    and (
                        candidate_quality < 0.9
                        or best_candidate.quality_reasons
                    )
                )
                if best_quality >= 0.82 and not (
                    variant == "paddle_mobile"
                    and (severe_original_suspicion or candidate_still_suspicious)
                ):
                    break
            except Exception as exc:
                attempts.append(
                    {
                        "engine": variant,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        record = {
            "group_id": group.group_id,
            "region_id": group.region_id or group.group_id,
            "original_text": group.text,
            "original_quality_score": round(current_quality, 4),
            "quality_reasons": list(group.quality_reasons),
            "crop_box": list(crop_box),
            "attempts": attempts,
            "fallback_used": bool(best_lines),
            "fallback_variant": best_engine,
        }
        records.append(record)
        if not best_lines:
            continue

        line_ids = {
            id(line)
            for line in [*group.lines, *group.cleanup_lines]
        }
        consumed_line_ids.update(line_ids)
        updated_lines = [
            line
            for line in updated_lines
            if id(line) not in line_ids
            or _line_is_nonoverlapping_context(line, best_lines)
        ]
        for line in best_lines:
            line.metadata = {
                **(line.metadata or {}),
                "selective_fallback_used": True,
                "fallback_variant": best_engine,
                "fallback_reason": ";".join(group.quality_reasons),
                "original_group_id": group.group_id,
                "original_text": group.text,
            }
        updated_lines.extend(best_lines)

    if consumed_line_ids:
        updated_lines.sort(key=lambda line: (line.box[1], line.box[0]))
    return updated_lines, records


def _fallback_crop_box(group, image_shape):
    box = group.box
    x, y, w, h = box
    h_img, w_img = image_shape[:2]
    pad = config.OCR_GROUP_FALLBACK_PADDING
    if (
        "short_context_word_needs_region_check" in group.quality_reasons
        or w * h < 4200
    ):
        pad = max(pad, 90)
    if any(
        reason in group.quality_reasons
        for reason in (
            "compact_word_segmentation_candidate",
            "dictionary_near_miss",
            "short_balloon_text_needs_region_context",
            "possible_cross_region_group",
            "ignored_high_content_text",
            "ignored_line_inside_text_region",
        )
    ):
        pad = max(pad, 130)
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(w_img, x + w + pad)
    y2 = min(h_img, y + h + pad)
    return x1, y1, max(1, x2 - x1), max(1, y2 - y1)


def _offset_line(line, dx, dy, engine_name, source_group_id):
    polygon = np.asarray(line.polygon, dtype=np.int32).copy()
    polygon[:, 0] += int(dx)
    polygon[:, 1] += int(dy)
    x, y, w, h = _box_from_poly(polygon)
    return OCRLine(
        text=line.text,
        confidence=line.confidence,
        polygon=polygon,
        box=(x, y, w, h),
        raw_text=line.raw_text,
        engine=engine_name,
        page=line.page,
        metadata={
            **(line.metadata or {}),
            "source_engine": engine_name,
            "selective_fallback_from_group": source_group_id,
        },
        original_text=line.original_text or line.raw_text,
        repaired_text=line.repaired_text or line.text,
        repair_reason=line.repair_reason,
    )


def _candidate_groups_for_fallback(original_bgr, crop_lines):
    if not crop_lines:
        return []
    _assign_visual_white_regions(original_bgr, crop_lines)
    candidates = [_candidate_from_line(line, original_bgr.shape) for line in crop_lines]
    usable_lines = [candidate.line for candidate in candidates if not candidate.ignored]
    groups = _group_lines(usable_lines)
    groups = _split_groups_at_sentence_boundaries(groups)
    _associate_ignored_cleanup_lines(groups, candidates, original_bgr.shape)
    _repair_group_texts(groups)
    _filter_groups(groups, original_bgr.shape)
    _classify_groups(groups, original_bgr)
    _score_group_quality(groups)
    _assign_region_metadata(groups)
    return groups


def _split_groups_at_sentence_boundaries(groups):
    result = []
    for group in groups:
        lines = sorted(group.lines, key=lambda item: (item.box[1], item.box[0]))
        if len(lines) < 4:
            result.append(group)
            continue

        median_height = float(np.median([max(1, line.box[3]) for line in lines]))
        split_after = []
        for index, (current, following) in enumerate(zip(lines, lines[1:])):
            vertical_gap = following.box[1] - (current.box[1] + current.box[3])
            sentence_ended = bool(re.search(r"[.!?…]{1,}\s*$", current.text or ""))
            next_has_content = len(re.sub(r"[^A-Za-z]", "", following.text or "")) >= 2
            if (
                sentence_ended
                and next_has_content
                and vertical_gap >= max(6, median_height * 0.35)
            ):
                split_after.append(index + 1)

        if not split_after:
            result.append(group)
            continue

        boundaries = [0, *split_after, len(lines)]
        chunks = [
            lines[start:end]
            for start, end in zip(boundaries, boundaries[1:])
            if end > start
        ]
        if len(chunks) <= 1 or any(len(chunk) == 1 for chunk in chunks):
            result.append(group)
            continue
        for chunk in chunks:
            result.append(
                TextGroup(
                    group_id=f"BALAO_{len(result) + 1}",
                    lines=chunk,
                    text=clean_ocr_text(" ".join(line.text for line in chunk)),
                )
            )

    for index, group in enumerate(result, start=1):
        group.group_id = f"BALAO_{index}"
    return result


def _assign_visual_white_regions(image_bgr, lines):
    if image_bgr is None or image_bgr.size == 0 or not lines:
        return
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    white = np.where(
        (gray >= 230) & (hsv[:, :, 1] <= 35),
        255,
        0,
    ).astype(np.uint8)
    white = cv2.morphologyEx(
        white,
        cv2.MORPH_CLOSE,
        np.ones((5, 5), dtype=np.uint8),
        iterations=1,
    )
    count, labels, stats, _ = cv2.connectedComponentsWithStats(white, connectivity=8)
    minimum_area = max(220, int(gray.size * 0.00018))
    valid_labels = {
        label
        for label in range(1, count)
        if int(stats[label, cv2.CC_STAT_AREA]) >= minimum_area
    }
    if not valid_labels:
        return

    image_height, image_width = gray.shape
    for line in lines:
        x, y, box_width, box_height = line.box
        pad_x = max(5, min(16, int(box_width * 0.12)))
        pad_y = max(4, min(12, int(box_height * 0.20)))
        x1 = max(0, x - pad_x)
        y1 = max(0, y - pad_y)
        x2 = min(image_width, x + box_width + pad_x)
        y2 = min(image_height, y + box_height + pad_y)
        region = labels[y1:y2, x1:x2]
        if region.size == 0:
            continue
        values, frequencies = np.unique(region, return_counts=True)
        candidates = [
            (int(frequency), int(value))
            for value, frequency in zip(values, frequencies)
            if int(value) in valid_labels
        ]
        if not candidates:
            continue
        frequency, label = max(candidates)
        coverage = frequency / max(1, region.size)
        if coverage < 0.12:
            continue
        line.metadata = {
            **(line.metadata or {}),
            "visual_white_region_id": label,
            "visual_white_region_coverage": round(coverage, 4),
        }


def _associate_ignored_cleanup_lines(groups, candidates, image_shape):
    """Attach ignored OCR geometry to the surrounding text region for cleanup.

    Recognition may collapse an otherwise valid visual line to one character.
    The text itself remains ignored, but its polygon is still useful for safely
    removing the original glyphs and for triggering a regional OCR fallback.
    """
    for candidate in candidates:
        if not candidate.ignored:
            continue
        line = candidate.line
        if line.confidence < 0.25 or line.box[2] * line.box[3] < 80:
            continue
        possible = []
        for group in groups:
            safe_box = _safe_draw_box(group.box, image_shape, group)
            if not (
                _center_inside(line.box, safe_box)
                or _box_iou(line.box, group.box) >= 0.08
            ):
                continue
            gx, gy, gw, gh = group.box
            lx, ly, lw, lh = line.box
            horizontal_overlap = max(0, min(gx + gw, lx + lw) - max(gx, lx))
            if horizontal_overlap / max(1, min(gw, lw)) < 0.3:
                continue
            center_gap = abs((gy + gh / 2) - (ly + lh / 2))
            possible.append((center_gap, group))
        if not possible:
            continue
        _, target = min(possible, key=lambda item: item[0])
        if all(id(existing) != id(line) for existing in target.cleanup_lines):
            target.cleanup_lines.append(line)


def _cleanup_lines_for_group(group):
    result = []
    seen = set()
    for line in list(group.lines) + list(group.cleanup_lines):
        key = id(line)
        if key in seen:
            continue
        seen.add(key)
        result.append(line)
    return result


def _best_candidate_quality(groups, reference_box):
    return _candidate_quality(_best_candidate_group(groups, reference_box))


def _best_candidate_text(groups, reference_box):
    best = _best_candidate_group(groups, reference_box)
    return best.text if best else ""


def _best_candidate_group(groups, reference_box):
    candidates = [
        group
        for group in groups
        if _box_iou(group.box, reference_box) >= 0.05
        or _center_inside(group.box, reference_box)
        or _center_inside(reference_box, group.box)
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda group: (
            _candidate_quality(group),
            _box_iou(group.box, reference_box),
            len(group.text),
        ),
    )


def _candidate_quality(group):
    if group is None:
        return 0.0
    if group.ignored and len(group.text) < 8:
        return min(group.quality_score, 0.35)
    return group.quality_score


def _repair_proposal_tokens(group):
    tokens = set()
    for line in group.lines:
        metadata = line.metadata or {}
        candidate = metadata.get("repair_candidate") or {}
        if candidate.get("accepted", False):
            continue
        repaired = candidate.get("repaired_text", "")
        original = candidate.get("original_text", "")
        repaired_tokens = set(re.findall(r"[A-Z]+", _ascii_fold(repaired).upper()))
        original_tokens = set(re.findall(r"[A-Z]+", _ascii_fold(original).upper()))
        tokens.update(repaired_tokens - original_tokens)
    return tokens


def _line_is_nonoverlapping_context(line, fallback_lines):
    if not fallback_lines:
        return False
    text = clean_ocr_text(line.text)
    if not text:
        return False
    if any(_box_iou(line.box, fallback.box) >= 0.18 for fallback in fallback_lines):
        return False
    return len(re.sub(r"[^A-Za-z0-9]", "", text)) <= 3


def _box_iou(left, right):
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    x1 = max(lx, rx)
    y1 = max(ly, ry)
    x2 = min(lx + lw, rx + rw)
    y2 = min(ly + lh, ry + rh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = lw * lh + rw * rh - inter
    return inter / max(1, union)


def _center_inside(inner, outer):
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    cx = ix + iw / 2
    cy = iy + ih / 2
    return ox <= cx <= ox + ow and oy <= cy <= oy + oh


def validate_translation_text(source_text, translation, classification="speech"):
    translated = clean_ocr_text(translation)
    if classification == "sfx" and not config.TRANSLATE_SFX:
        return True, "sfx_preserved"
    if not translated:
        return False, "empty_translation"

    source_tokens = set(re.findall(r"[A-Za-z']+", _ascii_fold(source_text).upper()))
    translated_folded = _ascii_fold(translated).upper()
    translated_tokens = re.findall(r"[A-Za-z']+", translated_folded)
    portuguese_hits = sum(token in PORTUGUESE_MARKERS for token in translated_tokens)
    forbidden = []
    for token in translated_tokens:
        if token in {"A", "O", "E"}:
            continue
        if token in COMMON_ENGLISH_WORDS and token in source_tokens:
            forbidden.append(token)

    longest_english_run = 0
    current_run = 0
    for token in translated_tokens:
        if token in COMMON_ENGLISH_WORDS and token not in {"A", "I", "O", "E"}:
            current_run += 1
            longest_english_run = max(longest_english_run, current_run)
        else:
            current_run = 0

    if forbidden and (
        portuguese_hits
        or len(forbidden) >= 2
        or longest_english_run >= 2
    ):
        return False, "mixed_language_tokens:" + ",".join(sorted(set(forbidden))[:6])

    source_english_tokens = [
        token
        for token in source_tokens
        if token in COMMON_ENGLISH_WORDS and token not in {"I"}
    ]
    translated_english_tokens = [
        token
        for token in translated_tokens
        if token in COMMON_ENGLISH_WORDS and token not in {"I"}
    ]
    if (
        source_english_tokens
        and translated_english_tokens
        and not portuguese_hits
        and len(translated_english_tokens) >= max(1, min(2, len(source_english_tokens)))
    ):
        return False, "untranslated_english_text"

    if len(translated_tokens) == 1 and translated_tokens[0] in COMMON_ENGLISH_WORDS:
        return False, "untranslated_single_english_token"

    return True, "ok"


def validate_and_retry_translations(groups, translator, force=False):
    if not config.TRANSLATION_VALIDATION:
        return []
    retry_records = []
    for group in groups:
        if not group.sent_to_translation:
            continue
        valid, reason = validate_translation_text(group.text, group.translation, group.classification)
        group.translation_valid = valid
        group.translation_validation_reason = reason
        if valid or not config.TRANSLATION_RETRY_ON_MIXED_LANGUAGE:
            continue
        if not hasattr(translator, "translate_strict"):
            group.rejected_translation = group.translation
            group.translation = group.text
            continue
        original_translation = group.translation
        for attempt in range(1, config.TRANSLATION_MAX_RETRIES + 1):
            try:
                candidate = translator.translate_strict(
                    group.text,
                    previous_translation=group.translation,
                    validation_reason=reason,
                    force=force,
                )
            except Exception as exc:
                candidate = ""
                reason = f"strict_retry_error:{type(exc).__name__}"
            candidate = _match_source_case(group.text, clean_ocr_text(candidate))
            valid, new_reason = validate_translation_text(
                group.text,
                candidate,
                group.classification,
            )
            retry_records.append(
                {
                    "group_id": group.group_id,
                    "source": group.text,
                    "previous_translation": group.translation,
                    "candidate_translation": candidate,
                    "attempt": attempt,
                    "valid": valid,
                    "reason": new_reason,
                }
            )
            group.translation_retry_count = attempt
            if valid:
                group.translation = candidate
                group.translation_valid = True
                group.translation_validation_reason = "retry_ok"
                break
            reason = new_reason
            group.translation = candidate or group.translation
        if not group.translation_valid:
            initial_valid, initial_reason = validate_translation_text(
                group.text,
                original_translation,
                group.classification,
            )
            if initial_valid:
                group.translation = original_translation
                group.translation_valid = True
                group.translation_validation_reason = (
                    "kept_initial_translation_after_failed_retry"
                )
            else:
                group.rejected_translation = original_translation
                group.translation = group.text
                group.translation_validation_reason = reason
    return retry_records


def _group_angle_degrees(group):
    angles = []
    for line in group.lines:
        poly = np.asarray(line.polygon, dtype=np.float32).reshape(-1, 2)
        if len(poly) < 2:
            continue
        dx = float(poly[1][0] - poly[0][0])
        dy = float(poly[1][1] - poly[0][1])
        if abs(dx) + abs(dy) < 1:
            continue
        angle = float(np.degrees(np.arctan2(dy, dx)))
        while angle > 90:
            angle -= 180
        while angle < -90:
            angle += 180
        angles.append(angle)
    return float(np.median(angles)) if angles else 0.0


def _group_alignment_score(group):
    if len(group.lines) < 2:
        return 1.0

    centers = np.array([line.box[0] + line.box[2] / 2 for line in group.lines], dtype=np.float32)
    lefts = np.array([line.box[0] for line in group.lines], dtype=np.float32)
    widths = np.array([max(1, line.box[2]) for line in group.lines], dtype=np.float32)
    scale = max(8.0, float(np.median(widths)) * 0.42)
    center_score = 1.0 - min(1.0, float(np.std(centers)) / scale)
    left_score = 1.0 - min(1.0, float(np.std(lefts)) / scale)
    return max(0.0, max(center_score, left_score))


def _external_narration_evidence(group, image_bgr, words, reading_phrase):
    if group.ignored:
        return False
    if not reading_phrase or len(words) < 3:
        return False
    if abs(group.angle_degrees) >= 7:
        return False
    if group.alignment_score < 0.55:
        return False
    if group.confidence < 0.58:
        return False

    x, y, w, h = group.box
    h_img, w_img = image_bgr.shape[:2]
    if w > w_img * 0.82 or h > h_img * 0.22:
        return False

    roi, _ = _classification_roi(image_bgr, group.box)
    if roi.size == 0:
        return False
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    local_std = float(np.std(gray))
    local_mean = float(np.mean(gray))
    uniform_background = local_std <= 58 or local_mean >= 168 or local_mean <= 88
    between_panels_or_top = y <= h_img * 0.2 or _horizontal_whitespace_band(image_bgr, y, h)
    return bool(uniform_background and between_panels_or_top)


def _horizontal_whitespace_band(image_bgr, y, h):
    h_img, w_img = image_bgr.shape[:2]
    pad = max(12, int(h * 1.4))
    y1 = max(0, y - pad)
    y2 = min(h_img, y + h + pad)
    if y2 <= y1:
        return False
    roi = image_bgr[y1:y2, :]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    row_std = np.std(gray, axis=1)
    row_mean = np.mean(gray, axis=1)
    quiet_rows = (row_std < 34) & ((row_mean > 165) | (row_mean < 92))
    return float(np.mean(quiet_rows)) >= 0.28


def _enclosure_evidence(image_bgr, group_box):
    roi, local_box = _classification_roi(image_bgr, group_box)
    if roi.size == 0:
        return False, False

    component = _uniform_container_evidence(roi, local_box)
    contour = _contour_container_evidence(roi, local_box)
    narration_like = component["rectangular"] or contour["rectangular"]
    balloon_like = narration_like or component["enclosed"] or contour["enclosed"]
    return balloon_like, narration_like


def _classification_roi(image_bgr, box):
    x, y, w, h = box
    h_img, w_img = image_bgr.shape[:2]
    pad_x = max(36, min(int(w * 1.15), int(w_img * 0.24)))
    pad_y = max(86, min(int(h * 3.2), int(h_img * 0.26)))
    x1 = max(0, x - pad_x)
    y1 = max(0, y - pad_y)
    x2 = min(w_img, x + w + pad_x)
    y2 = min(h_img, y + h + pad_y)
    return image_bgr[y1:y2, x1:x2], (x - x1, y - y1, w, h)


def _uniform_container_evidence(roi, group_box):
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    value = hsv[:, :, 2]
    saturation = hsv[:, :, 1]
    masks = [
        ((value >= 150) & (saturation <= 155)).astype(np.uint8) * 255,
        (value <= 120).astype(np.uint8) * 255,
    ]
    best = {"enclosed": False, "rectangular": False, "score": 0.0}

    for mask in masks:
        kernel_size = max(5, min(13, int(min(roi.shape[:2]) * 0.035) | 1))
        kernel = np.ones((kernel_size, kernel_size), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)

        gx, gy, gw, gh = group_box
        gcx, gcy = gx + gw / 2, gy + gh / 2
        group_area = max(1, gw * gh)

        for label in range(1, count):
            cx, cy, cw, ch, area = stats[label]
            if not (cx <= gcx <= cx + cw and cy <= gcy <= cy + ch):
                continue
            if cw < gw * 1.12 or ch < gh * 1.12 or area < group_area * 1.25:
                continue

            touches = sum(
                (
                    cx <= 1,
                    cy <= 1,
                    cx + cw >= roi.shape[1] - 1,
                    cy + ch >= roi.shape[0] - 1,
                )
            )
            if touches >= 3:
                continue

            rectangularity = float(area) / max(1, cw * ch)
            margin_score = min(cw / max(1, gw), ch / max(1, gh), 3.0) / 3.0
            score = rectangularity * 0.65 + margin_score * 0.35
            if score > best["score"]:
                best = {
                    "enclosed": True,
                    "rectangular": rectangularity >= 0.76 and touches <= 1,
                    "score": score,
                }

    return best


def _contour_container_evidence(roi, group_box):
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 45, 135)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    gx, gy, gw, gh = group_box
    center = (float(gx + gw / 2), float(gy + gh / 2))
    group_area = max(1, gw * gh)
    best = {"enclosed": False, "rectangular": False, "score": 0.0}

    for contour in contours:
        area = abs(float(cv2.contourArea(contour)))
        if area < group_area * 1.2:
            continue
        if cv2.pointPolygonTest(contour, center, False) < 0:
            continue

        x, y, w, h = cv2.boundingRect(contour)
        if w < gw * 1.1 or h < gh * 1.1:
            continue
        if w >= roi.shape[1] * 0.98 and h >= roi.shape[0] * 0.98:
            continue

        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.025 * perimeter, True)
        rectangularity = area / max(1, w * h)
        rectangular = 4 <= len(approx) <= 10 and rectangularity >= 0.62
        score = min(1.0, area / max(group_area * 3.0, 1.0))
        if score > best["score"]:
            best = {"enclosed": True, "rectangular": rectangular, "score": score}

    return best


def _ascii_fold(text):
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _translate_texts(translator, texts):
    if not texts:
        return []

    if hasattr(translator, "translate_many"):
        try:
            return translator.translate_many(texts)
        except Exception as exc:
            print(f"Falha na traducao em lote. Usando traducao individual: {exc}")

    translate_one = translator.translate if hasattr(translator, "translate") else translator
    translations = []

    for text in texts:
        try:
            translations.append(translate_one(text))
        except Exception as exc:
            print(f"Falha ao traduzir texto. Mantendo original: {exc}")
            translations.append(text)

    return translations


def _match_source_case(source, translation):
    source_letters = _letters(source or "")
    if len(source_letters) < 3:
        return translation

    uppercase_ratio = sum(char.isupper() for char in source_letters) / len(source_letters)
    if uppercase_ratio >= 0.82:
        return str(translation).upper()

    return translation


def _letters(text):
    return [char for char in str(text or "") if char.isalpha()]


def _build_text_mask(image_shape, groups, padding=None):
    mask = np.zeros(image_shape[:2], dtype=np.uint8)

    for group in groups:
        for line in _cleanup_lines_for_group(group):
            line_mask = np.zeros(image_shape[:2], dtype=np.uint8)
            cv2.fillPoly(line_mask, [np.asarray(line.polygon, dtype=np.int32)], 255)
            line_padding = _mask_padding(line.box) if padding is None else max(0, int(padding))
            if line_padding > 0:
                kernel = np.ones((line_padding * 2 + 1, line_padding * 2 + 1), np.uint8)
                line_mask = cv2.dilate(line_mask, kernel, iterations=1)
            mask = cv2.bitwise_or(mask, line_mask)

    return mask


def _expand_poly(poly, image_shape, padding=3):
    x, y, w, h = _box_from_poly(poly)
    h_img, w_img = image_shape[:2]
    x1 = max(0, x - padding)
    y1 = max(0, y - padding)
    x2 = min(w_img - 1, x + w + padding)
    y2 = min(h_img - 1, y + h + padding)
    return np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=np.int32)


def _mask_padding(box):
    _, _, _, h = box
    return max(1, min(config.MAX_MASK_EXPANSION, config.TEXT_MASK_PADDING, int(h * 0.16)))


def _remove_text_with_mask(img_bgr, mask):
    if mask is None or not np.any(mask):
        return img_bgr.copy()

    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.dilate(mask, kernel, iterations=1)
    return cv2.inpaint(img_bgr, mask, 3, cv2.INPAINT_TELEA)


def _remove_text_for_groups(img_bgr, groups):
    if not groups:
        return img_bgr.copy()

    result = img_bgr.copy()
    for group in groups:
        result, _, _ = _remove_text_for_group(
            result,
            img_bgr,
            group,
            strategy="primary",
        )
    return result


def _safe_white_text_cleanup_mask(img_bgr, group, group_mask, draw_box):
    del draw_box
    component_mask, _ = _component_text_mask(
        img_bgr,
        group,
        maximum_mask=group_mask,
        strategy="primary",
    )
    return component_mask


def _classify_background_region(img_bgr, group):
    if group.classification == "sfx":
        return "sfx_area", {"reason": "group_classified_as_sfx"}

    draw_box = _safe_draw_box(group.box, img_bgr.shape, group)
    x, y, w, h = draw_box
    roi = img_bgr[y : y + h, x : x + w]
    if roi.size == 0:
        return "unknown", {"reason": "empty_region"}

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    source_mask = _build_text_mask(img_bgr.shape, [group], padding=2)[y : y + h, x : x + w]
    background_pixels = source_mask == 0
    if np.count_nonzero(background_pixels) < max(32, int(gray.size * 0.18)):
        background_pixels = np.ones(gray.shape, dtype=bool)

    values = gray[background_pixels]
    saturation = hsv[:, :, 1][background_pixels]
    brightness = float(np.mean(values))
    brightness_std = float(np.std(values))
    saturation_mean = float(np.mean(saturation))
    white_ratio = float(np.mean((values >= 220) & (saturation <= 45)))
    dark_ratio = float(np.mean(values <= 75))

    # Text polygons from OCR can occupy nearly the whole tight draw box.  In
    # that case the glyphs themselves make a clean white page look textured.
    # Sample a ring around the group as an independent signal for open
    # narration printed directly on a white page.
    image_h, image_w = img_bgr.shape[:2]
    context_pad_x = max(16, min(64, int(w * 0.10)))
    context_pad_y = max(12, min(64, int(h * 1.50)))
    context_x1 = max(0, x - context_pad_x)
    context_y1 = max(0, y - context_pad_y)
    context_x2 = min(image_w, x + w + context_pad_x)
    context_y2 = min(image_h, y + h + context_pad_y)
    context_roi = img_bgr[context_y1:context_y2, context_x1:context_x2]
    context_gray = cv2.cvtColor(context_roi, cv2.COLOR_BGR2GRAY)
    context_hsv = cv2.cvtColor(context_roi, cv2.COLOR_BGR2HSV)
    context_background = np.ones(context_gray.shape, dtype=bool)
    inner_x1 = max(0, x - context_x1)
    inner_y1 = max(0, y - context_y1)
    inner_x2 = min(context_gray.shape[1], x + w - context_x1)
    inner_y2 = min(context_gray.shape[0], y + h - context_y1)
    context_background[inner_y1:inner_y2, inner_x1:inner_x2] = False
    context_values = context_gray[context_background]
    context_saturation = context_hsv[:, :, 1][context_background]
    context_brightness = float(np.mean(context_values)) if context_values.size else brightness
    context_white_ratio = (
        float(np.mean((context_values >= 220) & (context_saturation <= 45)))
        if context_values.size
        else white_ratio
    )
    context_dark_ratio = (
        float(np.mean(context_values <= 75)) if context_values.size else dark_ratio
    )
    context_saturation_mean = (
        float(np.mean(context_saturation)) if context_saturation.size else saturation_mean
    )

    blurred = cv2.GaussianBlur(gray, (7, 7), 0)
    local_texture = cv2.absdiff(gray, blurred)
    local_texture_mean = float(np.mean(local_texture[background_pixels]))
    edges = cv2.Canny(gray, 55, 155)
    edge_density = float(np.mean((edges > 0)[background_pixels]))
    sobel_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    sobel_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    gradient_strength = float(np.mean(cv2.magnitude(sobel_x, sobel_y)[background_pixels]))

    # Hough must inspect the background, not strokes from the OCR text itself.
    # Otherwise ordinary letters inside a clean white balloon look like dozens
    # of diagonal/action lines and incorrectly disable flat-fill cleanup.
    text_exclusion = cv2.dilate(
        source_mask,
        np.ones((5, 5), dtype=np.uint8),
        iterations=1,
    )
    background_edges = edges.copy()
    background_edges[text_exclusion > 0] = 0

    minimum_line = max(12, int(min(w, h) * 0.16))
    hough = cv2.HoughLinesP(
        background_edges,
        1,
        np.pi / 180,
        threshold=max(12, minimum_line // 2),
        minLineLength=minimum_line,
        maxLineGap=5,
    )
    diagonal_lines = 0
    long_lines = 0
    if hough is not None:
        for entry in hough[:, 0]:
            x1, y1, x2, y2 = (int(value) for value in entry)
            length = float(np.hypot(x2 - x1, y2 - y1))
            if length < minimum_line:
                continue
            long_lines += 1
            angle = abs(float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))) % 180
            acute = min(angle, 180 - angle)
            if 12 <= acute <= 78:
                diagonal_lines += 1

    metrics = {
        "draw_box": list(draw_box),
        "brightness_mean": round(brightness, 3),
        "brightness_std": round(brightness_std, 3),
        "saturation_mean": round(saturation_mean, 3),
        "white_pixel_ratio": round(white_ratio, 4),
        "dark_pixel_ratio": round(dark_ratio, 4),
        "context_brightness_mean": round(context_brightness, 3),
        "context_white_pixel_ratio": round(context_white_ratio, 4),
        "context_dark_pixel_ratio": round(context_dark_ratio, 4),
        "context_saturation_mean": round(context_saturation_mean, 3),
        "local_texture_mean": round(local_texture_mean, 3),
        "edge_density": round(edge_density, 4),
        "gradient_strength": round(gradient_strength, 3),
        "long_line_count": int(long_lines),
        "diagonal_line_count": int(diagonal_lines),
    }
    strongly_uniform_white = (
        brightness >= max(235.0, float(config.WHITE_BACKGROUND_MIN_BRIGHTNESS))
        and brightness_std <= min(34.0, float(config.WHITE_BACKGROUND_MAX_STD))
        and saturation_mean <= min(12.0, float(config.WHITE_BACKGROUND_MAX_SATURATION))
        and white_ratio >= max(0.90, float(config.WHITE_BACKGROUND_MIN_RATIO))
        and local_texture_mean <= min(4.0, float(config.WHITE_BACKGROUND_MAX_TEXTURE))
        and edge_density <= min(0.035, float(config.WHITE_BACKGROUND_MAX_EDGE_DENSITY))
    )
    enclosure_like = bool(
        group.inside_balloon_like_region
        or group.inside_narration_box_like_region
    )
    dominant_white_enclosure = (
        enclosure_like
        and brightness >= config.WHITE_ENCLOSURE_MIN_BRIGHTNESS
        and white_ratio >= config.WHITE_ENCLOSURE_MIN_RATIO
        and dark_ratio <= config.WHITE_ENCLOSURE_MAX_DARK_RATIO
        and saturation_mean <= config.WHITE_ENCLOSURE_MAX_SATURATION
    )
    stylized_white_enclosure = (
        enclosure_like
        and brightness >= config.WHITE_STYLIZED_ENCLOSURE_MIN_BRIGHTNESS
        and white_ratio >= config.WHITE_STYLIZED_ENCLOSURE_MIN_RATIO
        and dark_ratio <= config.WHITE_STYLIZED_ENCLOSURE_MAX_DARK_RATIO
        and saturation_mean <= config.WHITE_STYLIZED_ENCLOSURE_MAX_SATURATION
    )
    compact_text = re.sub(r"[^A-Z0-9]", "", _ascii_fold(group.text).upper())
    near_page_edge = y <= int(image_h * 0.22) or y + h >= int(image_h * 0.78)
    semantic_short_narration = (
        compact_text in COMMON_ENGLISH_WORDS
        and len(compact_text) >= 2
        and near_page_edge
    )
    white_context = (
        context_brightness >= 238.0
        and context_white_ratio >= 0.93
        and context_dark_ratio <= 0.03
        and context_saturation_mean <= 8.0
    )
    open_white_narration = (
        group.classification in {"narration", "unknown"}
        and w >= max(50, int(h * 1.5))
        and (len(compact_text) >= 8 or semantic_short_narration)
        and (
            (
                brightness >= 190.0
                and white_ratio >= 0.70
                and dark_ratio <= 0.21
                and saturation_mean <= 8.0
            )
            or white_context
        )
    )
    strict_uniform_light = (
        brightness >= config.WHITE_BACKGROUND_MIN_BRIGHTNESS
        and brightness_std <= config.WHITE_BACKGROUND_MAX_STD
        and saturation_mean <= config.WHITE_BACKGROUND_MAX_SATURATION
        and white_ratio >= config.WHITE_BACKGROUND_MIN_RATIO
        and local_texture_mean <= config.WHITE_BACKGROUND_MAX_TEXTURE
        and edge_density <= config.WHITE_BACKGROUND_MAX_EDGE_DENSITY
        and (
            strongly_uniform_white
            or dominant_white_enclosure
            or diagonal_lines <= config.WHITE_BACKGROUND_MAX_DIAGONAL_LINES
        )
    )
    uniform_light = bool(
        strict_uniform_light
        or dominant_white_enclosure
        or stylized_white_enclosure
        or open_white_narration
    )
    uniform_dark = (
        brightness <= 92
        and brightness_std <= 42
        and local_texture_mean <= 11
        and edge_density <= 0.14
    )
    speed_lines = (
        not strongly_uniform_white
        and not dominant_white_enclosure
        and not stylized_white_enclosure
        and not open_white_narration
        and
        diagonal_lines >= 3
        and (
            edge_density >= 0.055
            or local_texture_mean >= 8.0
            or gradient_strength >= 42.0
        )
    )
    textured = (
        brightness_std >= 42
        or local_texture_mean >= 12
        or edge_density >= 0.14
        or gradient_strength >= 58
    )

    if open_white_narration:
        background_type = "narration_box"
    elif uniform_light and group.inside_balloon_like_region:
        background_type = "white_balloon"
    elif uniform_light and group.inside_narration_box_like_region:
        background_type = "narration_box"
    elif speed_lines:
        background_type = "speed_lines"
    elif uniform_dark and group.inside_balloon_like_region:
        background_type = "dark_balloon"
    elif textured:
        background_type = "textured_art"
    else:
        background_type = "unknown"
    metrics["background_type"] = background_type
    metrics["uniform_light"] = bool(uniform_light)
    metrics["strict_uniform_light"] = bool(strict_uniform_light)
    metrics["strongly_uniform_white"] = bool(strongly_uniform_white)
    metrics["dominant_white_enclosure"] = bool(dominant_white_enclosure)
    metrics["stylized_white_enclosure"] = bool(stylized_white_enclosure)
    metrics["open_white_narration"] = bool(open_white_narration)
    metrics["background_edge_density"] = round(float(np.mean(background_edges > 0)), 4)
    return background_type, metrics


def _remove_text_for_group(current_bgr, original_bgr, group, strategy="primary"):
    background_type, background_metrics = _classify_background_region(
        original_bgr,
        group,
    )
    group.background_type = background_type
    group.background_metrics = background_metrics
    tight_background = background_type in {
        "textured_art",
        "speed_lines",
        "sfx_area",
        "unknown",
    }
    base_mask = _build_text_mask(original_bgr.shape, [group], padding=0)
    if not np.any(base_mask):
        return current_bgr.copy(), base_mask, {
            "mask_valid": False,
            "reason": "empty_source_polygon",
        }

    limit_padding = 1 if (strategy == "conservative" or tight_background) else min(
        config.MAX_MASK_EXPANSION,
        config.TEXT_MASK_PADDING + 1,
    )
    maximum_mask = base_mask.copy()
    if limit_padding > 0:
        maximum_mask = cv2.dilate(
            maximum_mask,
            np.ones((limit_padding * 2 + 1, limit_padding * 2 + 1), np.uint8),
            iterations=1,
        )
    mask_strategy = "conservative" if tight_background else strategy
    component_mask, component_metrics = _component_text_mask(
        original_bgr,
        group,
        maximum_mask=maximum_mask,
        strategy=mask_strategy,
    )
    cleanup_mask = component_mask
    if not np.any(cleanup_mask):
        return current_bgr.copy(), cleanup_mask, {
            **component_metrics,
            "mask_valid": False,
            "reason": "no_conservative_text_components",
        }

    text_area = max(1, int(component_metrics.get("text_component_pixels", 0)))
    mask_area = int(np.count_nonzero(cleanup_mask))
    area_ratio = mask_area / text_area
    shape_metrics = _mask_shape_metrics(cleanup_mask, group.box)
    metrics = {
        **component_metrics,
        **shape_metrics,
        "background_type": background_type,
        "background_metrics": background_metrics,
        "mask_pixels": mask_area,
        "mask_to_text_area_ratio": round(float(area_ratio), 4),
        "mask_valid": area_ratio <= config.MAX_MASK_TO_TEXT_AREA_RATIO,
        "strategy": strategy,
    }
    if not metrics["mask_valid"]:
        metrics["reason"] = "mask_too_large_for_detected_characters"
        return current_bgr.copy(), cleanup_mask, metrics

    if tight_background and (
        shape_metrics["mask_to_group_area_ratio"]
        > config.MAX_TEXTURED_MASK_GROUP_RATIO
        or shape_metrics["largest_mask_component_to_group_ratio"]
        > config.MAX_TEXTURED_MASK_COMPONENT_RATIO
        or shape_metrics["broad_rectangular_mask"]
    ):
        metrics["mask_valid"] = False
        metrics["reason"] = "broad_mask_rejected_on_nonuniform_background"
        return current_bgr.copy(), cleanup_mask, metrics

    cleaned = _apply_cleanup_mask(
        current_bgr,
        original_bgr,
        group,
        cleanup_mask,
        strategy=strategy,
    )
    white_patch_metrics = _white_patch_artifact_metrics(
        original_bgr,
        cleaned,
        group,
        cleanup_mask,
        background_type,
    )
    metrics.update(white_patch_metrics)
    if white_patch_metrics.get("white_patch_rejected"):
        metrics["mask_valid"] = False
        metrics["reason"] = "large_white_patch_on_nonwhite_background"
        return current_bgr.copy(), cleanup_mask, metrics
    residual_mask, residual_metrics = _component_text_mask(
        cleaned,
        group,
        maximum_mask=maximum_mask,
        strategy="conservative",
    )
    residual_mask = cv2.bitwise_and(residual_mask, cv2.bitwise_not(cleanup_mask))
    residual_pixels = int(np.count_nonzero(residual_mask))
    residual_limit = max(12, int(text_area * 0.035))
    if residual_pixels > residual_limit and strategy == "primary" and not tight_background:
        expanded_cleanup = cv2.bitwise_or(cleanup_mask, residual_mask)
        expanded_area = int(np.count_nonzero(expanded_cleanup))
        expanded_ratio = expanded_area / text_area
        if expanded_ratio <= config.MAX_MASK_TO_TEXT_AREA_RATIO:
            cleanup_mask = expanded_cleanup
            metrics["mask_pixels"] = expanded_area
            metrics["mask_to_text_area_ratio"] = round(float(expanded_ratio), 4)
            cleaned = _apply_cleanup_mask(
                current_bgr,
                original_bgr,
                group,
                cleanup_mask,
                strategy="conservative",
            )
            residual_mask, residual_metrics = _component_text_mask(
                cleaned,
                group,
                maximum_mask=maximum_mask,
                strategy="conservative",
            )
            residual_mask = cv2.bitwise_and(
                residual_mask,
                cv2.bitwise_not(cleanup_mask),
            )
            residual_pixels = int(np.count_nonzero(residual_mask))
            metrics["residual_cleanup_pass_used"] = True
    metrics["residual_text_pixels_after_cleanup"] = residual_pixels
    metrics["residual_text_pixel_limit"] = residual_limit
    metrics["residual_component_count"] = int(
        residual_metrics.get("accepted_text_components", 0)
    )
    if residual_pixels > residual_limit:
        metrics["mask_valid"] = False
        metrics["reason"] = "residual_source_text_after_cleanup"
    return cleaned, cleanup_mask, metrics


def _mask_shape_metrics(mask, group_box):
    ys, xs = np.where(mask > 0)
    gx, gy, gw, gh = group_box
    group_area = max(1, int(gw * gh))
    if not len(xs):
        return {
            "mask_bounding_box": None,
            "mask_bounding_box_fill_ratio": 0.0,
            "mask_to_group_area_ratio": 0.0,
            "largest_mask_component_area": 0,
            "largest_mask_component_to_group_ratio": 0.0,
            "broad_rectangular_mask": False,
        }
    x1, x2 = int(xs.min()), int(xs.max()) + 1
    y1, y2 = int(ys.min()), int(ys.max()) + 1
    bbox_area = max(1, (x2 - x1) * (y2 - y1))
    mask_area = int(len(xs))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    largest = max(
        (int(stats[label, cv2.CC_STAT_AREA]) for label in range(1, count)),
        default=0,
    )
    fill_ratio = mask_area / bbox_area
    group_ratio = mask_area / group_area
    bbox_group_ratio = bbox_area / group_area
    broad_rectangular = bool(
        bbox_group_ratio >= 0.45
        and fill_ratio >= 0.42
        and group_ratio >= 0.20
    )
    return {
        "mask_bounding_box": [x1, y1, x2 - x1, y2 - y1],
        "mask_bounding_box_fill_ratio": round(float(fill_ratio), 4),
        "mask_bounding_box_to_group_ratio": round(float(bbox_group_ratio), 4),
        "mask_to_group_area_ratio": round(float(group_ratio), 4),
        "largest_mask_component_area": int(largest),
        "largest_mask_component_to_group_ratio": round(
            float(largest / group_area),
            4,
        ),
        "broad_rectangular_mask": broad_rectangular,
    }


def _white_patch_artifact_metrics(
    original_bgr,
    cleaned_bgr,
    group,
    cleanup_mask,
    background_type,
):
    if background_type in {"white_balloon", "narration_box"}:
        return {
            "new_white_patch_pixels": 0,
            "largest_new_white_component_area": 0,
            "white_patch_rejected": False,
        }
    original_gray = cv2.cvtColor(original_bgr, cv2.COLOR_BGR2GRAY)
    cleaned_gray = cv2.cvtColor(cleaned_bgr, cv2.COLOR_BGR2GRAY)
    new_white = (
        (cleanup_mask > 0)
        & (original_gray < 220)
        & (cleaned_gray >= 244)
        & ((cleaned_gray.astype(np.int16) - original_gray.astype(np.int16)) >= 24)
    )
    new_white_u8 = new_white.astype(np.uint8) * 255
    count, _, stats, _ = cv2.connectedComponentsWithStats(new_white_u8, 8)
    largest = max(
        (int(stats[label, cv2.CC_STAT_AREA]) for label in range(1, count)),
        default=0,
    )
    pixels = int(np.count_nonzero(new_white))
    group_area = max(1, int(group.box[2] * group.box[3]))
    rejected = bool(
        config.REJECT_WHITE_PATCH_OUTSIDE_BALLOON
        and (largest > config.MAX_OUTSIDE_COMPONENT_AREA or pixels / group_area > 0.01)
    )
    return {
        "new_white_patch_pixels": pixels,
        "largest_new_white_component_area": int(largest),
        "new_white_patch_to_group_ratio": round(float(pixels / group_area), 6),
        "white_patch_rejected": rejected,
    }


def _component_text_mask(img_bgr, group, maximum_mask, strategy="primary"):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    mask = np.zeros(gray.shape, dtype=np.uint8)
    raw_components = np.zeros(gray.shape, dtype=np.uint8)
    accepted_components = 0

    for line in _cleanup_lines_for_group(group):
        polygon_mask = np.zeros(gray.shape, dtype=np.uint8)
        cv2.fillPoly(polygon_mask, [np.asarray(line.polygon, dtype=np.int32)], 255)
        line_limit = polygon_mask.copy()
        line_limit_padding = 1 if strategy == "conservative" else min(
            config.MAX_MASK_EXPANSION,
            config.TEXT_MASK_PADDING + 1,
        )
        if line_limit_padding > 0:
            line_limit = cv2.dilate(
                line_limit,
                np.ones(
                    (line_limit_padding * 2 + 1, line_limit_padding * 2 + 1),
                    np.uint8,
                ),
                iterations=1,
            )
        line_limit = cv2.bitwise_and(line_limit, maximum_mask)
        x, y, w, h = line.box
        x1 = max(0, int(x) - line_limit_padding)
        y1 = max(0, int(y) - line_limit_padding)
        x2 = min(gray.shape[1], int(x + w) + line_limit_padding)
        y2 = min(gray.shape[0], int(y + h) + line_limit_padding)
        if x2 <= x1 or y2 <= y1:
            continue

        roi_gray = gray[y1:y2, x1:x2]
        roi_poly = polygon_mask[y1:y2, x1:x2] > 0
        values = roi_gray[roi_poly]
        if values.size < 8:
            continue
        median = float(np.median(values))
        lower = float(np.percentile(values, 20))
        upper = float(np.percentile(values, 80))
        contrast = max(8.0, upper - lower)
        if median >= 138:
            threshold = max(25.0, min(205.0, median - max(16.0, contrast * 0.28)))
            foreground = roi_gray <= threshold
        elif median <= 118:
            threshold = min(235.0, max(48.0, median + max(16.0, contrast * 0.28)))
            foreground = roi_gray >= threshold
        else:
            blackhat = cv2.morphologyEx(
                roi_gray,
                cv2.MORPH_BLACKHAT,
                np.ones((5, 5), np.uint8),
            )
            tophat = cv2.morphologyEx(
                roi_gray,
                cv2.MORPH_TOPHAT,
                np.ones((5, 5), np.uint8),
            )
            foreground = (blackhat >= 12) | (tophat >= 12)
        foreground &= roi_poly
        foreground = cv2.morphologyEx(
            foreground.astype(np.uint8) * 255,
            cv2.MORPH_OPEN,
            np.ones((2, 2), np.uint8),
        )

        count, labels, stats, _ = cv2.connectedComponentsWithStats(foreground, 8)
        selected = np.zeros_like(foreground)
        line_area = max(1, w * h)
        for label in range(1, count):
            area = int(stats[label, cv2.CC_STAT_AREA])
            cw = int(stats[label, cv2.CC_STAT_WIDTH])
            ch = int(stats[label, cv2.CC_STAT_HEIGHT])
            if area < 2 or area > line_area * 0.34:
                continue
            if cw > w * 0.82 and ch > h * 0.72:
                continue
            selected[labels == label] = 255
            accepted_components += 1

        if np.any(selected):
            if median >= 138:
                weak_foreground = roi_gray <= min(252.0, median - 3.0)
            elif median <= 118:
                weak_foreground = roi_gray >= max(3.0, median + 3.0)
            else:
                weak_foreground = (blackhat >= 4) | (tophat >= 4)
            weak_foreground &= roi_poly
            support = cv2.dilate(
                selected,
                np.ones((7, 7), np.uint8),
                iterations=1,
            )
            antialias_halo = (
                weak_foreground
                & (support > 0)
            ).astype(np.uint8) * 255
            selected = cv2.bitwise_or(selected, antialias_halo)

        raw_components[y1:y2, x1:x2] = cv2.bitwise_or(
            raw_components[y1:y2, x1:x2],
            selected,
        )
        pad = 1 if strategy == "conservative" else min(2, config.TEXT_MASK_PADDING)
        if pad > 0 and np.any(selected):
            selected = cv2.dilate(
                selected,
                np.ones((pad * 2 + 1, pad * 2 + 1), np.uint8),
                iterations=1,
            )
        selected = cv2.bitwise_and(selected, line_limit[y1:y2, x1:x2])
        mask[y1:y2, x1:x2] = cv2.bitwise_or(mask[y1:y2, x1:x2], selected)

    mask = cv2.bitwise_and(mask, maximum_mask)
    return mask, {
        "text_component_pixels": int(np.count_nonzero(raw_components)),
        "accepted_text_components": int(accepted_components),
        "component_based": True,
    }


def _apply_cleanup_mask(current_bgr, original_bgr, group, cleanup_mask, strategy="primary"):
    result = current_bgr.copy()
    if cleanup_mask is None or not np.any(cleanup_mask):
        return result
    draw_box = _safe_draw_box(group.box, original_bgr.shape, group)
    white_region = (
        config.WHITE_BALLOON_FLAT_FILL
        and group.background_type in {"white_balloon", "narration_box"}
        and bool(group.background_metrics.get("uniform_light"))
    )
    if white_region:
        fill_color = _estimated_white_region_fill_color(
            original_bgr,
            cleanup_mask,
            draw_box,
        )
        result[cleanup_mask > 0] = fill_color
    else:
        radius = 1 if strategy == "conservative" else 2
        local = cv2.inpaint(result, cleanup_mask, radius, cv2.INPAINT_TELEA)
        result[cleanup_mask > 0] = local[cleanup_mask > 0]
    return result


def _estimated_background_color(img_bgr, mask, box):
    x, y, w, h = box
    h_img, w_img = img_bgr.shape[:2]
    x1 = max(0, x)
    y1 = max(0, y)
    x2 = min(w_img, x + w)
    y2 = min(h_img, y + h)
    roi = img_bgr[y1:y2, x1:x2]
    roi_mask = mask[y1:y2, x1:x2]
    if roi.size == 0:
        return np.array([245, 245, 245], dtype=np.uint8)
    samples = roi[roi_mask == 0]
    if samples.size == 0:
        samples = roi.reshape(-1, 3)
    color = np.median(samples, axis=0)
    return np.clip(color, 0, 255).astype(np.uint8)


def _estimated_white_region_fill_color(img_bgr, mask, box):
    x, y, w, h = box
    h_img, w_img = img_bgr.shape[:2]
    x1 = max(0, x)
    y1 = max(0, y)
    x2 = min(w_img, x + w)
    y2 = min(h_img, y + h)
    if x2 <= x1 or y2 <= y1:
        return np.array([255, 255, 255], dtype=np.uint8)

    roi = img_bgr[y1:y2, x1:x2]
    roi_mask = mask[y1:y2, x1:x2]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    bright_selected = (roi_mask == 0) & (gray >= 170) & (hsv[:, :, 1] <= 90)
    samples = roi[bright_selected]
    if samples.size == 0:
        ring = cv2.dilate(roi_mask, np.ones((7, 7), np.uint8), iterations=1)
        ring = (ring > 0) & (roi_mask == 0)
        samples = roi[ring]
    if samples.size == 0:
        return np.array([255, 255, 255], dtype=np.uint8)
    color = np.median(samples, axis=0)
    if float(np.mean(color)) < 150:
        color = np.array([245, 245, 245], dtype=np.uint8)
    return np.clip(color, 0, 255).astype(np.uint8)


def _draw_group_translation(img_bgr, group, font_path):
    text = group.translation or group.text
    if not text:
        return img_bgr

    draw_box = _safe_draw_box(group.box, img_bgr.shape, group)
    group.safe_area = tuple(draw_box)
    style = _text_style_for_region(img_bgr, draw_box)
    group.color_name = style.name
    group.region_brightness = style.brightness
    group.region_saturation = style.saturation
    group.region_hue = style.hue

    pil_img = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil_img)
    x, y, w, h = draw_box

    safe_padding = config.TEXT_SAFE_PADDING
    inset_x = max(4, min(safe_padding, int(w * 0.11)))
    inset_y = max(4, min(safe_padding, int(h * 0.14)))
    if w <= 100:
        inset_x = max(4, min(inset_x, int(w * 0.06)))
    content_x = x + inset_x
    content_y = y + inset_y
    content_w = max(12, w - inset_x * 2)
    content_h = max(12, h - inset_y * 2)

    source_line_heights = [line.box[3] for line in group.lines if line.box[3] > 0]
    source_height = float(np.median(source_line_heights)) if source_line_heights else h
    size_scale = 0.72 if style.name == "decorative_purple" else 0.78
    font_size = min(config.MAX_FONT_SIZE, max(config.MIN_FONT_SIZE, int(source_height * size_scale)))
    if len(text) > max(1, len(group.text)) * 1.2:
        font_size = max(config.MIN_FONT_SIZE, int(font_size * 0.92))

    if style.name == "decorative_purple":
        font_role = "decorative"
    elif "!" in group.text:
        font_role = "shout"
    else:
        font_role = "regular"

    lines = []
    spacing = 2
    text_bbox = (0, 0, 0, 0)

    overflow_ratio = 1.0
    while font_size >= config.MIN_FONT_SIZE:
        font = get_font(font_path, font_size, role=font_role)
        spacing = max(1, int(font_size * 0.1))
        wrap_width = int(content_w * 0.8) if style.name == "decorative_purple" else content_w
        lines = (
            _wrap_text(
                draw,
                text,
                font,
                max_width=max(12, wrap_width),
                split_long_words=font_size <= config.MIN_FONT_SIZE,
            )
            if config.AUTO_LINE_WRAP
            else [text]
        )
        text_block = "\n".join(lines)
        text_bbox = draw.multiline_textbbox(
            (0, 0),
            text_block,
            font=font,
            spacing=spacing,
            align="center",
            stroke_width=style.stroke_width,
        )
        text_w = text_bbox[2] - text_bbox[0]
        text_h = text_bbox[3] - text_bbox[1]
        overflow_ratio = max(
            0.0,
            max(text_w - content_w, text_h - content_h) / max(1, max(content_w, content_h)),
        )
        if text_h <= content_h and text_w <= content_w:
            break
        if not config.AUTO_FONT_SHRINK:
            break
        font_size -= 1

    font_size = max(config.MIN_FONT_SIZE, font_size)
    font = get_font(font_path, font_size, role=font_role)
    group.font_size = font_size
    group.text_overflow_ratio = float(overflow_ratio)
    group.draw_box = tuple(draw_box)
    text_block = "\n".join(lines)
    text_w = text_bbox[2] - text_bbox[0]
    text_h = text_bbox[3] - text_bbox[1]
    draw_x = content_x + (content_w - text_w) / 2 - text_bbox[0]
    draw_y = content_y + (content_h - text_h) / 2 - text_bbox[1]

    if group.text_overflow_ratio > config.MAX_TEXT_OVERFLOW_RATIO:
        group.translation_valid = False
        group.translation_validation_reason = "text_does_not_fit_region"
        group.translation_box = None
        return img_bgr

    stroke_pad = max(style.stroke_width, 1)
    tx1 = int(np.floor(draw_x + text_bbox[0] - stroke_pad))
    ty1 = int(np.floor(draw_y + text_bbox[1] - stroke_pad))
    tx2 = int(np.ceil(draw_x + text_bbox[2] + stroke_pad))
    ty2 = int(np.ceil(draw_y + text_bbox[3] + stroke_pad))
    translation_box = (tx1, ty1, max(1, tx2 - tx1), max(1, ty2 - ty1))
    overflow = _box_overflow_ratio(translation_box, draw_box)
    group.text_overflow_ratio = max(group.text_overflow_ratio, overflow)
    group.translation_box = translation_box
    if config.REJECT_TEXT_OVERFLOW and overflow > config.MAX_TEXT_OVERFLOW_RATIO:
        group.translation_valid = False
        group.translation_validation_reason = "translation_box_outside_safe_area"
        return img_bgr

    if style.shadow_fill:
        shadow_x = draw_x + style.shadow_offset[0]
        shadow_y = draw_y + style.shadow_offset[1]
        draw.multiline_text(
            (shadow_x, shadow_y),
            text_block,
            font=font,
            fill=style.shadow_fill,
            spacing=spacing,
            align="center",
        )

    draw.multiline_text(
        (draw_x, draw_y),
        text_block,
        font=font,
        fill=style.fill,
        spacing=spacing,
        align="center",
        stroke_width=style.stroke_width,
        stroke_fill=style.stroke_fill,
    )

    return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)


def _expanded_draw_box(box, image_shape):
    x, y, w, h = box
    h_img, w_img = image_shape[:2]
    pad_x = max(6, min(32, int(w * 0.15)))
    pad_y = max(4, min(18, int(h * 0.22)))
    x1 = max(0, x - pad_x)
    y1 = max(0, y - pad_y)
    x2 = min(w_img, x + w + pad_x)
    y2 = min(h_img, y + h + pad_y)
    return x1, y1, max(1, x2 - x1), max(1, y2 - y1)


def _safe_draw_box(box, image_shape, group=None):
    x, y, w, h = box
    h_img, w_img = image_shape[:2]
    if group and group.inside_balloon_like_region:
        pad_x = max(3, min(8, int(w * 0.08)))
        pad_y = max(2, min(6, int(h * 0.1)))
    elif group and group.inside_narration_box_like_region:
        pad_x = max(4, min(10, int(w * 0.07)))
        pad_y = max(3, min(7, int(h * 0.1)))
    else:
        pad_x = max(2, min(5, int(w * 0.05)))
        pad_y = max(1, min(4, int(h * 0.07)))
    x1 = max(0, x - pad_x)
    y1 = max(0, y - pad_y)
    x2 = min(w_img, x + w + pad_x)
    y2 = min(h_img, y + h + pad_y)
    return x1, y1, max(1, x2 - x1), max(1, y2 - y1)


def _draw_allowed_group_mask(image_shape, group, cleanup_mask=None):
    mask = (
        cleanup_mask.copy()
        if cleanup_mask is not None
        else _build_text_mask(image_shape, [group], padding=0)
    )
    if group.translation_box:
        x, y, w, h = group.translation_box
        pad = min(2, config.MAX_MASK_EXPANSION)
        x1 = max(0, int(x) - pad)
        y1 = max(0, int(y) - pad)
        x2 = min(image_shape[1], int(x + w) + pad)
        y2 = min(image_shape[0], int(y + h) + pad)
        cv2.rectangle(mask, (x1, y1), (x2, y2), 255, -1)
        group.allowed_modification_box = (x1, y1, max(1, x2 - x1), max(1, y2 - y1))
    return mask


def _box_overflow_ratio(inner, outer):
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    x1 = max(ix, ox)
    y1 = max(iy, oy)
    x2 = min(ix + iw, ox + ow)
    y2 = min(iy + ih, oy + oh)
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    return max(0.0, 1.0 - intersection / max(1, iw * ih))


def _enforce_visual_bounds(
    original_bgr,
    final_bgr,
    allowed_mask,
    group=None,
    mask_metrics=None,
):
    if final_bgr.shape != original_bgr.shape:
        return final_bgr, {
            "visual_validation_passed": False,
            "reason": "shape_mismatch",
        }
    gray_diff = cv2.absdiff(
        cv2.cvtColor(original_bgr, cv2.COLOR_BGR2GRAY),
        cv2.cvtColor(final_bgr, cv2.COLOR_BGR2GRAY),
    )
    changed = gray_diff > config.VISUAL_DIFF_THRESHOLD
    allowed = allowed_mask > 0
    outside = changed & ~allowed
    inside = changed & allowed
    outside_ratio = float(np.mean(outside))
    outside_u8 = outside.astype(np.uint8) * 255
    count, _, stats, _ = cv2.connectedComponentsWithStats(outside_u8, 8)
    outside_components = [
        {
            "area": int(stats[label, cv2.CC_STAT_AREA]),
            "width": int(stats[label, cv2.CC_STAT_WIDTH]),
            "height": int(stats[label, cv2.CC_STAT_HEIGHT]),
        }
        for label in range(1, count)
    ]
    largest_component = max((item["area"] for item in outside_components), default=0)
    unexpected_streak = any(
        item["area"] > config.MAX_OUTSIDE_COMPONENT_AREA
        and max(item["width"], item["height"]) / max(1, min(item["width"], item["height"])) >= 8.0
        for item in outside_components
    )
    distance_to_allowed = None
    if np.any(outside) and np.any(allowed):
        distance = cv2.distanceTransform((~allowed).astype(np.uint8), cv2.DIST_L2, 3)
        distance_to_allowed = round(float(np.max(distance[outside])), 3)

    border_change_ratio = 0.0
    if group is not None and group.safe_area:
        x, y, w, h = group.safe_area
        safe_mask = np.zeros_like(allowed_mask)
        cv2.rectangle(safe_mask, (x, y), (x + w, y + h), 255, -1)
        inner = cv2.erode(safe_mask, np.ones((5, 5), np.uint8), iterations=1)
        border_ring = (safe_mask > 0) & (inner == 0)
        original_gray = cv2.cvtColor(original_bgr, cv2.COLOR_BGR2GRAY)
        original_edges = cv2.Canny(original_gray, 55, 155) > 0
        text_exclusion = _build_text_mask(
            original_bgr.shape,
            [group],
            padding=2,
        ) > 0
        protected_border_edges = border_ring & original_edges & ~text_exclusion
        if np.any(protected_border_edges):
            border_change_ratio = float(np.mean(changed[protected_border_edges]))

    metrics = mask_metrics or {}
    mask_ratio = float(metrics.get("mask_to_text_area_ratio", 0.0) or 0.0)
    overflow_ratio = float(group.text_overflow_ratio if group is not None else 0.0)
    reasons = []
    if outside_ratio > config.MAX_OUTSIDE_CHANGE_RATIO:
        reasons.append("outside_change_ratio_exceeded")
    if largest_component > config.MAX_OUTSIDE_COMPONENT_AREA:
        reasons.append("outside_component_too_large")
    if unexpected_streak:
        reasons.append("unexpected_vertical_or_horizontal_streak")
    if mask_ratio > config.MAX_MASK_TO_TEXT_AREA_RATIO:
        reasons.append("mask_area_exceeds_text_area_limit")
    if (
        metrics.get("broad_rectangular_mask")
        and metrics.get("background_type")
        not in {"white_balloon", "narration_box"}
    ):
        reasons.append("broad_rectangular_or_polygonal_mask")
    if metrics.get("white_patch_rejected"):
        reasons.append("large_white_patch_on_nonwhite_background")
    if config.REJECT_TEXT_OVERFLOW and overflow_ratio > config.MAX_TEXT_OVERFLOW_RATIO:
        reasons.append("translation_outside_safe_area")
    if config.REJECT_BALLOON_BORDER_DAMAGE and border_change_ratio > 0.12:
        reasons.append("possible_balloon_border_damage")
    passed = not reasons

    summary = {
        "changed_pixels_inside_mask": int(np.count_nonzero(inside)),
        "changed_pixels_outside_mask": int(np.count_nonzero(outside)),
        "outside_change_ratio": round(outside_ratio, 8),
        "largest_outside_component_area": int(largest_component),
        "max_outside_distance_from_allowed_mask": distance_to_allowed,
        "unexpected_streak": bool(unexpected_streak),
        "balloon_border_change_ratio": round(border_change_ratio, 6),
        "mask_to_text_area_ratio": round(mask_ratio, 4),
        "text_overflow_ratio": round(overflow_ratio, 6),
        "visual_validation_passed": bool(passed),
        "reason": ";".join(reasons),
    }
    return (final_bgr if passed else original_bgr.copy()), summary


def _text_style_for_region(img_bgr, box):
    x, y, w, h = box
    roi = img_bgr[y : y + h, x : x + w]
    if roi.size == 0:
        return TextStyle(
            name="light",
            fill=(40, 35, 48),
            stroke_fill=(245, 242, 248),
            stroke_width=1,
            shadow_fill=None,
            shadow_offset=(0, 0),
            brightness=255.0,
            saturation=0.0,
            hue=0.0,
        )

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    brightness = float(np.mean(gray))
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    saturation = float(np.mean(hsv[:, :, 1]))
    saturated_pixels = hsv[:, :, 1] >= 24
    hue = float(np.median(hsv[:, :, 0][saturated_pixels])) if np.any(saturated_pixels) else 0.0
    purple_pixels = (
        (hsv[:, :, 0] >= 105)
        & (hsv[:, :, 0] <= 165)
        & (hsv[:, :, 1] >= 24)
        & (hsv[:, :, 2] >= 90)
    )
    purple_ratio = float(np.mean(purple_pixels))

    if brightness < 128:
        return TextStyle(
            name="dark",
            fill=(248, 247, 252),
            stroke_fill=(20, 17, 29),
            stroke_width=1,
            shadow_fill=(24, 18, 36),
            shadow_offset=(2, 2),
            brightness=brightness,
            saturation=saturation,
            hue=hue,
        )

    if brightness >= 145 and purple_ratio >= 0.24:
        return TextStyle(
            name="decorative_purple",
            fill=(78, 57, 112),
            stroke_fill=(240, 234, 250),
            stroke_width=1,
            shadow_fill=(207, 194, 229),
            shadow_offset=(1, 1),
            brightness=brightness,
            saturation=saturation,
            hue=hue,
        )

    return TextStyle(
        name="light_colored" if saturation >= 42 else "light",
        fill=(43, 36, 52),
        stroke_fill=(246, 243, 249),
        stroke_width=1,
        shadow_fill=(220, 215, 226) if saturation >= 42 else None,
        shadow_offset=(1, 1) if saturation >= 42 else (0, 0),
        brightness=brightness,
        saturation=saturation,
        hue=hue,
    )


def _wrap_text(draw, text, font, max_width, split_long_words=True):
    words = str(text).split()
    if not words:
        return []

    lines = []
    current = ""

    for word in words:
        if _text_width(draw, word, font) > max_width:
            if current:
                lines.append(current)
                current = ""
            if split_long_words:
                chunks = _split_long_word(draw, word, font, max_width)
                lines.extend(chunks[:-1])
                current = chunks[-1]
            else:
                current = word
            continue

        test = f"{current} {word}".strip()
        if _text_width(draw, test, font) <= max_width or not current:
            current = test
        else:
            lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def _split_long_word(draw, word, font, max_width):
    chunks = []
    current = ""

    for char in word:
        test = current + char
        if current and _text_width(draw, test, font) > max_width:
            chunks.append(current)
            current = char
        else:
            current = test

    if current:
        chunks.append(current)

    return chunks or [word]


def _text_width(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _lines_height(draw, lines, font, stroke_width=0):
    if not lines:
        return 0
    total = 0
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        total += bbox[3] - bbox[1] + 3
    return total


def _widest_line(draw, lines, font, stroke_width=0):
    if not lines:
        return 0
    return max(
        draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)[2]
        - draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)[0]
        for line in lines
    )


def _write_debug_images(
    debug_folder,
    page_index,
    original,
    final,
    raw_lines,
    candidates,
    groups,
    text_mask,
    inpainted,
    debug_data,
    allowed_mask=None,
):
    os.makedirs(debug_folder, exist_ok=True)
    prefix = os.path.join(debug_folder, f"page_{page_index:03}")

    cv2.imwrite(prefix + "_original.png", original)
    cv2.imwrite(prefix + "_ocr_boxes.png", _draw_ocr_debug(original, raw_lines, candidates, groups))
    cv2.imwrite(prefix + "_classified_boxes.png", _draw_classified_debug(original, groups))
    cv2.imwrite(prefix + "_text_mask.png", text_mask)
    cv2.imwrite(prefix + "_mask_initial.png", text_mask)
    cv2.imwrite(prefix + "_inpainted.png", inpainted)
    cv2.imwrite(prefix + "_cleaned_before_text.png", inpainted)
    cv2.imwrite(prefix + "_safe_areas.png", _draw_safe_area_debug(original, groups))
    cv2.imwrite(prefix + "_final.png", final)
    gray_diff = cv2.absdiff(
        cv2.cvtColor(original, cv2.COLOR_BGR2GRAY),
        cv2.cvtColor(final, cv2.COLOR_BGR2GRAY),
    )
    diff_map = cv2.applyColorMap(gray_diff, cv2.COLORMAP_TURBO)
    cv2.imwrite(prefix + "_difference_map.png", diff_map)
    if allowed_mask is None:
        allowed_mask = text_mask
    outside = (gray_diff > config.VISUAL_DIFF_THRESHOLD) & (allowed_mask == 0)
    outside_visual = original.copy()
    outside_visual[outside] = (0, 0, 255)
    cv2.imwrite(prefix + "_outside_changes.png", outside_visual)
    cv2.imwrite(prefix + "_compare.png", _compare_image(original, final))

    with open(prefix + "_ocr.json", "w", encoding="utf-8") as file:
        dump_json(debug_data, file, ensure_ascii=False, indent=2)


def _draw_ocr_debug(original, raw_lines, candidates, groups):
    img = original.copy()

    for candidate in candidates:
        color = (0, 0, 255) if candidate.ignored else (0, 180, 0)
        cv2.polylines(img, [candidate.line.polygon.astype(np.int32)], True, color, 2)

    for group in groups:
        if group.ignored:
            color = (0, 140, 255)
        else:
            color = (255, 0, 0)
        x, y, w, h = group.box
        cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)
        cv2.putText(
            img,
            group.group_id,
            (x, max(12, y - 5)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            color,
            1,
            cv2.LINE_AA,
        )

    return img


def _draw_classified_debug(original, groups):
    img = original.copy()
    colors = {
        "speech": (40, 180, 60),
        "narration": (220, 100, 20),
        "sfx": (0, 210, 255),
        "decorative": (30, 30, 220),
        "unknown": (30, 30, 220),
    }

    for group in groups:
        color = colors.get(group.classification, (30, 30, 220))
        if group.ignored and group.classification != "sfx":
            color = (30, 30, 220)

        x, y, w, h = group.box
        cv2.rectangle(img, (x, y), (x + w, y + h), color, 3)
        status = "OK" if group.sent_to_translation else "SKIP"
        label = f"{group.group_id} {group.classification.upper()} {status}"
        label_y = max(22, y - 8)
        cv2.putText(
            img,
            label,
            (x, label_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.52,
            (0, 0, 0),
            3,
            cv2.LINE_AA,
        )
        cv2.putText(
            img,
            label,
            (x, label_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.52,
            color,
            1,
            cv2.LINE_AA,
        )

    return img


def _draw_safe_area_debug(original, groups):
    img = original.copy()
    for group in groups:
        if not group.safe_area:
            continue
        x, y, w, h = group.safe_area
        color = (30, 210, 30) if group.redrawn else (0, 80, 255)
        cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)
        if group.translation_box:
            tx, ty, tw, th = group.translation_box
            cv2.rectangle(img, (tx, ty), (tx + tw, ty + th), (255, 180, 0), 1)
        cv2.putText(
            img,
            group.group_id,
            (x, max(12, y - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            color,
            1,
            cv2.LINE_AA,
        )
    return img


def _compare_image(original, final):
    h = max(original.shape[0], final.shape[0])
    w = original.shape[1] + final.shape[1]
    canvas = np.full((h, w, 3), 255, dtype=np.uint8)
    canvas[: original.shape[0], : original.shape[1]] = original
    canvas[: final.shape[0], original.shape[1] : original.shape[1] + final.shape[1]] = final
    return canvas


def _debug_payload(image_path, raw_lines, candidates, groups):
    ignored_lines = [
        {
            "id": f"LINE_{idx:03}",
            "raw_text": candidate.line.raw_text,
            "clean_text": candidate.line.text,
            "translation": "",
            "bounding_box": list(candidate.line.box),
                "confidence": candidate.line.confidence,
                "engine": candidate.line.engine,
                "source_engine": candidate.line.engine,
                "page": candidate.line.page,
                "metadata": candidate.line.metadata or {},
            "original_text": candidate.line.original_text or candidate.line.raw_text,
            "repaired_text": candidate.line.repaired_text or candidate.line.text,
            "repair_reason": candidate.line.repair_reason,
            "text_color": "",
                "classification": "unknown",
                "region_id": "",
                "region_type": "unknown",
                "parent_balloon_id": "",
                "inside_balloon_like_region": False,
                "inside_narration_box_like_region": False,
                "quality_score": 0.0,
                "quality_reasons": [],
                "fallback_used": False,
                "fallback_reason": "",
                "translation_valid": False,
                "translation_retry_count": 0,
                "translation_validation_reason": "",
                "translated": False,
            "ignored": True,
            "ignore_reason": candidate.ignore_reason,
            "sent_to_nvidia": False,
            "redrawn": False,
        }
        for idx, candidate in enumerate(candidates, start=1)
        if candidate.ignored
    ]

    group_records = []
    for group in groups:
        group_records.append(
            {
                "id": group.group_id,
                "raw_text": " ".join(line.raw_text for line in group.lines),
                "clean_text": group.text,
                "translation": group.translation,
                "bounding_box": list(group.box),
                "confidence": group.confidence,
                "engine": _group_engine(group),
                "source_engine": group.source_engine or _group_engine(group),
                "page": next(
                    (line.page for line in group.lines if line.page is not None),
                    None,
                ),
                "metadata": _group_ocr_metadata(group),
                "original_text": group.original_text or " ".join(
                    line.original_text or line.raw_text for line in group.lines
                ),
                "repaired_text": group.repaired_text or group.text,
                "repair_reason": group.repair_reason or ";".join(
                    dict.fromkeys(
                        line.repair_reason for line in group.lines if line.repair_reason
                    )
                ),
                "text_color": group.color_name,
                "font_size": group.font_size,
                "region_brightness": round(group.region_brightness, 2),
                "region_saturation": round(group.region_saturation, 2),
                "region_hue": round(group.region_hue, 2),
                "background_type": group.background_type,
                "background_metrics": dict(group.background_metrics),
                "classification": group.classification,
                "region_id": group.region_id,
                "region_type": group.region_type,
                "parent_balloon_id": group.parent_balloon_id,
                "inside_balloon_like_region": group.inside_balloon_like_region,
                "inside_narration_box_like_region": group.inside_narration_box_like_region,
                "main_text_score": round(group.main_text_score, 3),
                "quality_score": round(group.quality_score, 4),
                "quality_reasons": list(group.quality_reasons),
                "fallback_used": bool(group.fallback_used or _group_ocr_metadata(group).get("selective_fallback_used")),
                "fallback_reason": group.fallback_reason
                or _group_ocr_metadata(group).get("fallback_reason", ""),
                "fallback_chain": list(group.fallback_chain),
                "angle_degrees": round(group.angle_degrees, 2),
                "near_image_edge": group.near_image_edge,
                "alignment_score": round(group.alignment_score, 3),
                "translation_valid": group.translation_valid,
                "translation_retry_count": group.translation_retry_count,
                "translation_validation_reason": group.translation_validation_reason,
                "rejected_translation": group.rejected_translation,
                "text_overflow_ratio": round(group.text_overflow_ratio, 6),
                "draw_box": list(group.draw_box) if group.draw_box else None,
                "safe_area": list(group.safe_area) if group.safe_area else None,
                "translation_box": (
                    list(group.translation_box) if group.translation_box else None
                ),
                "allowed_modification_box": (
                    list(group.allowed_modification_box)
                    if group.allowed_modification_box
                    else None
                ),
                "visual_validation": group.visual_validation,
                "visual_attempts": list(group.visual_attempts),
                "mask_metrics": dict(group.mask_metrics),
                "manual_review_required": bool(group.manual_review_required),
                "translated": group.sent_to_translation,
                "ignored": group.ignored,
                "ignore_reason": group.ignore_reason,
                "sent_to_nvidia": group.sent_to_translation,
                "redrawn": group.redrawn,
                "line_count": len(group.lines),
                "cleanup_line_count": len(group.cleanup_lines),
                "cleanup_line_boxes": [
                    list(line.box) for line in group.cleanup_lines
                ],
            }
        )

    classification_counts = {
        name: sum(1 for group in groups if group.classification == name)
        for name in ("speech", "narration", "sfx", "decorative", "unknown")
    }

    return {
        "image_path": image_path,
        "ocr_line_count": len(raw_lines),
        "ignored_line_count": len(ignored_lines),
        "ignored_group_count": sum(1 for group in groups if group.ignored),
        "group_count": len(groups),
        "translated_group_count": sum(1 for group in groups if group.sent_to_translation),
        "redrawn_group_count": sum(1 for group in groups if group.redrawn),
        "classification_counts": classification_counts,
        "items": group_records + ignored_lines,
    }


def _group_engine(group):
    engines = [
        line.engine
        for line in group.lines
        if line.engine
    ]
    return engines[0] if engines and len(set(engines)) == 1 else "mixed"


def _group_ocr_metadata(group):
    for line in group.lines:
        if line.metadata:
            return line.metadata
    return {}


def _union_boxes(boxes):
    if not boxes:
        return 0, 0, 1, 1
    x1 = min(box[0] for box in boxes)
    y1 = min(box[1] for box in boxes)
    x2 = max(box[0] + box[2] for box in boxes)
    y2 = max(box[1] + box[3] for box in boxes)
    return x1, y1, max(1, x2 - x1), max(1, y2 - y1)


def _box_from_poly(poly):
    xs = poly[:, 0]
    ys = poly[:, 1]
    x1 = int(xs.min())
    y1 = int(ys.min())
    x2 = int(xs.max())
    y2 = int(ys.max())
    return x1, y1, max(1, x2 - x1), max(1, y2 - y1)


def _output_path_for(image_path):
    normalized = os.path.normpath(image_path)
    temp_folder = os.path.normpath(TEMP_FOLDER)

    if temp_folder in normalized:
        return normalized.replace(temp_folder, os.path.normpath(TEMP_OUT), 1)

    return os.path.join(TEMP_OUT, os.path.basename(image_path))


def _page_index_from_path(image_path):
    match = re.search(r"(\d+)", os.path.basename(image_path))
    return int(match.group(1)) if match else 1
