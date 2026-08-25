import difflib
import json
import hashlib
import math
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import config
import region_taxonomy
import font_fidelity
import ocr_line_provenance
import source_completeness
import semantic_fidelity
from classification_profiler import profile_step, record_count, record_group
from json_utils import dump_json
from ocr_engine import (
    COMMON_ENGLISH_WORDS as OCR_REPAIR_ENGLISH_WORDS,
    OCREngine,
    OCRLine,
    assess_ocr_repair,
    clean_ocr_text,
    repair_ocr_text,
    segment_compact_english_word,
    suggest_english_word,
)
from fast_ocr_policy import run_ocr_with_timeout

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

TRANSLATION_TERMINAL_STATES = frozenset(
    {
        "translated",
        "rejected",
        "manual_review",
        "preserved_original",
        "translation_failed",
        "skipped_with_reason",
    }
)

PROPER_NAME_ONLY_REASON = "proper_name_only"

# A source the recogniser never read is still a review item, but it is not a
# translator failure: reporting it as untranslated dialogue overstates how much
# ordinary text the translator left behind and hides the real defect, which is the
# read itself. It carries its own reason so the accounting can tell the two apart.
OCR_UNINTELLIGIBLE_SOURCE_REASON = "ocr_unintelligible_source_after_retries"

# Art reconstruction is a quality dimension of its own: source text can be gone
# and the artwork still be unacceptable, and artwork can be intact while the
# source text survives.  These are the mask/reconstruction rejections that mean
# "the artwork could not be rebuilt safely", as opposed to translation or OCR
# problems, and they route a region to structured review instead of letting it
# receive a destructive fill.
REVIEW_REQUIRED_ART_RECONSTRUCTION = "review_required_art_reconstruction"
ART_RECONSTRUCTION_REVIEW_REASONS = frozenset({
    "flat_reconstruction_patch_on_textured_background",
    "large_white_patch_on_nonwhite_background",
    "dark_blotch_created_on_textured_art",
    "broad_mask_rejected_on_nonuniform_background",
    "residual_source_text_after_cleanup",
    "source_scoped_region_too_large_for_safe_cleanup",
    "mask_too_large_for_detected_characters",
})


def art_reconstruction_verdict(visual_attempts, *, accepted):
    """Classify the art-reconstruction outcome independently of text coverage."""
    if accepted:
        return "clean", ""
    for attempt in reversed(list(visual_attempts or [])):
        reason = str(attempt.get("reason") or "")
        if reason in ART_RECONSTRUCTION_REVIEW_REASONS:
            return "review", reason
    return "review", REVIEW_REQUIRED_ART_RECONSTRUCTION

# Terminal reasons that record a proven, specific conclusion about a group. A later
# stage may still preserve the original pixels for such a group, but it must not
# replace the proven reason with its own generic one: the accounting keys off the
# reason, so a downgrade silently turns a settled name back into an untranslated echo
# and holds the whole chapter in review. Kept as a set so more proven reasons can join.
STICKY_TERMINAL_REASONS = frozenset({PROPER_NAME_ONLY_REASON})

REVIEW_TERMINAL_STATES = frozenset({"rejected", "manual_review", "translation_failed"})

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
    "HELP",
    "HAVE",
    "HOLY",
    "HEAD",
    "HE",
    "HER",
    "HIM",
    "HIS",
    "I",
    "IT",
    "LIKE",
    "LATE",
    "LUCKY",
    "MONSTER",
    "MET",
    "MOM",
    "MY",
    "AWAY",
    "BOSS",
    "CRAP",
    "FEEL",
    "REASON",
    "OR",
    "OF",
    "ON",
    "PLANKS",
    "REALLY",
    "RESONATED",
    "SHE",
    "SO",
    "SOON",
    "SOMEONE",
    "STOP",
    "THE",
    "THESE",
    "THAT",
    "THANK",
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
    "YOUR",
}

RESIDUAL_TRANSLATION_ENGLISH_WORDS = COMMON_ENGLISH_WORDS | {
    # Generic residual English vocabulary used only to validate final
    # speech/narration translations.  SFX/decorative preservation is decided
    # before this stage by classification policy.
    "ANYWAY",
    "BLOOD",
    "CORPSE",
    "DOOR",
    "EEK",
    "FOG",
    "HELL",
    "HERE",
    "HEY",
    "IS",
    "ONE",
    "PERSON",
    "RUN",
    "SHE'S",
    "STATION",
    "TRAIN",
    "WAY",
    "WHERE",
    "WHOA",
}

OCR_LEXICAL_REFERENCE_WORDS = {
    re.sub(r"[^A-Z]", "", word.upper())
    for word in (
        COMMON_ENGLISH_WORDS
        | RESIDUAL_TRANSLATION_ENGLISH_WORDS
        | SFX_WORDS
        | {word.upper() for word in OCR_REPAIR_ENGLISH_WORDS}
    )
}

# Ordinary English *dialogue* vocabulary, used only to decide classification and
# translation routing. Deliberately separate from
# ``RESIDUAL_TRANSLATION_ENGLISH_WORDS``: a word like "NO" is also Portuguese, so
# it must never count as untranslated English inside a candidate. Sound effects
# are excluded so an onomatopoeia never reads as ordinary speech. The extra
# entries are the high-frequency short words the compact OCR repair lexicon never
# needed, which is why a lone "NO..." carried no lexical evidence at all.
ORDINARY_DIALOGUE_WORDS = frozenset(
    OCR_LEXICAL_REFERENCE_WORDS
    - {re.sub(r"[^A-Z]", "", word.upper()) for word in SFX_WORDS}
) | frozenset(
    {
        "HELP",
        "HEY",
        "NO",
        "OKAY",
        "PLEASE",
        "RUN",
        "SORRY",
        "WAIT",
        "YES",
    }
)

ENGLISH_INFLECTION_BASE_WORDS = frozenset(
    re.sub(r"[^A-Z]", "", word.upper())
    for word in (
        RESIDUAL_TRANSLATION_ENGLISH_WORDS
        | {word.upper() for word in OCR_REPAIR_ENGLISH_WORDS}
    )
) | {
    # Common inflectable dialogue roots absent from the compact OCR lexicon.
    "BAR",
    "CRY",
    "DRINK",
    "LIGHT",
    "LOOK",
    "MAP",
    "MONSTER",
    "WAIT",
    "WALK",
}

PORTUGUESE_MARKERS = {
    "A",
    "ALGUM",
    "ALGUMA",
    "ALGUNS",
    "ALGUMAS",
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

# The OCR repair lexicon is broader than the compact high-confidence residual
# list above.  It is too permissive for rejecting a single token (many short
# words are shared by languages), but three source-preserved lexicon words in
# one candidate form an objective English-clause signal.  Portuguese markers
# and allowed proper names remain excluded below.
SOURCE_PRESERVED_ENGLISH_PHRASE_WORDS = frozenset(
    re.sub(r"[^A-Z']", "", str(word).upper())
    for word in OCR_REPAIR_ENGLISH_WORDS
) - PORTUGUESE_MARKERS - {"I"}


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
    font_runtime_validation: dict = field(default_factory=dict)
    region_brightness: float = 0.0
    region_saturation: float = 0.0
    region_hue: float = 0.0
    background_type: str = "unknown"
    background_metrics: dict = field(default_factory=dict)
    classification: str = "unknown"
    classification_reason: str = ""
    classification_confidence: float = 0.0
    classification_evidence: dict = field(default_factory=dict)
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
    raw_provider_candidate: str = ""
    raw_provider_candidate_class: str = ""
    retry_candidate: str = ""
    retry_candidate_class: str = ""
    translation_candidate: str = ""
    translation_final_state: str = ""
    translation_final_reason: str = ""
    translation_quality_impact: str = ""
    preserved_original: bool = False
    translation_unresolved: bool = False
    translation_attempts_exhausted: bool = False
    trusted_translation_missing: bool = False
    source_fallback_prevented: bool = False
    text_overflow_ratio: float = 0.0
    draw_box: tuple | None = None
    safe_area: tuple | None = None
    translation_box: tuple | None = None
    allowed_modification_box: tuple | None = None
    visual_validation: dict = field(default_factory=dict)
    source_completeness: dict = field(default_factory=dict)
    visual_attempts: list[dict] = field(default_factory=list)
    mask_metrics: dict = field(default_factory=dict)
    manual_review_required: bool = False
    detected_proper_names: list[str] = field(default_factory=list)
    preserve_as_name: bool = False
    ocr_quality_blocked: bool = False
    ocr_quality_block_reason: str = ""
    quality_evidence: dict = field(default_factory=dict)
    naturalization_eligible: bool = False
    naturalization_attempted: bool = False
    naturalization_accepted: bool = False
    naturalization_status: str = ""
    naturalization_candidate: str = ""
    naturalization_rejected_reason: str = ""
    naturalization_selected_version: str = ""
    naturalization_context: dict = field(default_factory=dict)
    art_reconstruction_status: str = ""
    art_reconstruction_reason: str = ""

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


def analyze_image_array(original_bgr, raw_lines, page_index=None):
    with ocr_line_provenance.page(page_index):
        return _analyze_image_array(original_bgr, raw_lines, page_index=page_index)


def _analyze_image_array(original_bgr, raw_lines, page_index=None):
    original = original_bgr
    # Snapshot the incoming lines *before* ``_candidate_from_line`` rewrites
    # ``OCRLine.text`` on these very objects.
    ocr_line_provenance.record_input_lines(raw_lines, origin="analyze_input")
    with profile_step(
        "analyze.assign_visual_white_regions",
        page_index=page_index,
        items=len(raw_lines or []),
    ):
        _assign_visual_white_regions(original, raw_lines)
    with profile_step(
        "analyze.candidate_from_line",
        page_index=page_index,
        items=len(raw_lines or []),
    ):
        candidates = [_candidate_from_line(line, original.shape) for line in raw_lines]
    usable_lines = [candidate.line for candidate in candidates if not candidate.ignored]
    for candidate in candidates:
        if candidate.ignored:
            ocr_line_provenance.record_filtered(
                candidate.line,
                candidate.ignore_reason or "ignored",
            )
    record_count("lines.usable", len(usable_lines), page_index=page_index)
    record_count("lines.ignored", len(candidates) - len(usable_lines), page_index=page_index)
    with profile_step(
        "analyze.group_lines",
        page_index=page_index,
        items=len(usable_lines),
    ):
        groups = _group_lines(usable_lines, page_index=page_index)
    with profile_step(
        "analyze.split_sentence_boundaries",
        page_index=page_index,
        items=len(groups),
    ):
        groups = _split_groups_at_sentence_boundaries(groups)
    with profile_step(
        "analyze.split_detached_story_outliers",
        page_index=page_index,
        items=len(groups),
    ):
        groups = _split_detached_story_outlier_lines(groups)
    with profile_step(
        "analyze.reclaim_short_lexical_lines",
        page_index=page_index,
        items=len(candidates),
    ):
        _reclaim_short_lexical_lines(groups, candidates, original.shape)
    with profile_step(
        "analyze.associate_ignored_cleanup_lines",
        page_index=page_index,
        items=len(candidates),
    ):
        _associate_ignored_cleanup_lines(groups, candidates, original.shape, page_index=page_index)
    with profile_step(
        "analyze.repair_group_texts",
        page_index=page_index,
        items=len(groups),
    ):
        _repair_group_texts(groups)
    with profile_step(
        "analyze.filter_groups",
        page_index=page_index,
        items=len(groups),
    ):
        _filter_groups(groups, original.shape)
    with profile_step(
        "analyze.classify_groups",
        page_index=page_index,
        items=len(groups),
    ):
        _classify_groups(groups, original, page_index=page_index)
    with profile_step(
        "analyze.score_group_quality",
        page_index=page_index,
        items=len(groups),
    ):
        _score_group_quality(groups)
    with profile_step(
        "analyze.assign_region_metadata",
        page_index=page_index,
        items=len(groups),
    ):
        _assign_region_metadata(groups)
    for group in groups:
        ocr_line_provenance.record_group(group)
    return candidates, groups


def get_translatable_groups(groups):
    return [group for group in groups if _should_translate_group(group)]


def normalize_recurring_compact_names(groups):
    """Recover split proper names using chapter-level OCR consensus.

    Some names are also valid sequences of common English words when split.
    We only join such a phrase when the compact form is observed in a strong
    vocative/name position and the spaced form recurs elsewhere in the same
    chapter. This keeps the rule dynamic and avoids a title-specific glossary.
    """
    groups = list(groups or [])
    compact_evidence = {}
    group_texts = [clean_ocr_text(group.text) for group in groups]
    for group, text in zip(groups, group_texts):
        for token in re.findall(r"[A-Za-z]{5,}", text):
            compact = token.upper()
            if compact in COMMON_ENGLISH_WORDS or compact in SFX_WORDS:
                continue
            segmented, confidence = segment_compact_english_word(compact)
            words = segmented.upper().split()
            if confidence < 0.58 or len(words) != 2:
                continue
            if not _compact_token_has_name_context(text, token):
                continue
            compact_evidence.setdefault(
                compact,
                {
                    "canonical": token,
                    "words": words,
                    "context_groups": set(),
                },
            )["context_groups"].add(group.group_id)

    repairs = []
    for compact, evidence in compact_evidence.items():
        phrase_pattern = re.compile(
            rf"\b{re.escape(evidence['words'][0])}\s+{re.escape(evidence['words'][1])}\b",
            flags=re.IGNORECASE,
        )
        matching_groups = [
            group
            for group in groups
            if phrase_pattern.search(clean_ocr_text(group.text))
        ]
        compact_context_count = len(evidence.get("context_groups") or [])
        if len(matching_groups) < 2 and compact_context_count < 2:
            continue

        canonical = evidence["canonical"]
        compact_pattern = re.compile(rf"\b{re.escape(compact)}\b", re.IGNORECASE)
        for group in groups:
            if compact_pattern.search(clean_ocr_text(group.text)):
                if compact not in group.detected_proper_names:
                    group.detected_proper_names.append(compact)
                if re.fullmatch(
                    rf"\s*{re.escape(compact)}\s*[.!?â€¦]*\s*",
                    clean_ocr_text(group.text),
                    flags=re.IGNORECASE,
                ):
                    group.preserve_as_name = True
        for group in matching_groups:
            original = group.text
            replacement = _match_compact_name_case(
                phrase_pattern.search(original).group(0),
                compact,
            )
            updated = phrase_pattern.sub(replacement, original)
            if updated == original:
                continue
            group.original_text = group.original_text or original
            group.text = updated
            group.repaired_text = updated
            group.repair_reason = ";".join(
                part
                for part in (
                    group.repair_reason,
                    "chapter_consensus_compact_name",
                )
                if part
            )
            if compact not in group.detected_proper_names:
                group.detected_proper_names.append(compact)
            if re.fullmatch(
                rf"\s*{re.escape(compact)}\s*[.!?â€¦]*\s*",
                clean_ocr_text(updated),
                flags=re.IGNORECASE,
            ):
                group.preserve_as_name = True
            repairs.append(
                {
                    "group_id": group.group_id,
                    "original_text": original,
                    "repaired_text": updated,
                    "repair_reason": "chapter_consensus_compact_name",
                    "compact_evidence": canonical,
                    "spaced_evidence_count": len(matching_groups),
                    "compact_context_count": compact_context_count,
                }
            )
    repairs.extend(_normalize_vocative_name_variants(groups))
    return repairs


def _normalize_vocative_name_variants(groups):
    observations = []
    for group in groups:
        text = clean_ocr_text(group.text)
        matches = [
            *re.finditer(r"^\s*([A-Za-z]{4,20})\s*,", text),
            *re.finditer(r",\s*([A-Za-z]{4,20})\s*[.!?â€¦]*\s*$", text),
        ]
        for match in matches:
            token = match.group(1).upper()
            if token in COMMON_ENGLISH_WORDS or token in SFX_WORDS:
                continue
            observations.append(
                {
                    "token": token,
                    "group": group,
                    "confidence": float(group.confidence),
                }
            )

    repairs = []
    handled = set()
    for observation in observations:
        token = observation["token"]
        if token in handled:
            continue
        cluster = [
            item
            for item in observations
            if abs(len(item["token"]) - len(token)) <= 1
            and _token_edit_distance(item["token"], token) <= 1
        ]
        variants = {item["token"] for item in cluster}
        group_ids = {id(item["group"]) for item in cluster}
        if len(variants) < 2 or len(group_ids) < 2:
            continue
        handled.update(variants)
        variant_scores = {}
        for variant in variants:
            matching = [item for item in cluster if item["token"] == variant]
            variant_scores[variant] = (
                len(matching),
                sum(item["confidence"] for item in matching) / len(matching),
            )
        canonical = max(
            variants,
            key=lambda variant: (
                variant_scores[variant][0],
                variant_scores[variant][1],
                variant,
            ),
        )
        variant_pattern = re.compile(
            r"\b(?:" + "|".join(re.escape(item) for item in variants) + r")\b",
            flags=re.IGNORECASE,
        )
        for group in groups:
            original = group.text
            if not variant_pattern.search(original):
                continue
            updated = variant_pattern.sub(
                lambda match: _match_compact_name_case(match.group(0), canonical),
                original,
            )
            if canonical not in group.detected_proper_names:
                group.detected_proper_names.append(canonical)
            if re.fullmatch(
                rf"\s*{re.escape(canonical)}\s*[.!?â€¦]*\s*",
                clean_ocr_text(updated),
                flags=re.IGNORECASE,
            ):
                group.preserve_as_name = True
            if updated == original:
                continue
            group.original_text = group.original_text or original
            group.text = updated
            group.repaired_text = updated
            group.repair_reason = ";".join(
                part
                for part in (
                    group.repair_reason,
                    "chapter_consensus_name_variant",
                )
                if part
            )
            repairs.append(
                {
                    "group_id": group.group_id,
                    "original_text": original,
                    "repaired_text": updated,
                    "repair_reason": "chapter_consensus_name_variant",
                    "variants": sorted(variants),
                    "canonical": canonical,
                }
            )
    return repairs


def _token_edit_distance(left, right):
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def _compact_token_has_name_context(text, token):
    escaped = re.escape(token)
    patterns = (
        rf",\s*{escaped}\s*[.!?â€¦]*\s*$",
        rf"^\s*{escaped}\s*[,!?â€¦]",
        rf"\b(?:WITH|TO|FOR|DEAR)\s+{escaped}\s*[.!?â€¦]",
        rf"^\s*{escaped}\s+(?:IS|WAS|HAS|WILL|CAN)\b",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _match_compact_name_case(source_phrase, compact):
    if source_phrase.islower():
        return compact.lower()
    if source_phrase.istitle():
        return compact.title()
    return compact.upper()


def _group_has_mixed_case_name_evidence(group):
    if group.classification not in {"speech", "thought", "narration", "unknown"}:
        return False
    source_tokens = re.findall(r"[A-Z]{3,}", _ascii_fold(group.text).upper())
    if not 2 <= len(source_tokens) <= 4:
        return False
    if any(
        token in RESIDUAL_TRANSLATION_ENGLISH_WORDS
        or _looks_like_inflected_english_token(token)
        for token in source_tokens
    ):
        return False
    raw_text = " ".join(
        str(
            line.original_text
            or (line.metadata or {}).get("original_text")
            or line.raw_text
            or line.text
            or ""
        )
        for line in group.lines
    )
    raw_tokens = re.findall(r"[A-Za-z]{3,}", raw_text)
    return any(token.isalpha() and not token.isupper() for token in raw_tokens)


ENGLISH_FUNCTION_TOKENS = frozenset(
    {
        # auxiliaries and modals
        "AM", "ARE", "BE", "BEEN", "BEING", "CAN", "COULD", "DID", "DO",
        "DOES", "DONE", "HAD", "HAS", "HAVE", "IS", "MAY", "MIGHT", "MUST",
        "SHALL", "SHOULD", "WAS", "WERE", "WILL", "WOULD", "WONT", "CANT",
        # pronouns and determiners
        "HE", "HER", "HERS", "HIM", "HIS", "I", "IT", "ITS", "ME", "MINE",
        "MY", "OUR", "OURS", "SHE", "THEIR", "THEIRS", "THEM", "THEY", "US",
        "WE", "YOU", "YOUR", "YOURS", "THIS", "THAT", "THESE", "THOSE",
        "THE", "AN", "SOME", "ANY", "EVERY",
        # interrogatives and frequent function words
        "WHAT", "WHEN", "WHERE", "WHICH", "WHO", "WHOM", "WHOSE", "WHY",
        "HOW", "NOT", "NOR", "AND", "OR", "BUT", "IF", "SO", "THEN", "THAN",
        "AS", "AT", "BY", "FROM", "IN", "INTO", "OF", "ON", "OUT", "OVER",
        "TO", "UP", "WITH", "WITHOUT",
        # interjections and discourse markers that open a clause
        "OK", "OKAY", "YES", "YEAH", "NO", "WELL", "HEY", "PLEASE", "SORRY",
        "THANKS", "JUST", "NOW", "HERE", "THERE", "STILL", "ALSO", "EVEN",
    }
)

NAME_TITLE_TOKENS = frozenset(
    {
        "LADY", "LORD", "SIR", "MADAM", "MADAME", "MISS", "MISTER", "MR",
        "MRS", "MS", "DR", "DOCTOR", "PROFESSOR", "CAPTAIN", "COMMANDER",
        "GENERAL", "COUNT", "COUNTESS", "DUKE", "DUCHESS", "BARON",
        "BARONESS", "PRINCE", "PRINCESS", "KING", "QUEEN", "EMPEROR",
        "EMPRESS", "MASTER", "SAINT", "FATHER", "MOTHER", "UNCLE", "AUNT",
    }
)

# Source-language determiners that mark the following title as a common noun.
SOURCE_DETERMINERS = frozenset(
    {"THE", "A", "AN", "THIS", "THAT", "THESE", "THOSE"}
)

# Endings that mark a token as an inflected English word rather than a name, so a
# clause-opening adverb followed by a comma is never read as a vocative.
NON_NAME_SUFFIXES = (
    "LY", "ING", "TION", "SION", "NESS", "MENT", "OUS", "FUL", "LESS",
    "ABLE", "IBLE", "EST",
)

MIN_PROPER_NAME_LENGTH = 3
MIN_STANDALONE_NAME_LENGTH = 4


def _name_token_of(raw):
    return re.sub(r"[^A-Z']", "", _ascii_fold(str(raw or "")).upper()).strip("'")


def _token_is_source_vocabulary(token):
    """True when the source language uses this token as a word, not as a name.

    Comic lettering is all-caps, so capitalisation carries no signal at all. The
    only reliable refusal is lexical: anything the pipeline already knows as an
    English word, an auxiliary/pronoun, an SFX, or a visibly inflected form is
    never a name, however name-shaped its position looks.
    """
    if not token:
        return True
    if (
        token in ENGLISH_FUNCTION_TOKENS
        or token in COMMON_ENGLISH_WORDS
        or token in RESIDUAL_TRANSLATION_ENGLISH_WORDS
        or token in SFX_WORDS
        or token in NAME_TITLE_TOKENS
    ):
        return True
    if token.casefold() in OCR_REPAIR_ENGLISH_WORDS:
        return True
    return len(token) >= 5 and token.endswith(NON_NAME_SUFFIXES)


def _separator_between(text, left_match, right_match):
    return text[left_match.end() : right_match.start()]


def _token_governs_following_pronoun(text, matches, index):
    """True when the token reads as a verb or auxiliary of the source clause.

    A modal opening a tag question ('..., <modal> you?') sits in exactly the
    position a vocative would, so position alone cannot separate the two. What
    separates them is that the next token is a pronoun the verb governs, with no
    punctuation in between: an address is always punctuated off from its clause,
    a verb phrase never is.
    """
    if index + 1 >= len(matches):
        return False
    following = _name_token_of(matches[index + 1].group(0))
    if following not in ENGLISH_FUNCTION_TOKENS:
        return False
    separator = _separator_between(text, matches[index], matches[index + 1])
    return not re.search(r"[,;:!?…]|\.\.\.", separator)


def _has_strong_name_context(matches, index):
    """True only in the positions that mark a name beyond doubt.

    Deliberately narrow. Neither a bare vocative (a name before a comma against a verb
    before a comma) nor a lone token (a name alone against an ordinary word alone) can
    be resolved from the sentence alone: separating them needs to know that the other
    word is a verb, and this pipeline has no English lexicon deep enough to know that.
    Guessing there freezes ordinary words in the source language, so nothing is claimed
    from position except a title binding to the name after it. Recurring names arrive
    from chapter consensus as ``known_names``, and a lone name is settled by evidence
    from the model instead.
    """
    token = _name_token_of(matches[index].group(0))
    if len(token) < MIN_PROPER_NAME_LENGTH:
        return False

    # A title binds to the name that follows it: 'LADY <NAME>'.
    return index > 0 and _name_token_of(matches[index - 1].group(0)) in NAME_TITLE_TOKENS


def _declared_name_tokens(text):
    """Names proven by the sentence itself, not by capitalization.

    Webtoon dialogue often introduces an alias explicitly ("X... but people call
    me Y") or asks about a just-heard name ("X? That's a strange name").  That
    context is stronger than suffix heuristics such as ``-LESS`` because the
    source is literally talking about a name.
    """
    cleaned = clean_ocr_text(text)
    result = []
    for pattern in (
        r"\b([A-Za-z][A-Za-z’-]{2,})\b\s*(?:[.…。]{2,}|,)?\s*BUT\s+PEOPLE\s+CALL\s+ME\s+([A-Za-z][A-Za-z’-]{2,})\b",
        r"^\s*([A-Za-z][A-Za-z’-]{2,})\s*\?\s+THAT'?S\s+A\s+STRANGE\s+NAME\b",
    ):
        for match in re.finditer(pattern, cleaned, flags=re.I):
            for group in match.groups():
                token = _name_token_of(group)
                if (
                    len(token) >= MIN_PROPER_NAME_LENGTH
                    and token not in ENGLISH_FUNCTION_TOKENS
                    and group not in result
                ):
                    result.append(group)
    return result


def detect_proper_name_spans(text, known_names=()):
    """Return the tokens of ``text`` that are confidently proper names.

    The translator must be told which spans it may keep, never merely that names
    may exist: told only 'keep proper names', the model elects its own candidate
    and then adapts it into the target language, which is how a source auxiliary
    became an invented character name. Detection is fail-closed - an ambiguous
    token is left out and simply gets translated like any other word, because a
    word translated in error is recoverable and a name invented in error is not.
    """
    cleaned = clean_ocr_text(text)
    matches = list(re.finditer(r"[A-Za-z][A-Za-z’-]*", cleaned))
    if not matches:
        return []
    declared = _declared_name_tokens(cleaned)
    known = {
        _name_token_of(part)
        for name in (known_names or ())
        for part in re.split(r"\s+", str(name or ""))
        if _name_token_of(part)
    }
    spans = []
    for raw in declared:
        if raw not in spans:
            spans.append(raw)
    for index, match in enumerate(matches):
        raw = match.group(0)
        token = _name_token_of(raw)
        if len(token) < MIN_PROPER_NAME_LENGTH:
            continue
        # The source vocabulary always wins, even over chapter consensus: a word
        # the language uses grammatically can never be preserved as a name.
        if _token_is_source_vocabulary(token):
            continue
        is_known = token in known
        if not is_known and _token_governs_following_pronoun(cleaned, matches, index):
            continue
        if is_known or _has_strong_name_context(matches, index):
            if raw not in spans:
                spans.append(raw)
    return spans


def _title_used_as_common_noun_tokens(source_infos):
    """Title tokens the source uses as a common noun, which must be translated.

    A title bound to a name ('LADY <NAME>') is a form of address and is kept
    verbatim. A title standing as a common noun - preceded by a determiner or
    carrying a possessive ("the count's ...") - is ordinary vocabulary and has to be
    translated like any other word, or the candidate stays half in the source
    language. Detected structurally from determiner and possessive, so no specific
    title or language is ever special-cased.
    """
    tokens = [info["token"] for info in source_infos]
    bases = [token.split("'")[0] for token in tokens]
    result = set()
    for index, base in enumerate(bases):
        if base not in NAME_TITLE_TOKENS:
            continue
        previous = bases[index - 1] if index > 0 else ""
        has_possessive = "'" in tokens[index]
        if previous in SOURCE_DETERMINERS or has_possessive:
            result.add(base)
    return result


def group_proper_name_spans(group):
    return detect_proper_name_spans(
        group.text,
        known_names=getattr(group, "detected_proper_names", ()) or (),
    )


def group_has_name_only_shape(group):
    """True when the group could be a lone name: one out-of-vocabulary token.

    Shape alone proves nothing - a lone ordinary word has the same shape as a lone
    name - so this only says the group is worth asking about. What settles it is
    ``group_is_untranslatable_name``, which reads the model's own answer.
    """
    if group.classification not in {"speech", "thought", "narration", "unknown"}:
        return False
    cleaned = clean_ocr_text(group.text)
    matches = list(re.finditer(r"[A-Za-z][A-Za-z’-]*", cleaned))
    if len(matches) != 1:
        return False
    token = _name_token_of(matches[0].group(0))
    if len(token) < MIN_STANDALONE_NAME_LENGTH:
        return False
    if _token_is_source_vocabulary(token):
        return False
    return not _is_nonlexical_vocalization_token({token})


def group_is_untranslatable_name(group, candidate):
    """True when the model, told to translate every word, returned the source.

    This pipeline has no English lexicon, so it cannot tell a lone name from a lone
    ordinary word by inspection. The model can. Once it has been instructed that the
    text contains no proper names and that every word must be translated, a candidate
    still identical to the source is the model reporting there is nothing to
    translate - which is what a name is. An ordinary word comes back translated and
    never reaches here.
    """
    if not group_has_name_only_shape(group):
        return False
    candidate = clean_ocr_text(candidate)
    if not candidate:
        return False
    return _normalized_translation_text(candidate) == _normalized_translation_text(
        group.text
    )


def _source_entity_tokens(text):
    return [
        _name_token_of(match)
        for match in re.findall(r"[A-Za-z][A-Za-z']*", clean_ocr_text(text))
    ]


def _ocr_unintelligible_source_text(text):
    """True when the source was never read properly, so there is nothing to translate.

    Only evidence the recogniser itself left behind is used. Comic lettering is
    uppercase, so a lone token whose case alternates internally is the recogniser
    stumbling on glyphs rather than a word or a name. A source title fused to the
    word after it is the recogniser having lost the space, which is why the fused
    token is neither dialogue nor an entity. Both belong in review, but neither is
    dialogue the translator failed to translate, and reporting them as such buries
    the read defect under a translation metric.
    """
    tokens = _source_entity_tokens(text)
    if len(tokens) != 1 or len(tokens[0]) < MIN_STANDALONE_NAME_LENGTH:
        return False
    raw = re.findall(r"[A-Za-z][A-Za-z']*", clean_ocr_text(text))[0]
    if not (raw.isupper() or raw.islower() or raw.istitle()):
        return True
    if _looks_like_title_name_ocr_compound(tokens[0]):
        return True
    if raw.isupper() and len(tokens[0]) >= 5 and (
        not re.search(r"[AEIOUY]", tokens[0])
        or re.search(r"[BCDFGHJKLMNPQRSTVWXYZ]{5,}", tokens[0])
    ):
        return True
    return False


def _chapter_preserved_entity_tokens(groups):
    """Tokens the chapter itself proves are entities rather than untranslated words.

    A token the translator kept verbatim inside a region it otherwise turned into
    valid target-language text is the translator reporting that this token has no
    translation - the same evidence the session ledger records as a name preserved in
    translation. Casing, length and spelling are never consulted: a token with no
    such region behind it has no evidence at all and stays unproven, which is how an
    ordinary source word that merely looks unusual keeps failing closed.
    """
    tokens = set()
    for group in groups:
        candidate = clean_ocr_text(getattr(group, "translation", "") or "")
        if not candidate or _translation_echoes_source(group):
            continue
        valid, _ = validate_translation_text(
            group.text,
            candidate,
            group.classification,
            _group_validation_allowed_proper_names(group),
            required_name_spans=group_proper_name_spans(group),
        )
        if not valid:
            continue
        source_tokens = set(_source_entity_tokens(group.text))
        for token in _source_entity_tokens(candidate):
            if token in source_tokens and not _token_is_source_vocabulary(token):
                tokens.add(token)
    return tokens


def _group_is_proven_entity_echo(group, candidate, entity_tokens):
    """True when every word kept from the source is a token the chapter proved.

    Punctuation and formatting carry no language, so they are ignored, but a single
    unproven word is enough to hold the group: a name beside ordinary source text is
    still ordinary source text left untranslated.
    """
    if not entity_tokens:
        return False
    candidate = clean_ocr_text(candidate)
    if _normalized_translation_text(candidate) != _normalized_translation_text(
        group.text
    ):
        return False
    if _ocr_unintelligible_source_text(group.text):
        return False
    tokens = _source_entity_tokens(group.text)
    return bool(tokens) and all(token in entity_tokens for token in tokens)


def _group_validation_allowed_proper_names(group):
    names = list(group.detected_proper_names or [])
    names.extend(group_proper_name_spans(group))
    if _group_has_mixed_case_name_evidence(group):
        names.append(group.text)
    return names


def _repair_translation_candidate_before_validation(
    source_text,
    candidate,
    classification="speech",
    *,
    required_name_spans=None,
):
    """Deterministic, narrow repairs before the safety validator runs.

    This is not a translation authority.  It only fixes candidate shapes whose intent is
    already explicit and locally provable: declared names must remain verbatim, one OCR
    debris parenthesis may be removed, and the common ``o que você fazer`` bad mood is
    converted to subjunctive when the source says ``what you do``.
    """

    text = clean_ocr_text(candidate)
    if not text:
        return ""
    text = _repair_declared_name_tokens(source_text, text, required_name_spans or ())
    text = _repair_stray_ocr_fragment_candidate(source_text, text)
    text = _repair_narrow_ptbr_verb_mood(source_text, text, classification)
    return clean_ocr_text(text)


def _repair_declared_name_tokens(source_text, candidate, required_name_spans):
    repaired = str(candidate or "")
    source = clean_ocr_text(source_text)
    for raw_name in required_name_spans or ():
        name = clean_ocr_text(raw_name)
        replacement_name = _canonical_declared_name_surface(name)
        token = _name_token_of(name)
        if not name or token in {_name_token_of(info["raw"]) for info in _translation_token_infos(repaired)}:
            continue
        if re.search(rf"^\s*{re.escape(name)}\b", source, flags=re.I):
            repaired = re.sub(
                r"^\s*[A-Za-zÀ-ÖØ-öø-ÿ]+(?:\s+[A-Za-zÀ-ÖØ-öø-ÿ]+){0,2}",
                replacement_name,
                repaired,
                count=1,
            )
            continue
        if re.search(rf"\b(?:MY\s+NAME\s+IS|I\s+AM|I'M)\s+{re.escape(name)}\b", source, flags=re.I):
            repaired = re.sub(
                r"(\b(?:MEU\s+NOME\s+(?:É|E)|EU\s+(?:SOU|ME\s+CHAMO))\s+)"
                r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:\s+[A-Za-zÀ-ÖØ-öø-ÿ]+){0,2}",
                lambda match, replacement=replacement_name: match.group(1) + replacement,
                repaired,
                count=1,
                flags=re.I,
            )
    return repaired


def _canonical_declared_name_surface(name):
    """Normalize OCR casing only for spans already proven by declaration syntax."""

    text = clean_ocr_text(name)
    if not text:
        return ""
    if text.isupper() or text.islower() or text.istitle():
        return text
    parts = re.split(r"([’'\-])", text)
    repaired = []
    for part in parts:
        if re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", part):
            repaired.append(part[:1].upper() + part[1:].lower())
        else:
            repaired.append(part)
    return "".join(repaired)


def _repair_stray_ocr_fragment_candidate(source_text, candidate):
    folded_source = _ascii_fold(source_text).upper()

    def replace(match):
        folded_match = _ascii_fold(match.group(0)).upper()
        return match.group(0) if folded_match in folded_source else match.group(1)

    return re.sub(r"\b([A-Z])\)", replace, str(candidate or ""), flags=re.I)


def _repair_narrow_ptbr_verb_mood(source_text, candidate, classification):
    if classification not in {"speech", "thought", "narration", "unknown"}:
        return candidate
    folded_source = _ascii_fold(source_text).upper()
    if not re.search(r"\bWHAT\s+YOU\s+DO\b", folded_source):
        return candidate
    return re.sub(
        r"\b(VO[CÇ]Ê|VOCE)\s+FAZER\b",
        lambda match: f"{match.group(1)} FIZER",
        str(candidate or ""),
        flags=re.I,
    )


def apply_group_translations(groups, translations):
    translations = list(translations or [])
    for index, group in enumerate(groups):
        group.sent_to_translation = True
        raw_translation = translations[index] if index < len(translations) else ""
        evidence = dict(getattr(raw_translation, "quality_evidence", {}) or {})
        if isinstance(raw_translation, dict):
            nested = raw_translation.get("quality_evidence")
            if isinstance(nested, dict):
                evidence.update(nested)
        if evidence:
            group.quality_evidence = {**group.quality_evidence, **evidence}
        translated = clean_ocr_text(
            raw_translation.get("translation", "") if isinstance(raw_translation, dict)
            else raw_translation
        )
        group.raw_provider_candidate = translated
        group.raw_provider_candidate_class = _candidate_forensic_class(
            group.text,
            translated,
            group.classification,
        )
        if not translated:
            group.translation_candidate = ""
            _finalize_translation_failure(
                group,
                "missing_translation_candidate",
                candidate="",
            )
            continue

        translated = _repair_translation_candidate_before_validation(
            group.text,
            translated,
            group.classification,
            required_name_spans=group_proper_name_spans(group),
        )
        group.translation_candidate = _match_source_case(group.text, translated)
        group.translation = group.translation_candidate
        valid, reason = validate_translation_text(
            group.text,
            group.translation,
            group.classification,
            _group_validation_allowed_proper_names(group),
            required_name_spans=group_proper_name_spans(group),
        )
        group.translation_valid = valid
        group.translation_validation_reason = reason
        if valid:
            _set_translation_terminal_state(group, "translated", reason)
        else:
            _set_translation_terminal_state(group, "rejected", reason)


def _set_translation_terminal_state(
    group,
    state,
    reason="",
    *,
    preserved_original=None,
):
    if state not in TRANSLATION_TERMINAL_STATES:
        raise ValueError(f"estado terminal de traducao invalido: {state}")
    new_reason = str(reason or group.translation_validation_reason or "")
    current_reason = getattr(group, "translation_final_reason", "") or ""
    # A proven, specific reason outranks a later generic one when the outcome stays
    # neutral: the renderer preserves original pixels for both a proper name and a
    # stylized echo, but only the name is quality-neutral, so the name reason must
    # survive the render's bookkeeping. A genuine review outcome still wins.
    if (
        state not in REVIEW_TERMINAL_STATES
        and current_reason in STICKY_TERMINAL_REASONS
        and new_reason not in STICKY_TERMINAL_REASONS
    ):
        new_reason = current_reason
    group.translation_final_state = state
    group.translation_final_reason = new_reason
    if preserved_original is not None:
        group.preserved_original = bool(preserved_original)
    group.translation_quality_impact = (
        "review_required" if state in REVIEW_TERMINAL_STATES else "none"
    )


def _normalized_translation_text(text):
    folded = _ascii_fold(clean_ocr_text(text)).casefold()
    return re.sub(r"[^a-z0-9]+", "", folded)


def _translation_echoes_source(group):
    """True when rendering the group would only echo its source text.

    Some groups are validated as translatable while keeping the source verbatim
    (stylized vocalizations, branding/logo tokens preserved as names). Redrawing
    such a group erases the original art and repaints identical (often
    OCR-corrupted) glyphs for no benefit, which is how a stylized title becomes a
    garbled overlay. These are preserved as original pixels instead. The
    comparison is accent-folded so a real translation such as 'No' -> 'Nao/Não'
    is never treated as an echo.
    """
    translation = getattr(group, "translation", "") or ""
    if not translation:
        return False
    source = getattr(group, "text", "") or ""
    normalized_source = _normalized_translation_text(source)
    if not normalized_source:
        return False
    return _normalized_translation_text(translation) == normalized_source


def _candidate_forensic_class(source, candidate, classification=""):
    candidate = clean_ocr_text(candidate)
    if not candidate:
        return "EMPTY"
    if _normalized_translation_text(candidate) == _normalized_translation_text(source):
        return "SOURCE_EQUAL"
    if str(classification or "").casefold() == "sfx":
        return "ENTITY/SFX/PRESERVED"
    valid, reason = validate_translation_text(
        source,
        candidate,
        classification,
        [],
        required_name_spans=[],
    )
    if valid:
        return "PTBR_CLEAN"
    reason = str(reason or "")
    if reason.startswith(("mixed_language", "multilingual_partial")):
        return "PARTIAL_ENGLISH"
    if reason.startswith(
        (
            "residual_english",
            "residual_inflected_english",
            "untranslated_english",
            "untranslated_single_english",
            "english_phrase",
        )
    ):
        return "FULL_ENGLISH_OTHER"
    return "OTHER"


def _terminal_translation_failure_reason(group, validation_reason, candidate):
    candidate_text = clean_ocr_text(candidate)
    if not candidate_text:
        return "missing_translation_candidate"
    if _normalized_translation_text(candidate_text) == _normalized_translation_text(
        group.text
    ):
        if _ocr_unintelligible_source_text(group.text):
            return OCR_UNINTELLIGIBLE_SOURCE_REASON
        return "untranslated_source_after_retries"
    if str(validation_reason or "").startswith(
        (
            "mixed_language",
            "english_phrase",
            "residual_english",
            "residual_inflected_english",
            "residual_source_language",
            "residual_spanish",
            "multilingual_partial",
            "untranslated_english",
            "untranslated_single_english",
        )
    ):
        return "residual_source_language_after_retries"
    if semantic_fidelity.is_fidelity_reason(validation_reason):
        return "semantic_fidelity_failed_after_retries"
    if str(validation_reason or "").startswith("strict_retry_error"):
        return "translation_failed_after_retries"
    return "invalid_translation_after_retries"


def _finalize_proper_name_only(group, candidate, *, proven=False):
    """Close a name-only group as correctly preserved instead of untranslated.

    A balloon holding just a character's name has no sentence to translate: keeping
    the name verbatim is the right output, not a missing translation. Routed through
    the failure path it produced a candidate equal to the source, a retry storm and a
    manual review, so a whole chapter was held back by a name that was already right.

    ``proven`` means the caller already established every kept word is an entity from
    chapter evidence, which is a stronger answer than the lone-token shape this asks
    for; without it the shape still has to hold.
    """
    if not proven and not group_is_untranslatable_name(group, candidate):
        return False
    group.translation = group.text
    group.translation_candidate = group.text
    group.translation_valid = True
    group.translation_validation_reason = PROPER_NAME_ONLY_REASON
    group.rejected_translation = ""
    group.manual_review_required = False
    _set_translation_terminal_state(
        group,
        "preserved_original",
        PROPER_NAME_ONLY_REASON,
        preserved_original=True,
    )
    return True


def _finalize_translation_failure(
    group,
    reason,
    *,
    candidate="",
    rejected_candidate=None,
    validator_reason="",
):
    candidate = clean_ocr_text(candidate)
    rejected_candidate = clean_ocr_text(
        candidate if rejected_candidate is None else rejected_candidate
    )
    if candidate:
        group.translation_candidate = _match_source_case(group.text, candidate)
    if rejected_candidate:
        group.rejected_translation = _match_source_case(
            group.text,
            rejected_candidate,
        )
    group.translation = ""
    group.translation_valid = False
    group.translation_validation_reason = str(
        validator_reason or group.translation_validation_reason or reason
    )
    group.translation_unresolved = True
    group.trusted_translation_missing = True
    group.source_fallback_prevented = True
    group.translation_attempts_exhausted = bool(
        str(reason).endswith("_after_retries")
        or str(reason)
        in {
            "untranslated_source_after_retries",
            "missing_translation_candidate",
            "translation_not_rendered_after_validation",
        }
    )
    group.manual_review_required = True
    state = (
        "translation_failed"
        if str(reason).startswith("translation_failed")
        else "manual_review"
    )
    _set_translation_terminal_state(
        group,
        state,
        reason,
        preserved_original=True,
    )


def _ensure_translation_terminal_state(group):
    if group.translation_final_state in TRANSLATION_TERMINAL_STATES:
        return
    if group.ignored or group.preserve_as_name:
        if group.manual_review_required:
            _set_translation_terminal_state(
                group,
                "manual_review",
                group.ignore_reason or "translation_review_required",
                preserved_original=True,
            )
            return
        _set_translation_terminal_state(
            group,
            "skipped_with_reason",
            group.ignore_reason or "translation_not_required",
            preserved_original=True,
        )
        return
    if not group.sent_to_translation:
        _set_translation_terminal_state(
            group,
            "skipped_with_reason",
            "translation_not_selected",
            preserved_original=True,
        )
        return
    if not group.translation_valid:
        _finalize_translation_failure(
            group,
            _terminal_translation_failure_reason(
                group,
                group.translation_validation_reason,
                group.translation_candidate or group.translation,
            ),
            candidate=group.translation_candidate,
            validator_reason=group.translation_validation_reason,
        )
        return
    if group.visual_attempts and not group.redrawn:
        _finalize_translation_failure(
            group,
            "translation_not_rendered_after_validation",
            candidate=group.translation_candidate or group.translation,
        )
        return
    _set_translation_terminal_state(
        group,
        "translated",
        group.translation_validation_reason or "ok",
        preserved_original=False,
    )


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
    with ocr_line_provenance.page(page_index):
        return _render_analyzed_image(
            original_bgr,
            raw_lines,
            candidates,
            groups,
            font_path=font_path,
            debug_folder=debug_folder,
            page_index=page_index,
            image_path=image_path,
            stage_timings=stage_timings,
        )


def _render_analyzed_image(
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
        # Boundary immediately before inpainting/redraw consume the group: this is
        # the exact text/geometry the renderer sees, not a later reconstruction.
        render_lines = _cleanup_lines_for_group(group)
        ocr_line_provenance.record_render_input(
            group,
            lines=render_lines,
            reason="render_analyzed_image",
        )
        # Pre-render guard: source content the group owns must still be present in
        # what the renderer was handed.  Detection only - the text is never
        # rewritten and the provider is never called again, because the provider
        # never received the lost token in the first place.
        group.source_completeness = source_completeness.check_live_group(
            group,
            render_lines=render_lines,
        )
        if group.source_completeness.get("status") in {
            source_completeness.STATUS_FAIL,
            source_completeness.STATUS_REVIEW,
        }:
            group.manual_review_required = True
        before_group = final.copy()
        group.visual_attempts = []
        group.manual_review_required = False
        accepted = False
        accepted_cleanup_mask = None
        accepted_strategy = ""
        accepted_allowed_mask = None

        if _translation_echoes_source(group):
            group.redrawn = False
            group.visual_validation = {
                "visual_validation_passed": True,
                "reason": "source_echo_preserved_original",
                "render_preserved_source_echo": True,
            }
            # Preserve the original pixels either way, but let a proven reason such as
            # ``proper_name_only`` survive: the precedence policy in
            # ``_set_translation_terminal_state`` keeps the specific reason and only
            # takes this generic one when none was proven earlier.
            _set_translation_terminal_state(
                group,
                "preserved_original",
                "source_echo_preserved_original",
                preserved_original=True,
            )
            continue

        for strategy in (
            "primary",
            "conservative",
            "glyph_overlay",
            "caption_overlay",
            # Last resort: only proven ordinary speech reaches it, and only after
            # every broad strategy above already refused the region.
            "source_scoped",
        ):
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
            rendered = _draw_group_translation(
                cleaned,
                group,
                font_path,
                strategy=strategy,
            )
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
            if visual_summary.get("visual_validation_passed"):
                # Needs no OCR: the cleanup either covered the source text this
                # group owns or it did not.  A mask that demonstrably left
                # source glyphs behind can never be accepted, whatever the
                # post-render OCR stage is configured to do.
                removal = _uncovered_source_text_evidence(
                    original,
                    rendered,
                    group,
                    cleanup_mask,
                )
                visual_summary["source_text_removal"] = removal
                if config.POST_RENDER_OCR_VALIDATION:
                    residual_summary = _post_render_source_text_check(
                        rendered,
                        group,
                        page_index,
                        original_bgr=original,
                        cleanup_mask=cleanup_mask,
                        removal=removal,
                    )
                    visual_summary["post_render_ocr"] = residual_summary
                    if not residual_summary.get("passed", True):
                        visual_summary["visual_validation_passed"] = False
                        visual_summary["reason"] = str(
                            residual_summary.get("reason")
                            or "post_render_ocr_validation_failed"
                        )
                elif _source_removal_incomplete(group, removal):
                    visual_summary["visual_validation_passed"] = False
                    visual_summary["reason"] = "uncovered_source_text_evidence"
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
                _set_translation_terminal_state(
                    group,
                    "translated",
                    group.translation_validation_reason or "ok",
                    preserved_original=False,
                )
                break

        (
            group.art_reconstruction_status,
            group.art_reconstruction_reason,
        ) = art_reconstruction_verdict(group.visual_attempts, accepted=accepted)
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
            _finalize_translation_failure(
                group,
                "translation_not_rendered_after_validation",
                candidate=group.translation_candidate or group.translation,
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
                    group.visual_validation = {
                        **group.visual_validation,
                        "visual_validation_passed": False,
                        "reason": "page_level_visual_guard_rollback",
                    }
                    _finalize_translation_failure(
                        group,
                        "translation_not_rendered_after_validation",
                        candidate=group.translation_candidate or group.translation,
                    )
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


def get_font(font_path, size, role="regular", *, prefer_role=False, text=""):
    font, runtime = font_fidelity.resolve_font(
        role,
        size,
        configured_font_path=font_path,
        prefer_role=prefer_role,
        text=text,
    )
    try:
        setattr(font, "tradutor_font_runtime", runtime)
    except Exception:
        pass
    return font


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
        # ``line.text`` is about to be rewritten on the shared object; record the
        # historical value before it is gone.
        ocr_line_provenance.record_normalization(line, line.text, repaired, reason=reason)
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

    # Censorship masks (a letter followed by * or #, e.g. "D***", "H#**") are
    # legitimate placeholders inside real speech, not noise. Neutralize them so
    # they do not inflate the odd-character ratio and drop translatable dialogue.
    sanitized = re.sub(
        r"(?<=[A-Za-z])[*#]+",
        lambda match: "." * len(match.group()),
        text,
    )
    odd = sum(
        1
        for char in sanitized
        if not (char.isalnum() or char in "'!?.,:; -")
    )
    if odd / max(1, len(sanitized)) > 0.28:
        return True

    letters = _letters(text)
    normalized = unicodedata.normalize("NFKD", text)
    vowels = sum(char in "AEIOUaeiou" for char in normalized)
    if len(letters) >= 5 and vowels == 0:
        return True

    return False


def reading_order(lines):
    """Lines of a text region in reading order: rows top to bottom, then left to right.

    A bbox ``y`` is not by itself a reading order.  Two fragments of the same
    printed row - a predecessor line that a region re-read came back with split
    in two, say - differ vertically by a few pixels, and a lexicographic
    ``(y, x)`` sort turns that jitter into a sentence carrying its suffix in
    front of its prefix.  Rows are therefore resolved by vertical overlap
    first, and only whole rows are ordered by ``y``; inside a row the page's
    horizontal direction decides.

    Geometry stays authoritative - a recogniser that emits its lines out of
    order is corrected, not obeyed - and the sort is stable, so lines the
    geometry cannot separate keep the order they arrived in.
    """

    rows = []
    for line in sorted(lines or [], key=lambda item: (item.box[1], item.box[0])):
        top = line.box[1]
        bottom = top + max(1, line.box[3])
        for row in rows:
            overlap = min(row[1], bottom) - max(row[0], top)
            if overlap >= 0.5 * min(bottom - top, row[1] - row[0]):
                row[0] = min(row[0], top)
                row[1] = max(row[1], bottom)
                row[2].append(line)
                break
        else:
            # ponytail: rows grow greedily, so a run of lines each overlapping
            # the previous by half could chain into one row.  Printed line
            # pitch is well clear of that; revisit if a real page chains.
            rows.append([top, bottom, [line]])
    return [
        line for row in rows for line in sorted(row[2], key=lambda item: item.box[0])
    ]


def _group_lines(lines, page_index=None):
    groups = []

    for line in sorted(lines, key=lambda item: (item.box[1], item.box[0])):
        target = None
        for group in groups:
            record_count("group_lines.pair_checks", 1, page_index=page_index)
            with profile_step("group_lines.line_belongs_to_group", page_index=page_index):
                belongs = _line_belongs_to_group(line, group)
            if belongs:
                target = group
                break

        if target is None:
            target = TextGroup(group_id=f"BALAO_{len(groups) + 1}")
            groups.append(target)

        target.lines.append(line)
        target.lines = reading_order(target.lines)
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
    line_region_enclosed = bool(
        (line.metadata or {}).get("visual_white_region_enclosed")
    )
    group_region_ids = {
        int((item.metadata or {}).get("visual_white_region_id") or 0)
        for item in group.lines
    }
    group_region_ids.discard(0)
    # Only a confirmed *enclosed* container of a different id blocks the merge.
    # Weak, non-enclosed white regions over-fragment a single balloon into one
    # region per line, which wrongly splits stacked, aligned speech lines; those
    # must fall through to the geometric checks below.
    if (
        line_region_id
        and group_region_ids
        and line_region_id not in group_region_ids
        and line_region_enclosed
    ):
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

    # Same reading row: a fragment sitting beside the group's text on the same
    # baseline is the tail of that line (an exclamation split off the end), not a
    # separate block. The row must genuinely coincide, the text height must be
    # comparable and the gap only word-sized, so a larger sound effect or a
    # neighbouring balloon is not pulled in. A different enclosed container is
    # already vetoed above.
    vertical_overlap = max(0, min(gy + gh, ly + lh) - max(gy, ly))
    horizontal_gap = max(gx, lx) - min(gx + gw, lx + lw)
    height_ratio = min(lh, base_height) / max(1.0, max(lh, base_height))
    same_container = bool(
        line_region_id and group_region_ids and line_region_id in group_region_ids
    )
    if (
        same_container
        and vertical_overlap >= 0.6 * min(gh, lh)
        and height_ratio >= 0.6
        and horizontal_gap <= 0.9 * base_height
    ):
        return True

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


def _visual_white_container_evidence(group):
    """Recover compact closed containers missed by contour-based enclosure."""
    records = []
    for line in group.lines:
        metadata = line.metadata or {}
        region_id = int(metadata.get("visual_white_region_id") or 0)
        if not region_id:
            continue
        records.append(
            (
                region_id,
                float(metadata.get("visual_white_region_coverage") or 0.0),
                bool(metadata.get("visual_white_region_enclosed")),
            )
        )
    if not records or len({region_id for region_id, _, _ in records}) != 1:
        return {}
    supported = [
        (region_id, coverage)
        for region_id, coverage, enclosed in records
        if coverage >= 0.5 and enclosed
    ]
    if not supported:
        return {}
    region_id, coverage = max(supported, key=lambda item: item[1])
    return {
        "reason": "enclosed_visual_region",
        "confidence": round(coverage, 4),
        "visual_white_region_id": region_id,
        "visual_white_region_coverage": round(coverage, 4),
    }


def _editorial_graphic_evidence(groups, image_bgr):
    """Find compact title/logo layouts using position, geometry, and styling."""
    if image_bgr is None or image_bgr.size == 0:
        return {}
    image_height, image_width = image_bgr.shape[:2]
    image_area = max(1, image_width * image_height)

    def candidate(group):
        if group.ignored or len(group.lines) > 2:
            return False
        folded = _ascii_fold(group.text).upper()
        tokens = re.findall(r"[A-Z0-9]+", folded)
        compact = "".join(tokens)
        if not 1 <= len(tokens) <= 3 or not 1 <= len(compact) <= 36:
            return False
        if re.search(r"[!?]", group.text):
            return False
        if any(
            int((line.metadata or {}).get("visual_white_region_id") or 0)
            for line in group.lines
        ):
            return False
        x, y, width, height = group.box
        center_y = y + height / 2
        in_editorial_band = (
            center_y <= image_height * 0.22
            or center_y >= image_height * 0.78
        )
        return bool(
            in_editorial_band
            and (width * height) / image_area <= 0.08
        )

    def styled_region(box):
        x, y, width, height = box
        roi = image_bgr[
            max(0, y) : min(image_height, y + height),
            max(0, x) : min(image_width, x + width),
        ]
        if roi.size == 0:
            return {}
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        saturation = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)[:, :, 1]
        edge_density = float(np.mean(cv2.Canny(gray, 55, 155) > 0))
        mean_saturation = float(np.mean(saturation))
        grayscale_std = float(np.std(gray))
        if not (
            mean_saturation >= 45.0
            or (
                grayscale_std >= 22.0
                and edge_density >= 0.08
            )
        ):
            return {}
        style_score = max(
            min(1.0, mean_saturation / 255.0),
            min(1.0, grayscale_std / 64.0)
            * min(1.0, edge_density / 0.2),
        )
        return {
            "mean_saturation": round(mean_saturation, 3),
            "grayscale_std": round(grayscale_std, 3),
            "edge_density": round(edge_density, 4),
            "style_score": round(style_score, 4),
        }

    candidates = [group for group in groups if candidate(group)]
    evidence_by_group = {}

    def record(group, layout, span_ratio, style):
        confidence = round(
            min(
                1.0,
                float(style["style_score"]) * 0.5
                + min(1.0, span_ratio / 0.6) * 0.5,
            ),
            4,
        )
        evidence = {
            "reason": "editorial_graphic_layout",
            "confidence": confidence,
            "layout": layout,
            "span_ratio": round(span_ratio, 4),
            **style,
        }
        current = evidence_by_group.get(group.group_id, {})
        if confidence >= float(current.get("confidence") or 0.0):
            evidence_by_group[group.group_id] = evidence

    for group in candidates:
        folded_tokens = re.findall(r"[A-Z0-9]+", _ascii_fold(group.text).upper())
        x, y, width, height = group.box
        style = styled_region((x, y, width, height))
        if (
            len(folded_tokens) >= 2
            and width >= image_width * 0.36
            and style
        ):
            record(group, "single_wide_group", width / image_width, style)

    for index, first in enumerate(candidates):
        for second in candidates[index + 1 :]:
            first_x, first_y, first_width, first_height = first.box
            second_x, second_y, second_width, second_height = second.box
            first_center_y = first_y + first_height / 2
            second_center_y = second_y + second_height / 2
            if abs(first_center_y - second_center_y) > max(
                first_height,
                second_height,
            ) * 0.7:
                continue
            left, right = sorted(
                (first, second),
                key=lambda group: group.box[0],
            )
            gap = right.box[0] - (left.box[0] + left.box[2])
            if gap > max(48, max(first_height, second_height) * 1.5):
                continue
            x1 = min(first_x, second_x)
            y1 = min(first_y, second_y)
            x2 = max(first_x + first_width, second_x + second_width)
            y2 = max(first_y + first_height, second_y + second_height)
            union_box = (x1, y1, x2 - x1, y2 - y1)
            style = styled_region(union_box)
            if union_box[2] < image_width * 0.35 or not style:
                continue
            span_ratio = union_box[2] / image_width
            record(first, "aligned_group_cluster", span_ratio, style)
            record(second, "aligned_group_cluster", span_ratio, style)
    return evidence_by_group


def _set_group_classification(
    group,
    classification,
    reason,
    *,
    confidence=0.0,
    evidence=None,
):
    group.classification = classification
    group.classification_reason = reason
    group.classification_confidence = round(float(confidence or 0.0), 4)
    group.classification_evidence = dict(evidence or {})


def _classify_groups(groups, image_bgr, page_index=None):
    h_img, w_img = image_bgr.shape[:2]
    editorial_evidence_by_group = _editorial_graphic_evidence(groups, image_bgr)

    for group in groups:
        group_started = time.perf_counter()
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

        with profile_step("classify.group_geometry", page_index=page_index):
            group.angle_degrees = _group_angle_degrees(group)
            group.alignment_score = _group_alignment_score(group)
        group.near_image_edge = (
            x <= w_img * 0.035
            or y <= h_img * 0.025
            or x + w >= w_img * 0.965
            or y + h >= h_img * 0.975
        )

        with profile_step("classify.enclosure_evidence", page_index=page_index):
            balloon_like, narration_like = _enclosure_evidence(
                image_bgr,
                group.box,
                page_index=page_index,
            )
        contour_balloon_like = bool(balloon_like)
        visual_white_evidence = _visual_white_container_evidence(group)
        balloon_like = bool(balloon_like or visual_white_evidence)
        group.inside_balloon_like_region = balloon_like
        group.inside_narration_box_like_region = narration_like
        with profile_step("classify.external_narration_evidence", page_index=page_index):
            external_narration = _external_narration_evidence(
                group,
                image_bgr,
                words,
                reading_phrase,
                page_index=page_index,
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
        elongated_vocal_sfx = bool(
            one_short_word
            and 3 <= len(normalized) <= 14
            and re.search(r"(.)\1{2,}", normalized)
            and len(set(normalized)) <= max(4, int(len(normalized) * 0.6))
            and re.search(r"[!?]", group.text)
            and normalized not in COMMON_ENGLISH_WORDS
        )
        compact_consonant_sfx = bool(
            one_short_word
            and 2 <= len(normalized) <= 4
            and not re.search(r"[AEIOUY]", normalized)
            and normalized not in COMMON_ENGLISH_WORDS
        )
        production_credit = bool(
            re.match(
                r"^(?:ART|STORY|SCRIPT|WRITTEN|CREATED|PRODUCED|ILLUSTRATED|ILLUSTRATION|COLORS?|LETTERING)\s*BY\b",
                _ascii_fold(group.text).upper().strip(),
            )
        )
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
        repeated_short_shape = (
            len(words) in {2, 3}
            and len(set(words)) == 1
            and len(words[0]) <= 8
        )
        weak_double_repeat_shape = bool(
            one_short_word
            and re.search(r"(.)\1{1,}", normalized)
            and not elongated_vocal_sfx
            and re.search(r"[!?]", group.text)
            and normalized not in COMMON_ENGLISH_WORDS
        )
        editorial_evidence = editorial_evidence_by_group.get(group.group_id)

        if production_credit or editorial_evidence:
            group.inside_balloon_like_region = False
            group.inside_narration_box_like_region = False
            group.classification = "decorative"
        elif oversized_short_graphic or elongated_vocal_sfx or compact_consonant_sfx:
            group.inside_balloon_like_region = False
            group.inside_narration_box_like_region = False
            group.classification = "sfx"
        elif (
            is_known_sfx
            and len(words) <= 2
            and not (balloon_like or narration_like)
        ):
            group.inside_balloon_like_region = False
            group.inside_narration_box_like_region = False
            group.classification = "sfx"
        elif repeated_short_outside_region:
            group.inside_balloon_like_region = False
            group.inside_narration_box_like_region = False
            group.classification = "sfx"
        elif (
            is_known_sfx
            and strongly_styled
            and not (balloon_like or narration_like)
        ):
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

        if editorial_evidence and group.classification == "decorative":
            _set_group_classification(
                group,
                "decorative",
                "editorial_graphic_layout",
                confidence=editorial_evidence["confidence"],
                evidence=editorial_evidence,
            )
        elif (
            group.classification == "speech"
            and visual_white_evidence
            and not contour_balloon_like
        ):
            sfx_conflict = bool(
                is_known_sfx
                or oversized_short_graphic
                or elongated_vocal_sfx
                or compact_consonant_sfx
                or repeated_short_shape
            )
            _set_group_classification(
                group,
                "speech",
                (
                    "enclosed_visual_region_over_sfx_shape"
                    if sfx_conflict
                    else "enclosed_visual_region_over_weak_text"
                ),
                confidence=visual_white_evidence["confidence"],
                evidence={
                    **visual_white_evidence,
                    "conflict_resolved": "sfx" if sfx_conflict else "unknown_weak",
                },
            )
        elif group.classification == "speech" and weak_double_repeat_shape:
            _set_group_classification(
                group,
                "speech",
                "container_over_weak_double_character_repeat",
                confidence=(
                    visual_white_evidence.get("confidence", group.main_text_score)
                ),
                evidence={
                    **visual_white_evidence,
                    "conflict_resolved": "weak_double_character_repeat",
                },
            )
        elif group.classification == "narration" and repeated_short_shape:
            # A "narration" whose entire content is one short token repeated
            # (e.g. footsteps "STEP STEP") is a sound effect lettered on the art
            # that a stylized bright region (or floor edges read as a box) was
            # mistaken for a caption box. Real narration is prose, never a lone
            # repeated word; a genuine repeated exclamation in a real balloon is
            # classified speech, not narration, so it is unaffected. Keep it a
            # sound effect instead of translating and redrawing over the art.
            group.inside_balloon_like_region = False
            group.inside_narration_box_like_region = False
            _set_group_classification(
                group,
                "sfx",
                "repeated_onomatopoeia_over_false_enclosure",
                confidence=group.main_text_score,
                evidence={"conflict_resolved": "repeated_onomatopoeia"},
            )

        with profile_step("classify.apply_policy", page_index=page_index):
            _apply_classification_policy(group)
        with profile_step(
            "classify.background_region",
            page_index=page_index,
            metadata={"group_id": group.group_id, "classification": group.classification},
        ):
            group.background_type, group.background_metrics = _classify_background_region(
                image_bgr,
                group,
                page_index=page_index,
            )
        with profile_step("classify.refine_background", page_index=page_index):
            _refine_classification_with_background(group)
        record_group(
            page_index=page_index,
            group_id=group.group_id,
            elapsed_seconds=time.perf_counter() - group_started,
            line_count=len(group.lines),
            classification=group.classification,
            fallback_used=bool(group.fallback_used),
            dominant_step="classification",
        )


def _lettering_is_saturated_effect_art(metrics):
    """True when the glyph region is saturated colour art, not lettering in a balloon.

    Dialogue is lettered in ink on a fill: whatever the balloon's colour, the glyph
    region always carries light pixels and almost no saturation. Effect lettering is
    drawn as coloured art - the whole region is one intense hue with no light fill at
    all. That difference is what separates a sound effect drawn over a dark panel from
    a line of speech inside a dark balloon, which the container evidence alone cannot.
    """
    return (
        float(metrics.get("saturation_mean") or 0.0) >= 90.0
        and float(metrics.get("white_pixel_ratio") or 0.0) <= 0.02
    )


def _refine_classification_with_background(group):
    metrics = group.background_metrics or {}
    folded = _ascii_fold(group.text).upper()
    words = re.findall(r"[A-Z0-9']+", folded)
    compact = re.sub(r"[^A-Z0-9]", "", folded)
    group_area_ratio = (group.box[2] * group.box[3]) / max(
        1,
        int(metrics.get("image_width") or group.box[2])
        * int(metrics.get("image_height") or group.box[3]),
    )
    if (
        group.classification == "speech"
        and group.classification_reason == "container_over_weak_double_character_repeat"
        and _lettering_is_saturated_effect_art(metrics)
        and not group_has_name_only_shape(group)
    ):
        # A short doubled-letter token ("KREE!", "HISS!") is only weak evidence of a
        # sound effect on its own - ordinary words share the shape ("WELL!", "FREE!") -
        # so container evidence rightly kept it speech. But when the lettering itself is
        # saturated colour art rather than glyphs on a balloon fill, the shape is no
        # longer the only signal, and the two together outweigh the enclosure that a
        # glowing effect drawn on a dark panel produces. Settling it here, before
        # candidate selection, is what keeps an untranslatable effect out of the
        # translation request instead of catching it as an echo after the fact.
        group.inside_balloon_like_region = False
        group.inside_narration_box_like_region = False
        group.parent_balloon_id = ""
        group.region_type = "sfx"
        group.background_type = "sfx_area"
        group.background_metrics = {
            **metrics,
            "background_type": "sfx_area",
            "reason": "saturated_effect_lettering",
        }
        _set_group_classification(
            group,
            "sfx",
            "saturated_effect_lettering_over_container",
            confidence=group.classification_confidence,
            evidence={
                **(group.classification_evidence or {}),
                "conflict_resolved": "saturated_effect_lettering",
                "saturation_mean": metrics.get("saturation_mean"),
                "white_pixel_ratio": metrics.get("white_pixel_ratio"),
            },
        )
        _apply_classification_policy(group)
        return

    false_light_enclosure_effect = bool(
        group.classification == "speech"
        and group.background_type in {"white_balloon", "narration_box"}
        and len(words) == 1
        and 5 <= len(compact) <= 12
        and compact not in ORDINARY_DIALOGUE_WORDS
        and compact not in COMMON_ENGLISH_WORDS
        and not bool(getattr(group, "preserve_as_name", False))
        and not bool(getattr(group, "detected_proper_names", []) or [])
        and len(group.lines) == 1
        and float(getattr(group, "main_text_score", 0.0) or 0.0) <= 0.12
        and bool(getattr(group, "near_image_edge", False))
        and group_area_ratio <= 0.045
        and (
            float(metrics.get("dark_pixel_ratio") or 0.0) >= 0.12
            or float(metrics.get("edge_density") or 0.0) >= 0.04
            or float(metrics.get("local_texture_mean") or 0.0) >= 6.0
        )
    )
    if false_light_enclosure_effect:
        group.inside_balloon_like_region = False
        group.inside_narration_box_like_region = False
        group.parent_balloon_id = ""
        group.region_type = "sfx"
        group.background_type = "sfx_area"
        group.background_metrics = {
            **metrics,
            "background_type": "sfx_area",
            "reason": "single_word_effect_over_false_light_enclosure",
        }
        _set_group_classification(
            group,
            "sfx",
            "single_word_effect_over_false_light_enclosure",
            confidence=float(getattr(group, "main_text_score", 0.0) or 0.0),
            evidence={
                "conflict_resolved": "single_word_effect",
                "main_text_score": round(
                    float(getattr(group, "main_text_score", 0.0) or 0.0),
                    4,
                ),
                "group_area_ratio": round(float(group_area_ratio), 6),
                "dark_pixel_ratio": metrics.get("dark_pixel_ratio"),
                "edge_density": metrics.get("edge_density"),
                "local_texture_mean": metrics.get("local_texture_mean"),
            },
        )
        _apply_classification_policy(group)
        return

    if (
        group.background_type == "narration_box"
        and (
            metrics.get("open_white_narration")
            or metrics.get("open_dark_narration")
        )
        and group.classification in {"speech", "narration", "unknown"}
    ):
        short_spoken_phrase = len(group.lines) <= 2 and len(words) <= 3
        group.classification = "speech" if short_spoken_phrase else "narration"
        group.region_type = group.classification
        group.inside_balloon_like_region = short_spoken_phrase
        group.inside_narration_box_like_region = not short_spoken_phrase
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
    dialogue_markers = {
        "I",
        "I'M",
        "IM",
        "ME",
        "MY",
        "WE",
        "US",
        "OUR",
        "YOU",
        "YOUR",
        "HE",
        "HIS",
        "SHE",
        "HER",
        "THEY",
        "THEIR",
        "WHAT",
        "WHY",
        "HOW",
        "WHEN",
        "WHERE",
        "WHO",
    }
    lacks_dialogue_markers = not any(word in dialogue_markers for word in words)
    short_embedded_label = (
        group.background_type in {"speed_lines", "textured_art"}
        and 4 <= len(words) <= 8
        and len(compact) <= 36
        and len(group.lines) <= 4
        and group_area_ratio <= 0.05
        and lacks_dialogue_markers
        and not re.search(r"[!?]", group.text)
        and not (
            metrics.get("open_white_narration")
            or metrics.get("open_dark_narration")
        )
        and (
            abs(group.angle_degrees) >= 4
            or edge_density >= 0.035
            or local_texture >= 6.0
            or white_ratio < 0.75
            or saturation >= 18.0
        )
    )
    small_embedded_interface_text = (
        (
            group.background_type == "speed_lines"
            and len(words) >= 6
            and len(group.lines) >= 3
        )
        or short_embedded_label
    )
    small_embedded_interface_text = (
        small_embedded_interface_text
        and group_area_ratio <= 0.05
    )
    if small_embedded_interface_text:
        group.classification = "decorative"
        group.region_type = "decorative"
        group.parent_balloon_id = ""
        _apply_classification_policy(group)
        return
    metadata_overlay_on_art = (
        group.background_type == "textured_art"
        and 2 <= len(words) <= 8
        and group_area_ratio <= 0.05
        and bool(re.search(r"\d", group.text))
        and "(" in group.text
        and ")" in group.text
    )
    if metadata_overlay_on_art:
        group.classification = "decorative"
        group.region_type = "decorative"
        group.parent_balloon_id = ""
        _apply_classification_policy(group)
        return
    colored_sign_or_label = (
        1 <= len(words) <= 3
        and 1 <= len(compact) <= 24
        and white_ratio < 0.65
        and saturation >= 25.0
    )
    compact_graphic = (
        len(words) == 1
        and 1 <= len(compact) <= 10
        # Shape alone never proves a sound effect: a lone bold "NO..." is drawn
        # exactly like "ACK!". Only the vocabulary separates them, so ordinary
        # dialogue words are held back from this branch entirely.
        and compact not in ORDINARY_DIALOGUE_WORDS
        and (
            white_ratio < 0.25
            or group.background_type == "speed_lines"
            or edge_density >= 0.05
        )
        and (edge_density >= 0.05 or local_texture >= 6.0)
    )
    embedded_in_art = colored_sign_or_label or compact_graphic
    if not embedded_in_art:
        _recover_lexical_dialogue_on_plain_field(group, metrics, words, saturation)
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


def _recover_lexical_dialogue_on_plain_field(group, metrics, words, saturation):
    """Keep a short, ordinary English line that sits on a plain bright field.

    ``weak_unknown_text`` exists to drop text the region evidence cannot place,
    and it stays that way for anything whose vocabulary is not ordinary English.
    But shortness and upper case are not evidence of a sound effect: a lone
    "NO..." lettered on a blank white panel is speech, and the background here
    proves it is not art. Two independent signals are required - every word is
    known dialogue vocabulary, and the surrounding field is plain, unsaturated
    white - so a stylised effect drawn over a panel is never caught by this.
    """
    if not (group.ignored and group.ignore_reason == "weak_unknown_text"):
        return
    if group.classification != "unknown" or len(group.lines) > 2:
        return
    if group.confidence < 0.78 or saturation >= 25.0:
        return
    if float(metrics.get("context_white_pixel_ratio") or 0.0) < 0.9:
        return
    if not words or not all(
        re.sub(r"[^A-Z]", "", _ascii_fold(word).upper()) in ORDINARY_DIALOGUE_WORDS
        for word in words
    ):
        return
    group.ignored = False
    group.ignore_reason = ""
    group.classification = "speech"
    group.region_type = "speech"
    group.inside_balloon_like_region = True


def _decorative_textured_story_promotion_allowed(group):
    """Allow strong ordinary prose mislabelled as decorative art to translate.

    The textured-art veto protects real SFX/environmental lettering.  It must
    not, however, retain a full sentence such as page-6's ordinary story line
    simply because the coarse background bucket is ``textured_art``.  Require a
    high-confidence, punctuated multi-word clause with clean OCR so short labels,
    credits, SFX and damaged garbage remain fail-closed.
    """
    if group.classification != "decorative":
        return False
    if getattr(group, "background_type", "") not in {"textured_art", "speed_lines"}:
        return False
    if bool(getattr(group, "preserve_as_name", False)):
        return False
    if bool(getattr(group, "quality_reasons", []) or []):
        return False
    try:
        confidence = float(getattr(group, "confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0.84:
        return False
    try:
        main_score = float(getattr(group, "main_text_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        main_score = 0.0
    if main_score < 0.55:
        return False
    body = str(getattr(group, "text", "") or "")
    if not re.search(r"[.!?…]", body):
        return False
    folded_words = [
        re.sub(r"[^A-Z']", "", _ascii_fold(word).upper())
        for word in re.findall(r"[A-Za-zÀ-ÿ']+", body)
    ]
    semantic_words = [
        word
        for word in folded_words
        if len(word.strip("'")) >= 2 and any(ch in "AEIOUY" for ch in word)
    ]
    if len(semantic_words) < 4:
        return False
    dialogue_markers = {
        "I",
        "ME",
        "MY",
        "WE",
        "US",
        "YOU",
        "YOUR",
        "HE",
        "SHE",
        "IT",
        "THEY",
        "THE",
        "A",
        "AN",
        "TO",
        "OF",
        "IN",
        "ON",
        "FOR",
        "WITH",
        "FROM",
        "IS",
        "ARE",
        "WAS",
        "WERE",
        "BE",
        "BEEN",
        "AM",
        "DO",
        "DID",
        "DONE",
        "HAVE",
        "HAS",
        "HAD",
        "WILL",
        "WOULD",
        "CAN",
        "COULD",
        "SHOULD",
        "DON'T",
        "DONT",
        "DIDN'T",
        "DIDNT",
        "CAN'T",
        "CANT",
    }
    return any(word in dialogue_markers for word in folded_words)


def _apply_classification_policy(group):
    if group.classification == "sfx" and not config.TRANSLATE_SFX:
        group.ignored = True
        group.ignore_reason = "sfx_translation_disabled"
        return
    policy = region_taxonomy.resolve_region_policy(
        original_classification=group.classification,
        source_text=group.text,
        preserve_as_name=bool(group.preserve_as_name),
        evidence={"confidence": group.confidence},
    )
    weak_promotion_allowed = region_taxonomy.weak_label_semantic_promotion_allowed(
        group.classification,
        group.text,
    )
    if (
        group.classification == "decorative"
        and getattr(group, "background_type", "") in {
            "textured_art",
            "speed_lines",
            "sfx_area",
        }
        and not _decorative_textured_story_promotion_allowed(group)
        and not (
            (group.background_metrics or {}).get("open_white_narration")
            or (group.background_metrics or {}).get("open_dark_narration")
        )
    ):
        weak_promotion_allowed = False

    if region_taxonomy.is_preservable(policy["normalized_classification"]):
        group.ignored = True
        group.ignore_reason = policy["semantic_role"]
    elif region_taxonomy.is_translatable(policy["normalized_classification"]):
        if weak_promotion_allowed:
            group.ignored = False
            group.ignore_reason = ""
        elif group.classification == "decorative":
            group.ignored = True
            group.ignore_reason = "decorative_text"
        elif group.classification == "unknown":
            group.ignored = True
            group.ignore_reason = "weak_unknown_text"
        else:
            group.ignored = False
            group.ignore_reason = ""
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
    if group.preserve_as_name:
        return False
    if group.classification == "sfx" and not config.TRANSLATE_SFX:
        return False
    # OCR corruption is an OCR problem. A region the recogniser could not read
    # twice is held back for review instead of being handed to the translator,
    # which has no way to recover the characters that were never read.
    if group.ocr_quality_blocked or _ocr_unintelligible_source_text(group.text):
        return False
    # Translation validation runs after the initial target list is built. If a
    # retry still fails, keep the untouched source region instead of erasing it
    # and drawing broken/mixed text back onto the page.
    if group.sent_to_translation and not group.translation_valid:
        return False
    if not _group_has_story_translation_authority(group):
        return False
    if (
        group.classification == "decorative"
        and getattr(group, "background_type", "") in {
            "textured_art",
            "speed_lines",
            "sfx_area",
        }
        and not _decorative_textured_story_promotion_allowed(group)
        and not (
            (group.background_metrics or {}).get("open_white_narration")
            or (group.background_metrics or {}).get("open_dark_narration")
        )
    ):
        return False
    return True


def _group_has_story_translation_authority(group):
    """Single local authority for story text that may enter translation/render.

    Region taxonomy decides preserve/translate.  Legacy weak labels still require
    sentence-level semantic evidence, so increasing story recall does not send SFX,
    credits, promos, logos or URL/watermark text to the provider.
    """

    policy = region_taxonomy.resolve_region_policy(
        original_classification=getattr(group, "classification", ""),
        source_text=getattr(group, "text", ""),
        preserve_as_name=bool(getattr(group, "preserve_as_name", False)),
        evidence={"confidence": getattr(group, "confidence", 0.0)},
    )
    category = policy["normalized_classification"]
    if region_taxonomy.is_preservable(category):
        return False
    if not region_taxonomy.is_translatable(category):
        return False
    return region_taxonomy.weak_label_semantic_promotion_allowed(
        getattr(group, "classification", ""),
        getattr(group, "text", ""),
    )


def _score_group_quality(groups):
    for group in groups:
        score, reasons = score_group_ocr_quality(group)
        if _ignored_decorative_requires_review(group, reasons):
            reasons.append("ignored_decorative_linguistic_content")
            group.manual_review_required = True
        group.quality_score = score
        group.quality_reasons = reasons


def _ignored_decorative_requires_review(group, reasons):
    """Keep unclassified linguistic decorative text visible to quality review.

    Decorative text is normally preserved. A long, punctuated or compact-word
    candidate can instead be missed dialogue, so it must not disappear from the
    audit merely because its classifier is conservative. This does not alter SFX
    routing or render the region.
    """
    if not (
        group.ignored
        and group.classification == "decorative"
        and group.ignore_reason == "decorative_text"
    ):
        return False
    text = clean_ocr_text(group.text)
    compact = re.sub(r"[^A-Za-z0-9]", "", text)
    if len(compact) < 10:
        return False
    has_sentence_punctuation = bool(re.search(r"[,!?…]", text))
    has_compact_segmentation = "compact_word_segmentation_candidate" in reasons
    return has_sentence_punctuation or (
        len(_translation_token_infos(text)) >= 2 and has_compact_segmentation
    )


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

    non_ascii_letters = [
        char
        for char in text
        if char.isalpha() and not ("A" <= char.upper() <= "Z")
    ]
    if non_ascii_letters:
        reasons.append("non_ascii_ocr_artifact")
        score -= min(0.42, 0.18 + len(non_ascii_letters) * 0.06)

    raw_case_tokens = re.findall(r"[A-Za-z]{5,}", text)
    if any(_case_transition_count(token) >= 3 for token in raw_case_tokens):
        reasons.append("mixed_case_ocr_artifact")
        score -= 0.2
    short_case_tokens = re.findall(r"[A-Za-z]{3,5}", text)
    if any(
        re.fullmatch(r"[a-z]{2,}[A-Z]{2,}", token)
        for token in short_case_tokens
    ):
        reasons.append("short_malformed_case_ocr_artifact")
        score -= 0.24

    folded = _ascii_fold(text).upper()
    compact = re.sub(r"[^A-Z0-9]", "", folded)
    tokens = re.findall(r"[A-Za-z0-9']+", folded)
    token_letters = [re.sub(r"[^A-Z]", "", token) for token in tokens]

    if _has_cross_line_lexical_confidence_disagreement(group):
        reasons.append("cross_line_lexical_confidence_disagreement")
        score -= 0.42

    apostrophe_tokens = re.findall(r"'?[A-Z]+(?:'[A-Z]+)+'?", folded)
    valid_contraction = re.compile(
        r"^[A-Z]+(?:'S|'T|'RE|'VE|'LL|'D|'M)$"
    )
    if any(
        token.count("'") > 1 or not valid_contraction.fullmatch(token.strip("'"))
        for token in apostrophe_tokens
    ):
        reasons.append("improbable_apostrophe_pattern")
        score -= 0.45

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


def _case_transition_count(token):
    case_pattern = ["U" if char.isupper() else "L" for char in token if char.isalpha()]
    return sum(
        case_pattern[index] != case_pattern[index - 1]
        for index in range(1, len(case_pattern))
    )


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
                "improbable_number_token",
                "non_ascii_ocr_artifact",
                "mixed_case_ocr_artifact",
                "short_malformed_case_ocr_artifact",
                "cross_line_lexical_confidence_disagreement",
            }
            for reason in group.quality_reasons
        )
    )


def _has_cross_line_lexical_confidence_disagreement(group):
    """Flag a weak OCR line only when stronger neighboring text supports it.

    Unknown vocabulary alone is not enough: names, acronyms and stylized text
    are common in comics.  This signal requires a multi-token line whose words
    are all outside the generic OCR vocabulary, a material confidence drop
    against another line in the same group, and at least two recognized words
    in the neighboring lines.  The result only requests multi-engine fallback;
    it never rewrites the text itself.
    """
    if (
        group.classification not in {"speech", "narration", "unknown"}
        or len(group.lines) < 2
    ):
        return False

    protected_names = {
        re.sub(r"[^A-Z]", "", _ascii_fold(name).upper())
        for name in group.detected_proper_names
        if name
    }
    line_tokens = []
    for line in group.lines:
        tokens = [
            re.sub(r"[^A-Z]", "", token.upper())
            for token in re.findall(
                r"[A-Za-z]+(?:'[A-Za-z]+)?",
                _ascii_fold(line.text),
            )
        ]
        line_tokens.append([token for token in tokens if token])

    for index, (line, tokens) in enumerate(zip(group.lines, line_tokens)):
        if len(tokens) < 2 or max(map(len, tokens)) < 5:
            continue
        if any(
            token in OCR_LEXICAL_REFERENCE_WORDS or token in protected_names
            for token in tokens
        ):
            continue

        peers = [
            peer
            for peer_index, peer in enumerate(group.lines)
            if peer_index != index
        ]
        confidence_gap = max(peer.confidence for peer in peers) - line.confidence
        if confidence_gap < 0.05:
            continue

        peer_tokens = [
            token
            for peer_index, tokens_for_peer in enumerate(line_tokens)
            if peer_index != index
            for token in tokens_for_peer
        ]
        recognized_peer_tokens = sum(
            token in OCR_LEXICAL_REFERENCE_WORDS or token in protected_names
            for token in peer_tokens
        )
        if recognized_peer_tokens >= 2:
            return True

    return False


def _should_skip_paddle_full_for_ignored_decorative(group):
    return bool(
        group.ignored
        and group.classification == "decorative"
        and group.ignore_reason == "decorative_text"
    )


def _ocr_crop_fingerprint(crop):
    """Return a non-reversible fingerprint for regional OCR diagnostics."""
    if crop is None or getattr(crop, "size", 0) == 0:
        return ""
    digest = hashlib.sha256()
    digest.update(str(tuple(int(v) for v in crop.shape)).encode("ascii", "ignore"))
    digest.update(crop.tobytes())
    return digest.hexdigest()


def _classify_paddle_full_call(call):
    """Classify a regional Paddle full call without changing OCR decisions."""
    if call.get("duplicate_region"):
        return "FULL_DUPLICATE"
    if call.get("full_accepted"):
        mobile_exists = bool(call.get("mobile_candidate_exists"))
        mobile_score = float(call.get("mobile_selection_score") or 0.0)
        pre_full_score = float(call.get("pre_full_best_selection_score") or 0.0)
        if not mobile_exists or mobile_score <= pre_full_score + 0.03:
            return "FULL_REQUIRED"
        return "FULL_USEFUL"
    full_score = float(
        call.get("full_adjusted_selection_score")
        or call.get("full_selection_score")
        or 0.0
    )
    pre_full_score = float(call.get("pre_full_best_selection_score") or 0.0)
    if call.get("full_candidate_exists") and full_score < pre_full_score:
        return "FULL_WORSE"
    return "FULL_NO_CHANGE"


# Evidence that the recogniser produced characters, not words: the shapes it
# returned do not form text any language would write. Each of these alone is
# enough to justify one more RapidOCR read of the same pixels.
RAPIDOCR_CORRUPTION_REASONS = frozenset(
    {
        "empty_text",
        "non_ascii_ocr_artifact",
        "mixed_case_ocr_artifact",
        "short_malformed_case_ocr_artifact",
        "improbable_apostrophe_pattern",
        "long_consonant_run",
        "improbable_tokens",
        "many_improbable_characters",
        "long_token_without_spaces",
        "cross_line_lexical_confidence_disagreement",
    }
)

# Weaker signals that are individually normal in comics: a rank ("B2"), a
# stat line, a quiet balloon. Two of them together stop looking like a
# coincidence, one of them never does. Stat-shaped signals
# (``short_improbable_caps_token``, ``improbable_number_token``) are excluded
# outright: "HP 50" trips both and is perfectly good text.
RAPIDOCR_CORROBORATING_REASONS = frozenset(
    {
        "alphanumeric_ocr_artifact",
        "unknown_short_token_in_phrase",
        "low_confidence",
    }
)


# Signals that lower the local OCR quality score but are not proof that the
# source text is unsafe to translate by themselves. Comics regularly contain
# contractions, stutters, rare names, ranks, fantasy vocabulary and compact
# lettering; treating these vocabulary-shaped warnings as hard OCR corruption is
# what holds back readable speech/narration before the translator ever sees it.
RAPIDOCR_WARNING_ONLY_REASONS = frozenset(
    {
        "dictionary_near_miss",
        "generic_ocr_repair_available",
        "short_improbable_caps_token",
        "unknown_short_token_in_phrase",
        "improbable_number_token",
        "adjacent_repeated_word_near_miss",
    }
)


# Warnings that describe a *damaged word shape* inside an otherwise readable
# read: lost spaces, a mangled apostrophe, mixed case, a near-miss word, or a
# consonant wall inside a long punctuated story sentence. They say the read is
# ugly, not that the recogniser invented characters. Anything outside this set
# (non-ASCII glyphs, digits inside words, improbable characters) is evidence of
# characters that were never on the page, and keeps its hard block.
OCR_RECOVERABLE_SUSPICION_REASONS = frozenset(
    {
        "improbable_apostrophe_pattern",
        "mixed_case_ocr_artifact",
        "short_malformed_case_ocr_artifact",
        "long_consonant_run",
        "long_token_without_spaces",
        "compact_word_segmentation_candidate",
        "cross_line_lexical_confidence_disagreement",
        "dictionary_near_miss",
        "adjacent_repeated_word_near_miss",
        "generic_ocr_repair_available",
        "ignored_line_inside_text_region",
        "unknown_short_token_in_phrase",
        "short_improbable_caps_token",
        "medium_low_confidence",
    }
)


def _ordinary_dialogue_words(text):
    """Distinct known English dialogue words in ``text``.

    Apostrophes are split rather than stripped so a contraction that lost its
    space ("I'VETURNED") still yields the pronoun it starts with.
    """
    words = set()
    for token in re.findall(r"[A-Za-z']+", _ascii_fold(str(text or "")).upper()):
        for part in token.split("'"):
            part = re.sub(r"[^A-Z]", "", part)
            if part in ORDINARY_DIALOGUE_WORDS:
                words.add(part)
    return words


def ocr_suspicious_but_translatable(group):
    """True when a suspicious read is still worth handing to the translator.

    Source quality and translation-attempt eligibility are different questions.
    A balloon whose spaces the recogniser dropped is damaged, but it is still
    ordinary English the translator can work with, and holding it back leaves
    untouched source text on the page. The source stays flagged: this only
    decides routing, never that the candidate can be trusted.
    """
    if group.classification not in {"speech", "narration"} and not (
        group.classification == "unknown"
        and _group_has_story_translation_authority(group)
        and float(getattr(group, "main_text_score", 0.0) or 0.0) >= 0.58
    ):
        return False
    reasons = set(group.quality_reasons or [])
    if not reasons or reasons - OCR_RECOVERABLE_SUSPICION_REASONS:
        return False
    text = clean_ocr_text(group.text)
    if not re.search(r"[.?!…,]", text):
        return False
    if _has_embedded_digit_corruption(text):
        return False
    if len(_ordinary_dialogue_words(text)) >= 2:
        return True
    if (
        reasons == {"improbable_apostrophe_pattern"}
        and len(text) >= 40
        and getattr(group, "main_text_score", 0.0) >= 0.75
        and (
            getattr(group, "inside_balloon_like_region", False)
            or getattr(group, "inside_narration_box_like_region", False)
        )
    ):
        return True
    if "long_consonant_run" in reasons:
        compact_letters = re.sub(r"[^A-Za-z]", "", text)
        return bool(
            len(compact_letters) >= 36
            and re.search(r"\s", text)
            and re.search(r"[.?!…]", text)
        )
    return False


# A rank, a level or a stat is written with the digits at the edge of the token
# ("B2", "LEVEL 10", "HP 50"). Digits *inside* a run of letters is not how text
# is written, it is how a recogniser reports glyphs it could not resolve.
_EMBEDDED_DIGIT_TOKEN = re.compile(r"[A-Za-z]+[0-9]+[A-Za-z]+")


def _has_embedded_digit_corruption(text):
    return bool(_EMBEDDED_DIGIT_TOKEN.search(_ascii_fold(str(text or ""))))


def _rapidocr_warning_only_quality(group):
    reasons = set(group.quality_reasons or [])
    if not reasons:
        return False
    if reasons - RAPIDOCR_WARNING_ONLY_REASONS:
        return False
    return not _has_embedded_digit_corruption(group.text)


def rapidocr_region_decision(group):
    """Return ``"accept"`` or ``"retry"`` for one RapidOCR region.

    Deliberately narrower than :func:`group_needs_selective_fallback`, which
    exists to justify a *cross-engine* second opinion. Vocabulary-shaped signals
    (``dictionary_near_miss``, ``compact_word_segmentation_candidate``) are not
    corruption: rare names, ranks and stylised text trip them constantly, and
    re-reading the same pixels with the same engine cannot resolve them anyway.
    """
    if not _rapidocr_recovery_enabled():
        return "accept"
    if group.source_engine and group.source_engine not in {"rapidocr", "mixed"}:
        return "accept"
    if group.preserve_as_name:
        return "accept"
    reasons = set(group.quality_reasons or [])
    if reasons & RAPIDOCR_CORRUPTION_REASONS:
        return "retry"
    if _has_embedded_digit_corruption(group.text):
        return "retry"
    if len(reasons & RAPIDOCR_CORROBORATING_REASONS) >= 2:
        return "retry"
    if float(group.quality_score) < config.RAPIDOCR_RECOVERY_MIN_QUALITY_SCORE:
        if _rapidocr_warning_only_quality(group):
            return "accept"
        return "retry"
    return "accept"


def _rapidocr_recovery_enabled():
    return bool(
        getattr(config, "RAPIDOCR_REGION_RECOVERY", False)
        and config.OCR_ENGINE == "rapidocr"
    )


def _rapidocr_attempt_record(attempt, strategy, text, score, reasons):
    return {
        "attempt": attempt,
        "strategy": strategy,
        "raw_text": text,
        "normalized_text": _normalized_ocr_candidate_text(text),
        "quality_score": round(float(score), 4),
        "quality_reasons": list(reasons or []),
    }


# What a region re-read is allowed to do to the lines it was derived from.
RECOVERY_REPLACE = "replace"
RECOVERY_RECONCILE = "reconcile"
RECOVERY_REJECT = "reject"


def _compact_index(text):
    """``(compact letters, index in text of each letter)``.

    Shares its notion of a letter with :func:`source_completeness.compact`, so a
    fragment located here is the same fragment the completeness contract will
    later hold the pipeline accountable for.
    """

    letters = []
    positions = []
    for index, char in enumerate(str(text or "")):
        folded = "".join(
            part
            for part in unicodedata.normalize("NFKD", char)
            if not unicodedata.combining(part)
        ).upper()
        if len(folded) == 1 and re.fullmatch(r"[A-Z']", folded):
            letters.append(folded)
            positions.append(index)
    return "".join(letters), positions


def _lines_over_box(box, lines):
    """Candidate lines that re-read the same strip of the page, left to right."""

    x, y, width, height = box
    hits = []
    for line in lines:
        lx, ly, lwidth, lheight = line.box
        vertical = min(y + height, ly + lheight) - max(y, ly)
        horizontal = min(x + width, lx + lwidth) - max(x, lx)
        if vertical >= 0.5 * min(height, lheight) and horizontal > 0:
            hits.append(line)
    return sorted(hits, key=lambda line: line.box[0])


def _credible_fragment(text):
    """True when a dropped fragment is text rather than recogniser debris."""

    return bool(source_completeness.meaningful_tokens(text)) and not _looks_like_noise(
        text
    )


def _union_box(left, right):
    x0 = min(left[0], right[0])
    y0 = min(left[1], right[1])
    x1 = max(left[0] + left[2], right[0] + right[2])
    y1 = max(left[1] + left[3], right[1] + right[3])
    return (x0, y0, x1 - x0, y1 - y0)


def _lines_union_box(lines):
    box = None
    for line in lines or []:
        box = line.box if box is None else _union_box(box, line.box)
    return box


def _adoptable_region_groups(candidate_groups, winner, group):
    """The other groups of a region re-read that the region genuinely owns.

    Two guards decide, and both are geometric:

    * containment - a group reaching outside the predecessor region is a
      neighbour the crop padding happened to touch (another balloon, an SFX, a
      title), and is never adopted however well it scored;
    * one owner per source line - if a source line is covered by both the
      winner and another group, the retry split *one* line in two rather than
      finding a second one.  ``reconcile_recovery_lines`` already owns that
      case and re-attaches the fragment to the line it belongs to, so adopting
      it here as well would strand half a sentence in a group of its own.

    What survives both is what Full #9 p076 needs: a whole utterance the crop
    split off at its sentence boundary, sitting on source lines the winner
    never covered.
    """
    winner_lines = _cleanup_lines_for_group(winner)
    predecessors = _cleanup_lines_for_group(group)
    adopted = []
    for other in candidate_groups:
        if other is winner or not _box_inside(other.box, group.box):
            continue
        if rapidocr_region_decision(other) != "accept":
            continue
        other_lines = _cleanup_lines_for_group(other)
        if any(
            _lines_over_box(predecessor.box, winner_lines)
            and _lines_over_box(predecessor.box, other_lines)
            for predecessor in predecessors
        ):
            continue
        adopted.append(other)
    return adopted


def reconcile_recovery_lines(predecessor_lines, candidate_lines):
    """Decide what a recovery read may do to its predecessor; return the lines.

    Returns ``(decision, lines, reason)`` where ``decision`` is one of
    :data:`RECOVERY_REPLACE`, :data:`RECOVERY_RECONCILE`, :data:`RECOVERY_REJECT`.

    A second read is allowed to supersede the first, and usually should: it is
    the same recogniser at the scale it prefers, and it fixes spacing the page
    scale destroyed.  What it is not allowed to do is take credible lexical
    content with it.  Confidence does not decide this - a truncated read is
    confident precisely because it is reading fewer glyphs - so the comparison
    is over letters and the geometry they came from.

    The predecessor is only re-attached where the overlap is unambiguous: a
    shared run of letters with the surviving fragment at one end.  Two reads
    that disagree in the middle are a conflict, and a conflict keeps the older
    evidence rather than inventing a sentence neither read produced.
    """

    predecessor_lines = list(predecessor_lines or [])
    candidate_lines = list(candidate_lines or [])
    if not candidate_lines:
        return RECOVERY_REJECT, predecessor_lines, "recovery_no_candidate_lines"
    if not predecessor_lines:
        return RECOVERY_REPLACE, candidate_lines, "recovery_no_predecessor"

    candidate_compact = source_completeness.compact(
        " ".join(line.text for line in candidate_lines)
    )
    decision = RECOVERY_REPLACE
    reasons = []
    plans = []  # deferred: a later conflict must not leave half a merge behind.
    noise_dropped = []
    retained = []
    for predecessor in predecessor_lines:
        text = predecessor.text or predecessor.raw_text or ""
        compact_text, positions = _compact_index(text)
        overlapping = _lines_over_box(predecessor.box, candidate_lines)
        if not overlapping:
            # Nothing in the second read covers these pixels, so nothing
            # superseded them.  Dropping the line here would leave the source
            # region with no owner at all - no group, no review, and no mask -
            # which is how visible dialogue survives into the rendered page.
            # A token too corrupted to yield letters ("iK3H") is exactly the
            # case the lexical checks below cannot speak for, so geometry
            # decides: only a read that saw the region may replace it.
            retained.append(predecessor)
            reasons.append("recovery_uncovered_predecessor_retained")
            if decision != RECOVERY_REJECT:
                decision = RECOVERY_RECONCILE
            continue
        covered = "".join(
            source_completeness.compact(line.text) for line in overlapping
        )
        if compact_text and compact_text in covered:
            continue
        if not [
            token
            for token in source_completeness.meaningful_tokens(text)
            if token not in candidate_compact
        ]:
            # Respaced, re-cased, re-punctuated or split across lines: every
            # token the predecessor owned is still somewhere in the new read.
            continue
        if not (covered and covered in compact_text):
            decision = RECOVERY_REJECT
            reasons.append("recovery_lexical_conflict")
            continue
        start = compact_text.index(covered)
        end = start + len(covered)
        head = text[: positions[start]] if start else ""
        tail = text[positions[end] :] if end < len(positions) else ""
        head_ok = _credible_fragment(head)
        tail_ok = _credible_fragment(tail)
        if not head_ok and not tail_ok:
            noise_dropped.append(predecessor)
            reasons.append("recovery_noise_removed")
            continue
        if head_ok:
            plans.append((overlapping[0], head.strip(), "", predecessor))
        if tail_ok:
            plans.append((overlapping[-1], "", tail.strip(), predecessor))
        if decision != RECOVERY_REJECT:
            decision = RECOVERY_RECONCILE
        reasons.append("recovery_reconciled_predecessor_fragment")

    if decision == RECOVERY_REJECT:
        return RECOVERY_REJECT, predecessor_lines, ";".join(dict.fromkeys(reasons))

    for line, head, tail, predecessor in plans:
        line.text = " ".join(part for part in (head, line.text, tail) if part).strip()
        line.raw_text = line.text
        line.box = _union_box(line.box, predecessor.box)
        # Re-attaching the tail without its geometry is how the tail became
        # invisible to every cleaning mask: masks are built from ``polygon``,
        # never from ``box``.  The hull of both reads is the narrowest shape
        # that still contains the glyphs each of them actually saw.
        line.polygon = cv2.convexHull(
            np.vstack(
                (
                    np.asarray(line.polygon, dtype=np.int32).reshape(-1, 2),
                    np.asarray(predecessor.polygon, dtype=np.int32).reshape(-1, 2),
                )
            )
        ).reshape(-1, 2)
        line.metadata = {
            **(line.metadata or {}),
            "recovery_reconciled_from": head or tail,
        }
    for line in noise_dropped:
        line.metadata = {
            **(line.metadata or {}),
            "ocr_discard_reason": "recovery_noise_removed",
        }
    for line in retained:
        line.metadata = {
            **(line.metadata or {}),
            "recovery_retained_reason": "recovery_uncovered_predecessor_retained",
        }
    return (
        decision,
        candidate_lines + retained,
        ";".join(dict.fromkeys(reasons)) or "recovery_safe_replace",
    )


def apply_rapidocr_region_recovery(
    original_bgr,
    raw_lines,
    groups,
    ocr_lang,
    page_index=None,
):
    """Re-read suspicious RapidOCR regions once, with RapidOCR, on their own crop.

    The fast path stays fast: a region whose diagnostics look clean costs exactly
    one OCR call and is never touched here. A region carrying corruption evidence
    costs exactly one more — the same recogniser, the same pixels, read at the
    scale the recogniser prefers instead of at whatever scale the page happened
    to have. Paddle is never invoked, no page or chapter is reprocessed, and a
    second bad reading is reported rather than silently preferred.
    """
    lines = list(raw_lines or [])
    if not _rapidocr_recovery_enabled():
        return lines, []
    if original_bgr is None or original_bgr.size == 0:
        return lines, []

    suspects = [group for group in groups if rapidocr_region_decision(group) == "retry"]
    accepted = len(groups) - len(suspects)
    record_count("rapidocr_recovery.primary_accepted", accepted, page_index=page_index)
    if not suspects:
        return lines, []
    suspects = suspects[: config.RAPIDOCR_RECOVERY_MAX_REGIONS_PER_PAGE]

    engine = None
    records = []
    for group in suspects:
        crop_box = _fallback_crop_box(group, original_bgr.shape)
        x, y, width, height = crop_box
        crop = original_bgr[y : y + height, x : x + width]
        record = {
            "region_id": group.region_id or group.group_id,
            "group_id": group.group_id,
            "crop_box": list(crop_box),
            "attempts": [
                _rapidocr_attempt_record(
                    1,
                    "rapidocr_page_scale",
                    group.text,
                    group.quality_score,
                    group.quality_reasons,
                )
            ],
            "selection": "rejected",
            "reason": "rapidocr_retry_not_better",
        }
        records.append(record)
        if crop.size == 0:
            record["reason"] = "rapidocr_retry_empty_crop"
            record_count("rapidocr_recovery.retry_failed", page_index=page_index)
            continue

        record_count("rapidocr_recovery.retry_attempted", page_index=page_index)
        try:
            with profile_step(
                "rapidocr_recovery.ocr",
                page_index=page_index,
                metadata={"group_id": group.group_id},
            ):
                if engine is None:
                    engine = OCREngine(ocr_lang, engine="rapidocr", fallback_engine="")
                crop_lines = engine.detect_lines(
                    crop, page=page_index, rapidocr_upscale=True
                )
        except Exception as exc:  # noqa: BLE001 - a failed retry must fail closed.
            record["reason"] = "rapidocr_retry_error"
            record["error"] = f"{type(exc).__name__}: {exc}"
            record_count("rapidocr_recovery.retry_failed", page_index=page_index)
            continue

        crop_lines = [
            _offset_line(line, x, y, "rapidocr", group.group_id)
            for line in (crop_lines or [])
        ]
        candidate_groups = _candidate_groups_for_fallback(
            original_bgr, crop_lines, page_index=page_index
        )
        candidate = _best_candidate_group(candidate_groups, group.box)
        if candidate is None:
            record["reason"] = "rapidocr_retry_no_candidate"
            record_count("rapidocr_recovery.retry_failed", page_index=page_index)
            continue

        # One region can legitimately re-read as more than one group: a sentence
        # boundary inside the same speech container splits the crop, and only one
        # of the pieces can win the overlap. Keeping only the winner discards the
        # rest of the region's own text however well it was read - Full #9 p076
        # read "HEY!" at 0.96 and dropped it for the longer group below it.
        # Ownership decides what may join: see ``_adoptable_region_groups``.
        region_groups = [candidate] + _adoptable_region_groups(
            candidate_groups, candidate, group
        )
        region_groups.sort(key=lambda item: (item.box[1], item.box[0]))
        for item in region_groups:
            item.source_engine = "rapidocr"
        record["region_groups"] = [
            {"text": item.text, "box": [int(value) for value in item.box]}
            for item in region_groups
        ]
        record["attempts"].append(
            _rapidocr_attempt_record(
                2,
                "rapidocr_region_crop_upscaled",
                " ".join(item.text for item in region_groups),
                candidate.quality_score,
                candidate.quality_reasons,
            )
        )
        # Attempt 1 already failed the policy, so there is no "both acceptable"
        # tie to break: attempt 2 wins only by clearing the same bar attempt 1
        # could not. Nothing is preferred merely for being newer.
        if rapidocr_region_decision(candidate) != "accept":
            record_count("rapidocr_recovery.retry_rejected", page_index=page_index)
            continue

        # Clearing the quality bar is not permission to take the predecessor's
        # content with it: a truncated read scores well precisely because there
        # is less of it left to score.
        predecessors = _cleanup_lines_for_group(group)
        candidate_lines = []
        seen_candidate_lines = set()
        for item in region_groups:
            for line in _cleanup_lines_for_group(item):
                if id(line) in seen_candidate_lines:
                    continue
                seen_candidate_lines.add(id(line))
                candidate_lines.append(line)
        candidate_lines.sort(key=lambda line: (line.box[1], line.box[0]))
        decision, reconciled, decision_reason = reconcile_recovery_lines(
            predecessors, candidate_lines
        )
        record["decision"] = decision
        record["decision_reason"] = decision_reason
        record["predecessor_text"] = " ".join(line.text for line in predecessors)[:400]
        record["predecessor_bbox"] = list(_lines_union_box(predecessors) or ())
        record["reconciled_text"] = " ".join(line.text for line in reconciled)[:400]
        record["reconciled_bbox"] = list(_lines_union_box(reconciled) or ())
        if decision == RECOVERY_REJECT:
            record["reason"] = decision_reason
            record_count("rapidocr_recovery.retry_rejected", page_index=page_index)
            record_count(
                "rapidocr_recovery.prevented_lexical_loss", page_index=page_index
            )
            continue
        if decision == RECOVERY_RECONCILE:
            record_count("rapidocr_recovery.reconciled", page_index=page_index)
            record_count(
                "rapidocr_recovery.prevented_lexical_loss", page_index=page_index
            )
            record_count(
                "rapidocr_recovery.prevented_geometry_loss", page_index=page_index
            )
        else:
            record_count("rapidocr_recovery.safe_replace", page_index=page_index)
        record["selection"] = "attempt_2"
        record["reason"] = "rapidocr_retry_accepted"
        record_count("rapidocr_recovery.retry_selected", page_index=page_index)
        replaced = {id(line) for line in predecessors}
        lines = [line for line in lines if id(line) not in replaced]
        for line in reconciled:
            line.metadata = {
                **(line.metadata or {}),
                "rapidocr_recovery_used": True,
                "rapidocr_recovery_attempt": 2,
                "rapidocr_recovery_strategy": "rapidocr_region_crop_upscaled",
                "original_group_id": group.group_id,
                "original_text": group.text,
            }
            lines.append(line)

    lines.sort(key=lambda line: (line.box[1], line.box[0]))
    return lines, records


def enforce_rapidocr_quality_gate(groups, page_index=None):
    """Hold back any region that still fails the policy after its last attempt."""
    if not _rapidocr_recovery_enabled():
        return []
    blocked = []
    warning_accepted = 0
    recoverable = 0
    for group in groups:
        decision = rapidocr_region_decision(group)
        if group.ocr_quality_blocked or decision == "accept":
            if (
                not group.ocr_quality_blocked
                and decision == "accept"
                and _rapidocr_warning_only_quality(group)
            ):
                warning_accepted += 1
            continue
        if ocr_suspicious_but_translatable(group):
            # The read stays suspicious and keeps every warning it earned; only
            # the routing changes, so the translator sees text it can actually
            # work with and the candidate still faces the normal validators.
            group.quality_evidence = {
                **(group.quality_evidence or {}),
                "ocr_source_suspicious": True,
                "ocr_source_quality_reasons": list(group.quality_reasons or []),
                "ocr_source_quality_score": float(group.quality_score),
            }
            recoverable += 1
            continue
        group.ocr_quality_blocked = True
        group.ocr_quality_block_reason = ";".join(group.quality_reasons) or "low_quality_score"
        group.manual_review_required = True
        blocked.append(group)
    if recoverable:
        record_count(
            "ocr_suspicious_translation_allowed",
            recoverable,
            page_index=page_index,
        )
    if warning_accepted:
        record_count(
            "ocr_quality_warning_accept",
            warning_accepted,
            page_index=page_index,
        )
    if blocked:
        record_count(
            "rapidocr_recovery.all_attempts_rejected",
            len(blocked),
            page_index=page_index,
        )
        record_count("ocr_quality_blocked", len(blocked), page_index=page_index)
        record_count("translation_held_by_ocr", len(blocked), page_index=page_index)
    return blocked


def apply_selective_ocr_fallbacks(
    original_bgr,
    raw_lines,
    groups,
    ocr_lang,
    page_index,
    fast_ocr_budget=None,
):
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
    record_count("selective_fallback.suspect_groups", len(suspects), page_index=page_index)
    if not suspects:
        return raw_lines, []

    updated_lines = list(raw_lines)
    records = []
    consumed_line_ids = set()
    regional_engines = {}
    full_region_fingerprints = {}
    for group in suspects:
        group_started = time.perf_counter()
        current_quality = group.quality_score
        with profile_step("selective_fallback.crop_box", page_index=page_index):
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
        candidate_options = []
        paddle_full_calls = []
        mobile_summary = {
            "attempted": False,
            "candidate_exists": False,
            "candidate_sufficient": False,
            "quality_score": 0.0,
            "selection_score": 0.0,
            "rejection_reasons": ["mobile_not_attempted"],
        }
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
        crop_fingerprint = _ocr_crop_fingerprint(crop)
        for engine_name, variant in (("paddle_mobile", "paddle_mobile"), ("paddle", "paddle_full")):
            started = time.perf_counter()
            pre_attempt_best_engine = best_engine
            pre_attempt_best_quality = best_quality
            pre_attempt_best_selection_score = best_selection_score
            full_call_record = None
            if variant == "paddle_full":
                if fast_ocr_budget is not None:
                    allowed, budget_reason = fast_ocr_budget.allow(
                        kind="paddle_full_region", page=page_index
                    )
                    if not allowed:
                        attempts.append(
                            {
                                "engine": variant,
                                "skipped": True,
                                "skip_reason": budget_reason,
                            }
                        )
                        break
                duplicate_source = full_region_fingerprints.get(crop_fingerprint)
                duplicate_region = duplicate_source is not None
                if duplicate_source is None:
                    full_region_fingerprints[crop_fingerprint] = {
                        "group_id": group.group_id,
                        "crop_box": list(crop_box),
                    }
                full_call_record = {
                    "page_index": page_index,
                    "group_id": group.group_id,
                    "region_id": group.region_id or group.group_id,
                    "crop_fingerprint": crop_fingerprint,
                    "crop_box": list(crop_box),
                    "crop_width": int(w),
                    "crop_height": int(h),
                    "classification_before": group.classification,
                    "fallback_reason": ";".join(group.quality_reasons),
                    "previous_engine": "paddle_mobile" if mobile_summary.get("attempted") else "rapidocr",
                    "previous_quality_score": round(float(current_quality or 0.0), 4),
                    "mobile_candidate_exists": bool(mobile_summary.get("candidate_exists")),
                    "mobile_quality_score": round(float(mobile_summary.get("quality_score") or 0.0), 4),
                    "mobile_selection_score": round(float(mobile_summary.get("selection_score") or 0.0), 4),
                    "mobile_candidate_sufficient": bool(mobile_summary.get("candidate_sufficient")),
                    "mobile_rejection_reasons": list(mobile_summary.get("rejection_reasons") or []),
                    "pre_full_best_engine": pre_attempt_best_engine,
                    "pre_full_best_quality": round(float(pre_attempt_best_quality or 0.0), 4),
                    "pre_full_best_selection_score": round(float(pre_attempt_best_selection_score or 0.0), 4),
                    "duplicate_region": duplicate_region,
                    "duplicate_of": duplicate_source,
                    "full_candidate_exists": False,
                    "full_quality_score": 0.0,
                    "full_selection_score": 0.0,
                    "full_adjusted_selection_score": 0.0,
                    "full_accepted": False,
                    "full_rejected": False,
                    "result_changed": False,
                    "call_classification": "",
                    "final_engine": "",
                    "final_selection_score": 0.0,
                    "group_translated_or_ignored": "ignored" if group.ignored else "candidate",
                    "classification_final": group.classification,
                }
                record_count("selective_fallback.paddle_full_calls", page_index=page_index)
                record_count(
                    f"selective_fallback.paddle_full_for_{group.classification or 'unknown'}",
                    page_index=page_index,
                )
                if duplicate_region:
                    record_count("selective_fallback.paddle_full_duplicate_region", page_index=page_index)
                if mobile_summary.get("candidate_exists"):
                    record_count("selective_fallback.paddle_full_after_mobile_candidate", page_index=page_index)
                if mobile_summary.get("candidate_sufficient"):
                    record_count("selective_fallback.paddle_full_after_mobile_sufficient", page_index=page_index)
                if mobile_summary.get("attempted") and not mobile_summary.get("candidate_exists"):
                    record_count("selective_fallback.paddle_full_after_mobile_failure", page_index=page_index)
            try:
                with profile_step(
                    f"selective_fallback.ocr.{variant}",
                    page_index=page_index,
                    metadata={"group_id": group.group_id},
                ):
                    if fast_ocr_budget is not None:
                        crop_lines, timeout_metadata = run_ocr_with_timeout(
                            crop,
                            lang=ocr_lang,
                            engine_name=engine_name,
                            page=page_index,
                            timeout_seconds=fast_ocr_budget.region_timeout_seconds,
                        )
                        if timeout_metadata.get("timeout"):
                            raise TimeoutError("fast_ocr_region_timeout")
                    else:
                        engine = regional_engines.get(engine_name)
                        if engine is None:
                            engine = OCREngine(ocr_lang, engine=engine_name, fallback_engine="")
                            regional_engines[engine_name] = engine
                        crop_lines = engine.detect_lines(crop, page=page_index)
                elapsed = time.perf_counter() - started
                if fast_ocr_budget is not None:
                    fast_ocr_budget.record(
                        kind=(
                            "paddle_full_region"
                            if variant == "paddle_full"
                            else "paddle_mobile_region"
                        ),
                        elapsed=elapsed,
                        page=page_index,
                    )
                with profile_step("selective_fallback.offset_lines", page_index=page_index, items=len(crop_lines)):
                    crop_lines = [_offset_line(line, x, y, variant, group.group_id) for line in crop_lines]
                with profile_step("selective_fallback.candidate_groups_for_fallback", page_index=page_index, items=len(crop_lines)):
                    candidate_groups = _candidate_groups_for_fallback(
                        original_bgr,
                        crop_lines,
                        page_index=page_index,
                    )
                with profile_step("selective_fallback.best_candidate_group", page_index=page_index, items=len(candidate_groups)):
                    best_candidate = _best_candidate_group(candidate_groups, group.box)
                candidate_lines = (
                    _cleanup_lines_for_group(best_candidate)
                    if best_candidate
                    else []
                )
                with profile_step("selective_fallback.candidate_scoring", page_index=page_index):
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
                    shrink_penalty = _content_shrink_penalty(
                        content_retention,
                        len(context_tokens),
                        cross_region_suspected=(
                            "possible_cross_region_group" in group.quality_reasons
                        ),
                    )
                    expansion_penalty = (
                        0.35
                        if len(context_compact) >= 8
                        and len(candidate_compact) > len(context_compact) * 1.8
                        else 0.0
                    )
                    cross_region_resolution_bonus = _cross_region_resolution_bonus(
                        group,
                        best_candidate,
                        content_retention,
                    )
                    selection_score = (
                        candidate_quality
                        + min(0.12, proposal_support * 0.06)
                        + context_coverage * 0.35
                        + content_retention * 0.35
                        - shrink_penalty
                        - expansion_penalty
                        + cross_region_resolution_bonus
                    )
                attempt = {
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
                    "cross_region_resolution_bonus": round(
                        cross_region_resolution_bonus,
                        4,
                    ),
                    "text": candidate_text,
                }
                attempts.append(attempt)
                if candidate_lines:
                    candidate_options.append(
                        {
                            "lines": candidate_lines,
                            "quality": candidate_quality,
                            "selection_score": selection_score,
                            "engine": variant,
                            "text": candidate_text,
                            "attempt": attempt,
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
                candidate_sufficient = bool(
                    best_quality >= 0.82
                    and not (
                        variant == "paddle_mobile"
                        and (severe_original_suspicion or candidate_still_suspicious)
                    )
                )
                if variant == "paddle_mobile":
                    rejection_reasons = []
                    if not candidate_lines:
                        rejection_reasons.append("mobile_no_candidate")
                    if best_quality < 0.82:
                        rejection_reasons.append("best_quality_below_acceptance")
                    if severe_original_suspicion:
                        rejection_reasons.append("original_severe_suspicion")
                    if candidate_still_suspicious:
                        rejection_reasons.append("mobile_candidate_still_suspicious")
                    if not rejection_reasons and not candidate_sufficient:
                        rejection_reasons.append("mobile_not_sufficient")
                    mobile_summary = {
                        "attempted": True,
                        "candidate_exists": bool(candidate_lines),
                        "candidate_sufficient": candidate_sufficient,
                        "quality_score": candidate_quality,
                        "selection_score": selection_score,
                        "rejection_reasons": rejection_reasons,
                    }
                    attempt.update(
                        {
                            "candidate_sufficient": candidate_sufficient,
                            "escalation_reasons": rejection_reasons,
                        }
                    )
                    if _should_skip_paddle_full_for_ignored_decorative(group):
                        attempt["paddle_full_skipped_reason"] = "ignored_decorative"
                        record_count(
                            "selective_fallback.paddle_full_skipped_ignored_decorative",
                            page_index=page_index,
                        )
                        break
                elif full_call_record is not None:
                    full_call_record.update(
                        {
                            "elapsed_seconds": round(elapsed, 6),
                            "full_candidate_exists": bool(candidate_lines),
                            "full_quality_score": round(float(candidate_quality or 0.0), 4),
                            "full_selection_score": round(float(selection_score or 0.0), 4),
                            "detected_line_count": len(crop_lines),
                            "selected_line_count": len(candidate_lines),
                        }
                    )
                    attempt["paddle_full_call"] = full_call_record
                if best_quality >= 0.82 and not (
                    variant == "paddle_mobile"
                    and (severe_original_suspicion or candidate_still_suspicious)
                ):
                    break
            except Exception as exc:
                error_attempt = {
                    "engine": variant,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                if variant == "paddle_mobile":
                    mobile_summary = {
                        "attempted": True,
                        "candidate_exists": False,
                        "candidate_sufficient": False,
                        "quality_score": 0.0,
                        "selection_score": 0.0,
                        "rejection_reasons": ["mobile_error"],
                    }
                elif full_call_record is not None:
                    full_call_record.update(
                        {
                            "elapsed_seconds": round(time.perf_counter() - started, 6),
                            "error": error_attempt["error"],
                        }
                    )
                    error_attempt["paddle_full_call"] = full_call_record
                attempts.append(error_attempt)

        with profile_step(
            "selective_fallback.candidate_agreement_scoring",
            page_index=page_index,
            items=len(candidate_options),
        ):
            normalized_original = _normalized_ocr_candidate_text(group.text)
            normalized_options = [
                _normalized_ocr_candidate_text(option["text"])
                for option in candidate_options
            ]
            for option_index, option in enumerate(candidate_options):
                normalized = normalized_options[option_index]
                original_agreement_bonus = (
                    0.22
                    if normalized and normalized == normalized_original
                    else 0.0
                )
                peer_agreement_bonus = (
                    0.28
                    if normalized
                    and any(
                        normalized == peer
                        for peer_index, peer in enumerate(normalized_options)
                        if peer_index != option_index
                    )
                    else 0.0
                )
                adjusted_score = (
                    option["selection_score"]
                    + original_agreement_bonus
                    + peer_agreement_bonus
                )
                option["attempt"].update(
                    {
                        "original_engine_agreement_bonus": original_agreement_bonus,
                        "peer_engine_agreement_bonus": peer_agreement_bonus,
                        "adjusted_selection_score": round(adjusted_score, 4),
                    }
                )
                if option["engine"] == "paddle_full":
                    full_call = option["attempt"].get("paddle_full_call")
                    if full_call is not None:
                        full_call["full_adjusted_selection_score"] = round(adjusted_score, 4)
                if adjusted_score > best_selection_score + 0.03:
                    best_lines = option["lines"]
                    best_quality = option["quality"]
                    best_selection_score = adjusted_score
                    best_engine = option["engine"]

        for attempt in attempts:
            full_call = attempt.get("paddle_full_call")
            if not full_call:
                continue
            full_call["final_engine"] = best_engine
            full_call["final_selection_score"] = round(float(best_selection_score or 0.0), 4)
            full_call["full_accepted"] = bool(best_engine == "paddle_full" and best_lines)
            full_call["full_rejected"] = not full_call["full_accepted"]
            full_call["result_changed"] = full_call["full_accepted"]
            full_call["classification_final"] = group.classification
            full_call["group_translated_or_ignored"] = "ignored" if group.ignored else "candidate"
            full_call["call_classification"] = _classify_paddle_full_call(full_call)
            paddle_full_calls.append(full_call)
            record_count(
                f"selective_fallback.paddle_full_{full_call['call_classification'].lower()}",
                page_index=page_index,
            )
            if full_call["full_accepted"]:
                record_count("selective_fallback.paddle_full_accepted", page_index=page_index)
            else:
                record_count("selective_fallback.paddle_full_rejected", page_index=page_index)

        record = {
            "group_id": group.group_id,
            "region_id": group.region_id or group.group_id,
            "original_text": group.text,
            "original_quality_score": round(current_quality, 4),
            "quality_reasons": list(group.quality_reasons),
            "crop_box": list(crop_box),
            "crop_fingerprint": crop_fingerprint,
            "attempts": attempts,
            "paddle_full_calls": paddle_full_calls,
            "fallback_used": bool(best_lines),
            "fallback_variant": best_engine,
        }
        records.append(record)
        record_group(
            page_index=page_index,
            group_id=group.group_id,
            elapsed_seconds=time.perf_counter() - group_started,
            line_count=len(group.lines),
            classification=group.classification,
            fallback_used=bool(best_lines),
            dominant_step="selective_fallback",
        )
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


def _normalized_ocr_candidate_text(text):
    return re.sub(r"[^A-Z0-9']", "", _ascii_fold(text).upper())


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


def _candidate_groups_for_fallback(original_bgr, crop_lines, page_index=None):
    if not crop_lines:
        return []
    with profile_step(
        "fallback_candidate_groups.assign_visual_white_regions",
        page_index=page_index,
        items=len(crop_lines),
    ):
        _assign_visual_white_regions(original_bgr, crop_lines)
    with profile_step(
        "fallback_candidate_groups.candidate_from_line",
        page_index=page_index,
        items=len(crop_lines),
    ):
        candidates = [_candidate_from_line(line, original_bgr.shape) for line in crop_lines]
    usable_lines = [candidate.line for candidate in candidates if not candidate.ignored]
    with profile_step(
        "fallback_candidate_groups.group_lines",
        page_index=page_index,
        items=len(usable_lines),
    ):
        groups = _group_lines(usable_lines, page_index=page_index)
    with profile_step("fallback_candidate_groups.split_sentence_boundaries", page_index=page_index, items=len(groups)):
        groups = _split_groups_at_sentence_boundaries(groups)
    with profile_step("fallback_candidate_groups.split_detached_story_outliers", page_index=page_index, items=len(groups)):
        groups = _split_detached_story_outlier_lines(groups)
    with profile_step("fallback_candidate_groups.associate_ignored_cleanup_lines", page_index=page_index, items=len(candidates)):
        _associate_ignored_cleanup_lines(groups, candidates, original_bgr.shape, page_index=page_index)
    with profile_step("fallback_candidate_groups.repair_group_texts", page_index=page_index, items=len(groups)):
        _repair_group_texts(groups)
    with profile_step("fallback_candidate_groups.filter_groups", page_index=page_index, items=len(groups)):
        _filter_groups(groups, original_bgr.shape)
    with profile_step("fallback_candidate_groups.classify_groups", page_index=page_index, items=len(groups)):
        _classify_groups(groups, original_bgr, page_index=page_index)
    with profile_step("fallback_candidate_groups.score_group_quality", page_index=page_index, items=len(groups)):
        _score_group_quality(groups)
    with profile_step("fallback_candidate_groups.assign_region_metadata", page_index=page_index, items=len(groups)):
        _assign_region_metadata(groups)
    return groups


def _split_groups_at_sentence_boundaries(groups):
    result = []
    for group in groups:
        lines = reading_order(group.lines)
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
        if len(chunks) <= 1:
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


def _split_detached_story_outlier_lines(groups):
    """Split a short visual/SFX seed away from a coherent story block.

    OCR can read a large effect drawn above a narration panel before the real
    narration lines.  If that short effect becomes the seed group, the following
    story lines are merged into an over-tall box; the renderer then tries to
    clean both the effect and the narration with one Portuguese candidate.  This
    pass keeps the coherent story cluster translatable and leaves the detached
    short line preserved/reviewable as its own ignored group.
    """

    result = []
    for group in groups:
        lines = reading_order(group.lines)
        if len(lines) < 4:
            result.append(group)
            continue

        split = None
        for index in (0, len(lines) - 1):
            candidate = lines[index]
            core = lines[1:] if index == 0 else lines[:-1]
            if _line_is_detached_story_outlier(candidate, core):
                split = (candidate, core)
                break

        if split is None:
            result.append(group)
            continue

        outlier, core = split
        story_group = TextGroup(
            group_id=group.group_id,
            lines=reading_order(core),
            text=clean_ocr_text(" ".join(line.text for line in core)),
        )
        outlier_group = TextGroup(
            group_id=f"{group.group_id}_DETACHED",
            lines=[outlier],
            text=clean_ocr_text(outlier.text),
            ignored=True,
            ignore_reason="detached_story_outlier_line",
        )
        # Preserve the story unit first so its BALAO_N identity remains stable
        # after the final renumbering; the detached short effect is not a
        # translation target.
        result.extend([story_group, outlier_group])

    for index, group in enumerate(result, start=1):
        group.group_id = f"BALAO_{index}"
    return result


def _line_is_detached_story_outlier(line, core_lines):
    if len(core_lines) < 3:
        return False

    compact = re.sub(r"[^A-Z]", "", _ascii_fold(line.text or "").upper())
    if not compact or len(compact) > 4 or compact in COMMON_ENGLISH_WORDS:
        return False

    core_text = clean_ocr_text(" ".join(item.text for item in core_lines))
    core_words = re.findall(r"[A-Za-zÀ-ÿ]{2,}", core_text)
    if len(core_words) < 3:
        return False

    core = reading_order(core_lines)
    core_heights = [max(1, item.box[3]) for item in core]
    median_height = float(np.median(core_heights))
    if median_height <= 0:
        return False
    core_areas = [max(1, item.box[2] * item.box[3]) for item in core]
    median_area = float(np.median(core_areas))
    lx, ly, lw, lh = line.box
    if not (
        lh >= median_height * 2.4
        or lw * lh >= median_area * 4.0
        or float(getattr(line, "confidence", 1.0)) <= 0.85
    ):
        return False

    core_box = _union_boxes([item.box for item in core])
    cx, cy, cw, ch = core_box
    vertical_gap = max(cy - (ly + lh), ly - (cy + ch))
    if vertical_gap < max(72.0, median_height * 2.2):
        return False
    horizontal_overlap = max(0, min(cx + cw, lx + lw) - max(cx, lx))
    if horizontal_overlap / max(1, min(cw, lw)) < 0.30:
        return False

    core_gaps = [
        following.box[1] - (current.box[1] + current.box[3])
        for current, following in zip(core, core[1:])
    ]
    if core_gaps and max(core_gaps) > max(32.0, median_height * 0.85):
        return False
    core_widths = [max(1, item.box[2]) for item in core]
    if min(core_widths) / max(core_widths) < 0.35:
        return False
    return True


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
        region_x, region_y, region_width, region_height, region_area = (
            int(value) for value in stats[label]
        )
        touches_edge = sum(
            (
                region_x <= 1,
                region_y <= 1,
                region_x + region_width >= image_width - 1,
                region_y + region_height >= image_height - 1,
            )
        )
        width_ratio = region_width / max(1, box_width)
        height_ratio = region_height / max(1, box_height)
        region_box_area_ratio = (
            region_width * region_height
        ) / max(1, image_width * image_height)
        # This supplements contour detection only for compact containers.
        # Large white components remain under the existing narration rules.
        enclosed = bool(
            touches_edge == 0
            and region_width >= max(box_width + 8, box_width * 1.15)
            and region_height >= max(box_height + 10, box_height * 1.35)
            and region_box_area_ratio <= 0.18
        )
        line.metadata = {
            **(line.metadata or {}),
            "visual_white_region_id": label,
            "visual_white_region_coverage": round(coverage, 4),
            "visual_white_region_box": [
                region_x,
                region_y,
                region_width,
                region_height,
            ],
            "visual_white_region_area_ratio": round(
                region_area / max(1, image_width * image_height),
                6,
            ),
            "visual_white_region_rectangularity": round(
                region_area / max(1, region_width * region_height),
                4,
            ),
            "visual_white_region_width_ratio": round(width_ratio, 4),
            "visual_white_region_height_ratio": round(height_ratio, 4),
            "visual_white_region_touches_edge": int(touches_edge),
            "visual_white_region_enclosed": enclosed,
        }


_RECLAIMABLE_IGNORE_REASONS = frozenset(
    {"noise_like_text", "too_few_useful_chars", "low_alpha_ratio"}
)

_INTERRUPTED_SPEECH_DASHES = r"\-‐-―−"
_CONTAINER_COVERAGE_MIN = 0.9


def _text_is_interrupted_speech_fragment(text):
    """A single-letter utterance cut off by a dash or ellipsis.

    Interrupted speech ("a pronoun the speaker never finishes") is a real line of
    a balloon even though it carries too few letters for the lexical test. A bare
    letter is deliberately excluded: alone it is ambiguous (a roman numeral, an
    initial, a decorative glyph), so it must not be promoted into speech.
    """
    stripped = clean_ocr_text(text)
    return bool(
        re.fullmatch(
            rf"[A-Za-zÀ-ÿ][{_INTERRUPTED_SPEECH_DASHES}.…]{{1,4}}[.!?]?",
            stripped,
        )
    )


def _glyph_count(text):
    return len(re.sub(r"\s", "", clean_ocr_text(text)))


def _box_fits_glyphs(box, text):
    """True when a box is dimensionally plausible for the glyphs it is said to hold.

    A glyph is at most about one line-height wide, so a box far wider than the
    recognised glyph count is holding characters the engine never returned.
    """
    _, _, width, height = box
    if width <= 0 or height <= 0:
        return False
    glyphs = _glyph_count(text)
    if glyphs <= 0:
        return False
    return width <= height * 1.15 * glyphs


def _line_box_matches_text_density(line):
    """True when the box is dimensionally plausible for the recognised glyphs.

    A box far wider than the text the engine returned means only part of the line
    was recognised. Merging that partial text would corrupt the speech, so such a
    line is never reclaimed; the selective re-OCR pass reprocesses it instead.
    """
    return _box_fits_glyphs(line.box, line.text)


_UNDERREAD_MIN_CONFIDENCE = 0.8
_REOCR_MIN_CONFIDENCE = 0.75
_GAP_MIN_COMPONENTS = 3


def _container_text_gap_boxes(image_bgr, lines):
    """Text-like clusters inside a container that no recognised line covers.

    Bounded to containers that already hold text: inside each one, dark glyph-like
    components are collected, everything the existing lines already cover is
    removed, and what is left is grouped into reading rows. A row survives only if
    it holds several glyph-sized components at a text height comparable to the
    lines already read there, so a balloon border, a tail, art bleeding in or a
    speck of noise never becomes a candidate. The page as a whole is never
    scanned; only containers with text are examined.
    """
    if image_bgr is None or image_bgr.size == 0 or not lines:
        return []
    regions = {}
    for line in lines:
        region_id = int((line.metadata or {}).get("visual_white_region_id") or 0)
        if region_id:
            regions.setdefault(region_id, []).append(line)
    if not regions:
        return []

    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    gaps = []
    for region_id, region_lines in regions.items():
        heights = [line.box[3] for line in region_lines if line.box[3] > 0]
        if not heights:
            continue
        text_height = float(np.median(heights))
        # Container extent: the block the known lines occupy, opened up enough to
        # reach a row they may have missed, but never the whole page.
        xs = [line.box[0] for line in region_lines]
        ys = [line.box[1] for line in region_lines]
        xe = [line.box[0] + line.box[2] for line in region_lines]
        ye = [line.box[1] + line.box[3] for line in region_lines]
        pad_x = int(text_height * 1.2)
        pad_y = int(text_height * 1.6)
        x1 = max(0, int(min(xs)) - pad_x)
        y1 = max(0, int(min(ys)) - pad_y)
        x2 = min(width, int(max(xe)) + pad_x)
        y2 = min(height, int(max(ye)) + pad_y)
        if x2 - x1 < 8 or y2 - y1 < 8:
            continue
        crop = gray[y1:y2, x1:x2]
        # Glyphs are dark on the light inside of a container.
        dark = np.where(crop <= 140, 255, 0).astype(np.uint8)
        # Drop what any recognised line already covers. Every line counts, not
        # only this container's: the crop reaches neighbouring text, and leaving
        # it in would rediscover lines that were read perfectly well.
        for line in lines:
            lx, ly, lw, lh = line.box
            margin = max(3, int(lh * 0.25))
            cx1 = max(0, int(lx) - x1 - margin)
            cy1 = max(0, int(ly) - y1 - margin)
            cx2 = min(crop.shape[1], int(lx + lw) - x1 + margin)
            cy2 = min(crop.shape[0], int(ly + lh) - y1 + margin)
            if cx2 > cx1 and cy2 > cy1:
                dark[cy1:cy2, cx1:cx2] = 0
        count, _, stats, _ = cv2.connectedComponentsWithStats(dark, connectivity=8)
        glyphs = []
        for label in range(1, count):
            cx = int(stats[label, cv2.CC_STAT_LEFT])
            cy = int(stats[label, cv2.CC_STAT_TOP])
            cw = int(stats[label, cv2.CC_STAT_WIDTH])
            ch = int(stats[label, cv2.CC_STAT_HEIGHT])
            area = int(stats[label, cv2.CC_STAT_AREA])
            if ch < text_height * 0.25 or ch > text_height * 1.6:
                continue  # not a glyph at this container's text size
            if cw > text_height * 2.0 or area < max(12, text_height * 0.8):
                continue  # a border stroke, a tail, or a speck
            if cx <= 1 or cy <= 1 or cx + cw >= crop.shape[1] - 1 or cy + ch >= crop.shape[0] - 1:
                continue  # touches the crop edge: art or balloon outline
            glyphs.append((cx, cy, cw, ch))
        if len(glyphs) < _GAP_MIN_COMPONENTS:
            continue
        # Group the leftover glyphs into reading rows.
        for row in _cluster_glyphs_into_rows(glyphs, text_height):
            if len(row) < _GAP_MIN_COMPONENTS:
                continue
            rx1 = min(g[0] for g in row)
            ry1 = min(g[1] for g in row)
            rx2 = max(g[0] + g[2] for g in row)
            ry2 = max(g[1] + g[3] for g in row)
            margin = int(text_height * 0.4)
            gaps.append(
                (
                    int(region_id),
                    (
                        max(0, x1 + rx1 - margin),
                        max(0, y1 + ry1 - margin),
                        min(width, x1 + rx2 + margin) - max(0, x1 + rx1 - margin),
                        min(height, y1 + ry2 + margin) - max(0, y1 + ry1 - margin),
                    ),
                )
            )
    return gaps


_REOCR_RECOVERY_MIN_CONFIDENCE = 0.85


def _recovered_text_is_acceptable(text, confidence):
    """A newly recovered line must be confident and read like real words.

    Adding text that was never read is the riskiest thing this pass can do, so the
    bar is higher than for a re-read: the engine must be sure, and the result must
    contain a real word rather than a smear of glyph-shaped noise.
    """
    if confidence < _REOCR_RECOVERY_MIN_CONFIDENCE:
        return False
    return _text_has_lexical_word(text)


_REOCR_ENGINES = ("paddle", "rapidocr")


def _reocr_engines_for(ocr_lang):
    """Every recogniser available, so the crop gets a genuine second opinion.

    The engine that produced the incomplete reading is the one that failed, and
    which engine that was depends on how the run was configured, so no assumption
    is made: each recogniser is asked and the candidates are scored against each
    other.
    """
    engines = []
    for name in _REOCR_ENGINES:
        try:
            engines.append(OCREngine(ocr_lang, engine=name, fallback_engine=name))
        except Exception:  # noqa: BLE001 - a missing recogniser is not fatal.
            continue
    return engines


def _reocr_crop_candidates(engines, crop, page_index=None):
    """Read a crop with every recogniser; return (engine, text, confidence, lines)."""
    candidates = []
    for engine in engines:
        try:
            crop_lines = engine.detect_lines(crop, page=page_index)
        except Exception:  # noqa: BLE001 - never break the page for one crop.
            continue
        if not crop_lines:
            continue
        text = clean_ocr_text(" ".join(item.text for item in crop_lines))
        confidence = min(float(item.confidence or 0.0) for item in crop_lines)
        candidates.append((engine.engine, text, confidence, crop_lines))
    return candidates


def apply_speech_container_reocr(
    original_bgr,
    raw_lines,
    ocr_lang,
    page_index=None,
):
    """Second, selective OCR pass over speech containers with coverage gaps.

    Two things make a container's coverage incomplete, and each is reprocessed on
    its own pixels with the alternate engine:

    * a confidently recognised line whose box is far too wide for the glyphs it
      returned, meaning the rest of the line was never read;
    * a run of glyph-sized components inside a container that no line covers,
      meaning a whole row of text was never detected.

    Nothing is inferred: a replacement is accepted only when it explains the box
    the old reading could not, and a recovered row only when the engine is sure and
    returns real words. Otherwise the previous reading is kept untouched and the
    gap is reported, so the region can be flagged rather than silently completed.
    Returns the (possibly extended) lines and a record of every decision.
    """
    lines = list(raw_lines or [])
    records = []
    if original_bgr is None or original_bgr.size == 0 or not lines:
        return lines, records
    if not config.OCR_REGION_SELECTIVE_FALLBACK:
        return lines, records

    underread = [line for line in lines if _line_needs_underread_reocr(line)]
    gaps = _container_text_gap_boxes(original_bgr, lines)
    if not underread and not gaps:
        return lines, records

    engines = _reocr_engines_for(ocr_lang)
    if not engines:
        return lines, records
    height, width = original_bgr.shape[:2]

    engine_names = [engine.engine for engine in engines]

    for line in underread:
        x, y, w, h = line.box
        pad = max(4, int(h * 0.35))
        x1, y1 = max(0, x - pad), max(0, y - pad)
        x2, y2 = min(width, x + w + pad), min(height, y + h + pad)
        crop = original_bgr[y1:y2, x1:x2]
        started = time.perf_counter()
        record = {
            "trigger": "speech_line_underread",
            "page": page_index,
            "container_id": int(
                (line.metadata or {}).get("visual_white_region_id") or 0
            ),
            "box": [int(v) for v in line.box],
            "crop_box": [int(x1), int(y1), int(x2 - x1), int(y2 - y1)],
            "previous_engine": line.engine,
            "previous_text": line.text,
            "previous_confidence": round(float(line.confidence or 0.0), 4),
            "engines_attempted": list(engine_names),
            "candidates": [],
            "accepted": False,
            "reason": "selective_reocr_no_confident_candidate",
        }
        best = None
        if crop.size:
            for engine_name, _, _, crop_lines in _reocr_crop_candidates(
                engines, crop, page_index
            ):
                # The crop holds one line, so read the detection that lands on the
                # original box. Anything the padding picked up along the way (a
                # balloon border, a neighbouring glyph) is not this line.
                for item in crop_lines:
                    ix, iy, iw, ih = item.box
                    overlap = _box_iou(
                        (int(x1 + ix), int(y1 + iy), int(iw), int(ih)),
                        tuple(int(v) for v in line.box),
                    )
                    if overlap < 0.25:
                        continue
                    text = clean_ocr_text(item.text)
                    confidence = float(item.confidence or 0.0)
                    record["candidates"].append(
                        {
                            "engine": engine_name,
                            "text": text,
                            "confidence": round(confidence, 4),
                            "overlap": round(overlap, 3),
                        }
                    )
                    if not _underread_candidate_is_better(
                        line.text, text, confidence, line.box
                    ):
                        continue
                    # Prefer the reading that explains the box with the most glyphs
                    # it is confident about, not merely the longest or the boldest.
                    score = _glyph_count(text) + confidence
                    if best is None or score > best[0]:
                        best = (score, engine_name, text, confidence)
        if best:
            _, engine_name, text, confidence = best
            record["accepted"] = True
            record["reason"] = "selective_reocr_recovered_text"
            record["selected_engine"] = engine_name
            record["selected_confidence"] = round(float(confidence), 4)
            record["new_text"] = text
            line.original_text = line.original_text or line.raw_text or line.text
            line.text = text
            line.raw_text = text
            line.confidence = confidence
            line.engine = f"{engine_name}+reocr"
        record["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        records.append(record)

    for container_id, box in gaps:
        x, y, w, h = box
        crop = original_bgr[y:y + h, x:x + w]
        started = time.perf_counter()
        record = {
            "trigger": "speech_container_uncovered_text",
            "page": page_index,
            "container_id": int(container_id),
            "box": [int(v) for v in box],
            "crop_box": [int(x), int(y), int(w), int(h)],
            "previous_text": "",
            "engines_attempted": list(engine_names),
            "candidates": [],
            "accepted": False,
            "reason": "selective_reocr_no_confident_candidate",
        }
        if not crop.size:
            record["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
            records.append(record)
            continue
        best = None
        for engine_name, text, confidence, crop_lines in _reocr_crop_candidates(
            engines, crop, page_index
        ):
            record["candidates"].append(
                {"engine": engine_name, "text": text, "confidence": round(confidence, 4)}
            )
            if not _recovered_text_is_acceptable(text, confidence):
                continue
            score = _glyph_count(text) + confidence
            if best is None or score > best[0]:
                best = (score, engine_name, crop_lines)
        if best:
            _, engine_name, crop_lines = best
            for item in crop_lines:
                text = clean_ocr_text(item.text)
                confidence = float(item.confidence or 0.0)
                if not _recovered_text_is_acceptable(text, confidence):
                    continue
                ix, iy, iw, ih = item.box
                recovered_box = (int(x + ix), int(y + iy), int(iw), int(ih))
                # A crop cut around leftover components can still reach text that
                # was read perfectly well. A reading that lands on a line we
                # already have is a rediscovery, not a recovery, and adding it
                # would duplicate the line.
                if any(
                    _box_iou(recovered_box, tuple(int(v) for v in known.box)) >= 0.2
                    for known in lines
                ):
                    record.setdefault("skipped", []).append(
                        {"text": text, "reason": "overlaps_existing_line"}
                    )
                    continue
                lines.append(
                    OCRLine(
                        text=text,
                        confidence=confidence,
                        polygon=_polygon_from_box(recovered_box),
                        box=recovered_box,
                        raw_text=text,
                        engine=f"{engine_name}+reocr",
                        page=page_index,
                        metadata={
                            "selective_reocr": "speech_container_uncovered_text",
                        },
                    )
                )
                record["accepted"] = True
                record["reason"] = "selective_reocr_recovered_text"
                record["selected_engine"] = engine_name
                record["selected_confidence"] = round(float(confidence), 4)
                record.setdefault("new_lines", []).append(
                    {
                        "text": text,
                        "box": list(recovered_box),
                        "confidence": round(confidence, 4),
                    }
                )
        record["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        records.append(record)

    return lines, records


def summarize_speech_container_reocr(records):
    """Roll the per-container decisions up into auditable counters.

    The mechanism was already deciding correctly in production, but nothing it
    decided reached an artifact, so a recovered line could not be traced back to the
    container, engine and score that recovered it.
    """
    records = list(records or [])
    triggers = {}
    for record in records:
        trigger = str(record.get("trigger") or "unknown")
        triggers[trigger] = triggers.get(trigger, 0) + 1
    accepted = [record for record in records if record.get("accepted")]
    return {
        "containers_evaluated": len(records),
        "triggers": triggers,
        "accepted": len(accepted),
        "rejected": len(records) - len(accepted),
        "underread_recovered": sum(
            record.get("trigger") == "speech_line_underread" for record in accepted
        ),
        "uncovered_text_recovered": sum(
            record.get("trigger") == "speech_container_uncovered_text"
            for record in accepted
        ),
        "total_duration_ms": round(
            sum(float(record.get("duration_ms") or 0.0) for record in records),
            3,
        ),
    }


def _polygon_from_box(box):
    x, y, w, h = box
    return np.array(
        [[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
        dtype=np.int32,
    )


def _cluster_glyphs_into_rows(glyphs, text_height):
    """Group glyph boxes that share a reading row."""
    rows = []
    for glyph in sorted(glyphs, key=lambda g: (g[1], g[0])):
        cy_center = glyph[1] + glyph[3] / 2.0
        placed = False
        for row in rows:
            ref = row[0]
            ref_center = ref[1] + ref[3] / 2.0
            if abs(cy_center - ref_center) <= text_height * 0.6:
                row.append(glyph)
                placed = True
                break
        if not placed:
            rows.append([glyph])
    return rows


def _line_needs_underread_reocr(line):
    """A confidently recognised line whose box still holds unread characters.

    The engine was sure of the glyphs it returned, so this is not noise, yet the
    box is far too wide for them: the rest of the line was never read. Such a line
    is worth reprocessing on its own pixels. A low-confidence read is left alone,
    since it is more likely a phantom picked up from art or a balloon border.
    """
    try:
        confidence = float(line.confidence or 0.0)
    except (TypeError, ValueError):
        return False
    if confidence < _UNDERREAD_MIN_CONFIDENCE:
        return False
    if not re.search(r"[A-Za-zÀ-ÿ]", clean_ocr_text(line.text or "")):
        return False
    return not _box_fits_glyphs(line.box, line.text)


def _underread_candidate_is_better(previous_text, candidate_text, candidate_confidence, box):
    """Accept a re-read only when it explains the box the old reading could not.

    The replacement must be confident, must return more glyphs than before, and
    those glyphs must make the box plausible. A longer but still implausible read,
    or a confident read that does not add characters, is rejected, so a valid
    reading is never replaced by a worse one.
    """
    if candidate_confidence < _REOCR_MIN_CONFIDENCE:
        return False
    if not re.search(r"[A-Za-zÀ-ÿ]", clean_ocr_text(candidate_text or "")):
        return False
    if _glyph_count(candidate_text) <= _glyph_count(previous_text):
        return False
    return _box_fits_glyphs(box, candidate_text)


def _text_has_lexical_word(text):
    """A word of >=2 letters containing a vowel (generic, language-agnostic).

    Excludes OCR noise and consonant-cluster onomatopoeia so a phantom read is
    never promoted into a translated line.
    """
    for word in re.findall(r"[A-Za-zÀ-ÿ]{2,}", str(text or "")):
        if re.search(r"[aeiouyAEIOUYÀ-ÿ]", word):
            return True
    return False


def _short_line_joins_group(line, group):
    """Symmetric structural test: does a filtered short line belong to a group.

    Uses only generic geometry (comparable text height, strong horizontal
    overlap, a small vertical gap in either direction). A confirmed *enclosed*
    visual container with a different id vetoes the join; a weak, non-enclosed
    region assignment does not, since it over-fragments a single balloon.
    """
    line_region_id = int((line.metadata or {}).get("visual_white_region_id") or 0)
    line_enclosed = bool((line.metadata or {}).get("visual_white_region_enclosed"))
    group_region_ids = {
        int((item.metadata or {}).get("visual_white_region_id") or 0)
        for item in group.lines
    }
    group_region_ids.discard(0)
    if (
        line_region_id
        and group_region_ids
        and line_region_id not in group_region_ids
        and line_enclosed
    ):
        return False
    gx, gy, gw, gh = group.box
    lx, ly, lw, lh = line.box
    if lw <= 0 or lh <= 0 or gw <= 0 or gh <= 0:
        return False
    heights = [item.box[3] for item in group.lines if item.box[3] > 0]
    base_height = float(np.median(heights)) if heights else max(1, gh)
    if min(lh, base_height) / max(lh, base_height) < 0.5:
        return False
    horizontal_overlap = max(0, min(gx + gw, lx + lw) - max(gx, lx))
    if horizontal_overlap / max(1, min(gw, lw)) < 0.35:
        return False
    vertical_gap = max(gy - (ly + lh), ly - (gy + gh))
    return vertical_gap <= 0.8 * base_height


def _group_has_enclosed_container(group):
    """True when a group sits in a confirmed enclosed visual container.

    Distinguishes real dialogue balloons from stylized vocalizations/SFX drawn
    on open art, which are not enclosed. Reclaiming short lines only into
    confirmed containers keeps screams and art onomatopoeia from being merged.

    A closed contour is the strongest signal, but a balloon drawn on a light page
    can merge with the background so its region is never marked enclosed even
    though the text sits almost entirely inside it. Such a region counts as a
    container when it covers nearly all of the line; stylized shouts lettered on
    open art keep only partial coverage and stay out.
    """
    for line in group.lines:
        metadata = line.metadata or {}
        if metadata.get("visual_white_region_enclosed"):
            return True
        try:
            coverage = float(metadata.get("visual_white_region_coverage") or 0.0)
        except (TypeError, ValueError):
            coverage = 0.0
        if coverage >= _CONTAINER_COVERAGE_MIN:
            return True
    return False


def _group_accepts_ignored_cleanup_attachment(group):
    """Whether ignored child-line geometry may be cleaned by this story group.

    Cleanup ownership is stricter than translation ownership but broader than a
    closed-contour balloon: real narration boxes can be visually open/merged with
    the page background while still being the obvious owner of a short corrupted
    child line.  The child text is not merged into provider input here; only its
    source glyph geometry joins the cleanup mask.
    """

    if _group_has_enclosed_container(group):
        return True
    background_type = str(getattr(group, "background_type", "") or "")
    metrics = getattr(group, "background_metrics", None) or {}
    if background_type == "narration_box" and (
        metrics.get("open_white_narration")
        or metrics.get("open_dark_narration")
        or metrics.get("dominant_white_enclosure")
        or metrics.get("stylized_white_enclosure")
    ):
        return True
    return bool(getattr(group, "inside_narration_box_like_region", False))


def _shared_open_visual_region_cleanup_owner(line, group):
    """True when an ignored child physically belongs to the same open text region.

    This path deliberately attaches geometry only.  It does not merge the child
    OCR text into the provider input, and it is gated by story translation
    authority so stylized SFX/open art lettering is not erased just because it
    happens to share a weak white-region id.
    """

    line_metadata = getattr(line, "metadata", None) or {}
    line_region = int(line_metadata.get("visual_white_region_id") or 0)
    if not line_region or line_metadata.get("visual_white_region_enclosed"):
        return False
    try:
        line_coverage = float(line_metadata.get("visual_white_region_coverage") or 0.0)
    except (TypeError, ValueError):
        line_coverage = 0.0
    if line_coverage < 0.65:
        return False
    group_regions = []
    group_coverages = []
    for item in getattr(group, "lines", None) or []:
        metadata = getattr(item, "metadata", None) or {}
        if metadata.get("visual_white_region_enclosed"):
            continue
        region = int(metadata.get("visual_white_region_id") or 0)
        if not region:
            continue
        group_regions.append(region)
        try:
            group_coverages.append(float(metadata.get("visual_white_region_coverage") or 0.0))
        except (TypeError, ValueError):
            group_coverages.append(0.0)
    if line_region not in group_regions:
        return False
    if max(group_coverages or [0.0]) < 0.45:
        return False
    return _group_has_story_translation_authority(group)


def _reclaim_short_lexical_lines(groups, candidates, image_shape):
    """Reclaim short speech lines filtered as noise into their speech group.

    A short line dropped by a length/shape noise filter can be a real part of a
    balloon: a leading word, a short interjection, or an utterance the speaker
    cuts off. When it reads as speech and structurally belongs to an existing
    text group, merge it before classification so it is translated and rendered
    with the rest of the speech instead of surviving as visible source text.
    A line whose box is far wider than the glyphs the engine returned is only
    partially recognised and is never merged, since its text would corrupt the
    speech. Purely structural: no word, page, coordinate or chapter rule.
    """
    for candidate in candidates:
        if not candidate.ignored:
            continue
        if candidate.ignore_reason not in _RECLAIMABLE_IGNORE_REASONS:
            continue
        line = candidate.line
        if not (
            _text_has_lexical_word(line.text)
            or _text_is_interrupted_speech_fragment(line.text)
        ):
            continue
        if not _line_box_matches_text_density(line):
            continue
        target = None
        for group in groups:
            if group.ignored or not _text_has_lexical_word(group.text):
                continue
            if not _group_has_enclosed_container(group):
                continue
            if _short_line_joins_group(line, group):
                target = group
                break
        if target is None:
            continue
        if any(id(existing) == id(line) for existing in target.lines):
            continue
        target.lines.append(line)
        target.lines = reading_order(target.lines)
        target.text = clean_ocr_text(" ".join(item.text for item in target.lines))
        candidate.ignored = False
        candidate.ignore_reason = ""


def _associate_ignored_cleanup_lines(groups, candidates, image_shape, page_index=None):
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
            record_count("associate_ignored.group_checks", 1, page_index=page_index)
            with profile_step("associate_ignored.safe_draw_box", page_index=page_index):
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
            continue
        # A real #65 residual can be a high-confidence but text-corrupted OCR line
        # ("IT'LL" read back as punctuation/digits) directly attached to a translated
        # story box.  Its text is intentionally not reclaimed into the provider input,
        # but its source glyph geometry still belongs to the story render unit and
        # must be cleaned with that unit.  Use the same structural join as short-line
        # reclamation, guarded by story authority and an enclosed/near-total container,
        # so open-art SFX, credits and unrelated fragments stay untouched.
        if (
            not possible
            and candidate.ignore_reason in _RECLAIMABLE_IGNORE_REASONS
            and not _text_has_lexical_word(line.text)
        ):
            for group in groups:
                record_count("associate_ignored.story_block_checks", 1, page_index=page_index)
                if group.ignored or not _text_has_lexical_word(group.text):
                    continue
                if not (
                    _group_accepts_ignored_cleanup_attachment(group)
                    or _shared_open_visual_region_cleanup_owner(line, group)
                ):
                    continue
                if str(getattr(group, "classification", "") or "") in {
                    "decorative",
                    "logo",
                    "sfx",
                }:
                    continue
                if not _group_has_story_translation_authority(group):
                    continue
                if _short_line_joins_group(line, group):
                    possible.append((0.0, group))
                    break
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


_SHRINK_PENALTY_MAX = 0.75
_SHRINK_PENALTY_FREE_RETENTION = 0.95
_SHRINK_PENALTY_FULL_RETENTION = 0.55


def _content_shrink_penalty(
    content_retention,
    context_token_count,
    cross_region_suspected=False,
):
    """Penalise a fallback OCR candidate in proportion to the content it drops.

    A hard threshold let a candidate that discarded a whole line of speech pass
    free whenever it landed just above the cut-off, so the truncated reading
    could win and the missing text never reached translation. Scale the penalty
    with how much content is lost instead: a candidate must be clearly better to
    justify dropping source text, while a small trim backed by a clean read (for
    example shedding a leading sound effect) is only lightly penalised.

    When the original reading is itself suspected of merging text across regions,
    dropping content is the intended correction, so no penalty applies.
    """
    if cross_region_suspected:
        return 0.0
    if context_token_count < 2:
        return 0.0
    if content_retention >= _SHRINK_PENALTY_FREE_RETENTION:
        return 0.0
    span = _SHRINK_PENALTY_FREE_RETENTION - _SHRINK_PENALTY_FULL_RETENTION
    lost = _SHRINK_PENALTY_FREE_RETENTION - content_retention
    return _SHRINK_PENALTY_MAX * min(1.0, lost / span)


def _cross_region_resolution_bonus(original_group, candidate_group, content_retention):
    """Prefer a clean regional reading over agreement with a crossed region."""
    if "possible_cross_region_group" not in original_group.quality_reasons:
        return 0.0
    if candidate_group is None or (
        "possible_cross_region_group" in candidate_group.quality_reasons
    ):
        return 0.0
    if _candidate_quality(candidate_group) < 0.82 or content_retention < 0.70:
        return 0.0
    return 0.32


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


def _box_inside(inner, outer):
    """True when ``inner`` lies within ``outer``, allowing for re-read jitter.

    A region re-read at the recogniser's preferred scale returns boxes a couple
    of pixels off the ones the page scale produced, so exact containment would
    reject a line the region plainly owns.  The tolerance is a fraction of the
    inner box's own height - always less than one text line - so it can absorb
    that jitter and still never reach into a neighbouring container.
    """
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    slack = max(2.0, ih * 0.25)
    return (
        ix >= ox - slack
        and iy >= oy - slack
        and ix + iw <= ox + ow + slack
        and iy + ih <= oy + oh + slack
    )


PORTUGUESE_ACCENTED_FOLD_TOKENS = {"SO"}
PORTUGUESE_FOR_PREVIOUS_CONTEXT = {"COMO", "ONDE", "QUANDO", "QUE", "QUEM", "SE"}
PORTUGUESE_FOR_CONTEXT_BRIDGE_TOKENS = {
    "AINDA",
    "ASSIM",
    "DEPOIS",
    "DISSO",
    "ELE",
    "ELA",
    "EU",
    "ISSO",
    "ISTO",
    "MAIS",
    "MESMO",
    "NAO",
    "O",
    "TAMBEM",
    "VOCE",
}
PORTUGUESE_FOR_LEFT_CONTEXT_WINDOW = 4
HIGH_CONFIDENCE_RESIDUAL_SPANISH_MARKERS = {
    "AHORA",
    "AUNQUE",
    "ENTONCES",
    "GRACIAS",
    "HOLA",
    "MUY",
    "PERO",
    "QUIZA",
    "QUIZAS",
    "USTED",
    "USTEDES",
}
HIGH_CONFIDENCE_RESIDUAL_SPANISH_FORMS = {"QUÉ"}


def _has_diacritic(text):
    return any(unicodedata.combining(char) for char in unicodedata.normalize("NFKD", text))


def _translation_token_infos(text):
    value = str(text or "")
    quoted_spans = [
        match.span()
        for match in re.finditer(
            r'"[^"\r\n]*"|“[^”\r\n]*”|«[^»\r\n]*»',
            value,
        )
    ]
    infos = []
    for match in re.finditer(
        r"[A-Za-zÀ-ÖØ-öø-ÿ\u0300-\u036f']+",
        value,
    ):
        raw = match.group(0)
        token = re.sub(r"[^A-Z']", "", _ascii_fold(raw).upper())
        if not token:
            continue
        infos.append(
            {
                "raw": raw,
                "token": token,
                "start": match.start(),
                "end": match.end(),
                "left_hyphen": match.start() > 0 and value[match.start() - 1] == "-",
                "right_hyphen": match.end() < len(value) and value[match.end()] == "-",
                "has_diacritic": _has_diacritic(raw),
                "normalized": unicodedata.normalize("NFC", raw).upper(),
                "quoted": any(
                    start <= match.start() and match.end() <= end
                    for start, end in quoted_spans
                ),
            }
        )
    return infos


def _is_accented_portuguese_fold_token(info):
    return info["has_diacritic"] and info["token"] in PORTUGUESE_ACCENTED_FOLD_TOKENS


def _is_portuguese_for_context_bridge_token(token):
    return (
        token in PORTUGUESE_MARKERS
        or token in PORTUGUESE_FOR_CONTEXT_BRIDGE_TOKENS
    )


def _has_portuguese_for_left_context(tokens, index):
    start = max(0, index - PORTUGUESE_FOR_LEFT_CONTEXT_WINDOW)
    for anchor_index in range(index - 1, start - 1, -1):
        anchor = tokens[anchor_index]
        if anchor in PORTUGUESE_FOR_PREVIOUS_CONTEXT:
            return all(
                _is_portuguese_for_context_bridge_token(token)
                for token in tokens[anchor_index + 1:index]
            )
        if not _is_portuguese_for_context_bridge_token(anchor):
            break
    return False


def _is_portuguese_folded_token(token_infos, index):
    info = token_infos[index]
    token = info["token"]
    if _is_accented_portuguese_fold_token(info):
        return True
    if token != "FOR":
        return False

    tokens = [item["token"] for item in token_infos]
    next_token = tokens[index + 1] if index + 1 < len(tokens) else ""
    next_next_token = tokens[index + 2] if index + 2 < len(tokens) else ""

    if _has_portuguese_for_left_context(tokens, index):
        return True
    if next_token == "O" and next_next_token == "QUE":
        return True
    return False


def _is_english_residual_candidate(token_infos, index, vocabulary):
    token = token_infos[index]["token"]
    if token not in vocabulary:
        return False
    if _is_token_inside_hyphenated_entity_fragment(token_infos, index):
        return False
    return not _is_portuguese_folded_token(token_infos, index)


def _is_token_inside_hyphenated_entity_fragment(token_infos, index):
    info = token_infos[index]
    token = info["token"]
    if not token:
        return False
    if len(token) > 2:
        return False
    previous_info = token_infos[index - 1] if index > 0 and info.get("left_hyphen") else None
    next_info = (
        token_infos[index + 1]
        if index + 1 < len(token_infos) and info.get("right_hyphen")
        else None
    )
    return any(_hyphenated_entity_neighbour(item) for item in (previous_info, next_info))


def _hyphenated_entity_neighbour(info):
    if not info:
        return False
    token = info["token"]
    return bool(
        token
        and len(token) >= 3
        and token not in RESIDUAL_TRANSLATION_ENGLISH_WORDS
        and not _looks_like_inflected_english_token(token)
        and not _is_nonlexical_vocalization_token({token})
    )


_HORIZONTAL_WHITESPACE_PATTERN = (
    r"[\t \u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]*"
)


def _leading_hyphenated_fragment(text):
    match = re.match(
        r"^\s*([A-Za-zÀ-ÖØ-öø-ÿ\u0300-\u036f]{1,4})"
        rf"{_HORIZONTAL_WHITESPACE_PATTERN}"
        rf"-{_HORIZONTAL_WHITESPACE_PATTERN}"
        r"([A-Za-zÀ-ÖØ-öø-ÿ\u0300-\u036f']+)",
        str(text or ""),
    )
    if not match:
        return None
    return tuple(
        re.sub(r"[^A-Z']", "", _ascii_fold(part).upper())
        for part in match.groups()
    )


def _residual_source_hyphen_fragment(source_text, translation, allowed_names):
    source = _leading_hyphenated_fragment(source_text)
    translated = _leading_hyphenated_fragment(translation)
    if not source or not translated:
        return ""

    source_prefix, source_word = source
    translated_prefix, translated_word = translated
    if (
        source_prefix != translated_prefix
        or source_word == translated_word
        or not source_word.startswith(source_prefix)
    ):
        return ""
    if {
        source_prefix,
        source_word,
        translated_word,
    } & set(allowed_names or []):
        return ""
    # A kept stutter letter is speech disfluency, not language. Once the word after
    # the hyphen has actually been translated, that letter carries no source-language
    # meaning, and demanding it be re-derived from the translated word ("G-SAIA"
    # rejected for not being "S-SAIA") sent correct lines to review over one glyph.
    # A body that was NOT translated is a real residual, and the English-token checks
    # below catch it whether or not a stutter precedes it. Only a known source word
    # surviving the hyphen is reported here.
    if source_word not in RESIDUAL_TRANSLATION_ENGLISH_WORDS:
        return ""
    return source_prefix


def _normalized_allowed_name_tokens(allowed_proper_names):
    allowed = set()
    for name in allowed_proper_names or []:
        folded = _ascii_fold(name).upper()
        compact = re.sub(r"[^A-Z]", "", folded)
        if compact:
            allowed.add(compact)
        allowed.update(re.findall(r"[A-Z]+", folded))
    return allowed


def _source_token_is_name_like(info):
    """Mixed case is OCR noise, not evidence of a name, so this proves nothing.

    Comic lettering is all-caps: an internally alternating token like an OCR misread
    of a common word is the recogniser stumbling on glyphs, not a character name.
    Treating that casing as name evidence let an ordinary word survive untranslated
    as a "name-like" echo. Real names are established from detected spans and chapter
    consensus (which reach the validator through ``allowed_names``) and, for a lone
    token, from the model refusing to translate it - never from case alone.
    """
    return False


def _is_stuttered_name_fragment(text):
    return bool(
        re.fullmatch(
            r"\s*[A-Za-z]\s*-\s*[A-Za-z]{3,}\s*[.!?…]*\s*",
            str(text or ""),
        )
    )


def _is_nonlexical_vocalization_token(source_tokens):
    if len(source_tokens) != 1:
        return False
    token = next(iter(source_tokens))
    if not 3 <= len(token) <= 4:
        return False
    if token in RESIDUAL_TRANSLATION_ENGLISH_WORDS or _looks_like_inflected_english_token(
        token
    ):
        return False
    return bool(
        re.fullmatch(r"[AEIOU][A-Z]{1,3}H|H[AEIOU]H|[A-Z]*([AEIOU])\1+[A-Z]*", token)
    )


OCR_JOINED_NAME_FALSE_POSITIVE_PARTS = frozenset(
    {
        "A", "ABOUT", "AGAIN", "ALOT", "AS", "BACK", "BARELY", "BE", "BECOMING",
        "BECAREFUL", "BOSS", "BUT", "CONSIDER", "CRAP", "DEVOURED", "DO", "DUNGEON",
        "ELSE", "ENTRANCE", "FEEL", "GET", "GOING", "HAVE", "HERE", "HOLY", "I",
        "INTO", "IS", "IT", "JUST", "KNOW", "LATE", "LIKE", "LOOKS", "LUCKY",
        "LL", "M", "MADE", "MATTER", "MONSTER", "MORE", "NEAR", "NEXT", "NO", "OF", "OUT",
        "PLEASE", "REASON", "RIGHT", "SEEN", "SERIOUSLY", "SO", "SOMEONE",
        "SPECIAL", "STILL", "STOP", "TALKING", "THAN", "THAT", "THE", "THERE",
        "TIME", "TO", "TURNED", "UNFORTUNATE", "US", "VE", "WAY", "WERE", "YOUR",
        "YOURSELVES",
    }
)


def _can_segment_ocr_joined_source_words(token):
    token = re.sub(r"[^A-Z]", "", str(token or "").upper())
    if len(token) < 7:
        return False
    if token in OCR_LEXICAL_REFERENCE_WORDS:
        return False
    parts = OCR_JOINED_NAME_FALSE_POSITIVE_PARTS | OCR_LEXICAL_REFERENCE_WORDS
    reachable = [False] * (len(token) + 1)
    reachable[0] = True
    for start in range(len(token)):
        if not reachable[start]:
            continue
        for end in range(start + 1, len(token) + 1):
            piece = token[start:end]
            if len(piece) >= 1 and piece in parts:
                reachable[end] = True
    return reachable[-1]


def _looks_like_source_contraction_token(token):
    token = str(token or "").upper().strip()
    if "'" not in token:
        return False
    left, right = token.split("'", 1)
    right = right.strip("'")
    if right not in {"D", "LL", "M", "RE", "S", "T", "VE"}:
        return False
    if left in ENGLISH_FUNCTION_TOKENS or left in OCR_LEXICAL_REFERENCE_WORDS:
        return True
    return _can_segment_ocr_joined_source_words(left)


def _looks_like_title_name_ocr_compound(token):
    token = re.sub(r"[^A-Z]", "", str(token or "").upper())
    for title in sorted(NAME_TITLE_TOKENS | {"HUNTER"}, key=len, reverse=True):
        if not token.startswith(title) or len(token) <= len(title) + 2:
            continue
        remainder = token[len(title):]
        if remainder and not _token_is_source_vocabulary(remainder):
            return True
    return False


def _required_name_span_is_hard_authority(span):
    token = _name_token_of(span)
    if not token:
        return False
    if _looks_like_source_contraction_token(token):
        return False
    if token in ENGLISH_FUNCTION_TOKENS:
        return False
    if _token_is_source_vocabulary(token):
        return False
    if _looks_like_inflected_english_token(token):
        return False
    if _is_nonlexical_vocalization_token({token}):
        return False
    if _can_segment_ocr_joined_source_words(token):
        return False
    if _looks_like_title_name_ocr_compound(token):
        return False
    return True


def validate_translation_text(
    source_text,
    translation,
    classification="speech",
    allowed_proper_names=None,
    required_name_spans=None,
):
    translated = clean_ocr_text(translation)
    if classification == "sfx" and not config.TRANSLATE_SFX:
        return True, "sfx_preserved"
    if not translated:
        return False, "empty_translation"

    source_infos = _translation_token_infos(source_text)
    translated_infos = _translation_token_infos(translated)
    source_tokens = {info["token"] for info in source_infos}
    translated_tokens = [info["token"] for info in translated_infos]
    allowed_names = _normalized_allowed_name_tokens(allowed_proper_names)
    translatable_context_for_names = classification in {
        "speech",
        "thought",
        "narration",
        "unknown",
    }
    declared_name_authority = {
        _name_token_of(span) for span in _declared_name_tokens(source_text)
    }
    altered_names = sorted(
        {
            token
            for span in (required_name_spans or ())
            for token in [_name_token_of(span)]
            if token in source_tokens
            and token not in translated_tokens
            and (
                _required_name_span_is_hard_authority(span)
                or token in declared_name_authority
            )
        }
    )
    if translatable_context_for_names and altered_names:
        # A detected name must survive the translation verbatim. When it does not,
        # the model replaced it with a target-language equivalent it invented.
        return False, "proper_name_altered:" + ",".join(altered_names[:6])
    translatable_context = classification in {"speech", "thought", "narration", "unknown"}
    portuguese_hits = sum(token in PORTUGUESE_MARKERS for token in translated_tokens)
    target_language_signal = portuguese_hits or any(
        info["has_diacritic"] for info in translated_infos
    )
    # A title used as a common noun in the source ("the count's ...") that survived
    # verbatim into an otherwise-translated candidate is a partial translation, not a
    # loanword: reject it so the retry finishes the job. A title bound to a name is
    # not flagged, so a form of address like a title before a character name stays.
    residual_titles = sorted(
        {
            info["token"]
            for info in translated_infos
            if info["token"] in _title_used_as_common_noun_tokens(source_infos)
            and info["token"] not in allowed_names
            and not info["has_diacritic"]
        }
    )
    if translatable_context and residual_titles and target_language_signal:
        return (
            False,
            "residual_source_language_title:" + ",".join(residual_titles[:6]),
        )
    source_has_known_english = any(
        token in RESIDUAL_TRANSLATION_ENGLISH_WORDS
        or _looks_like_inflected_english_token(token)
        for token in source_tokens
    )
    source_has_name_like_token = any(
        _source_token_is_name_like(info) for info in source_infos
    )
    normalized_source = _normalized_translation_text(source_text)
    if (
        translatable_context
        and normalized_source
        and normalized_source == _normalized_translation_text(translated)
        and not (source_tokens and source_tokens.issubset(allowed_names))
        and not target_language_signal
        and not source_has_known_english
        and not source_has_name_like_token
        and not _is_stuttered_name_fragment(source_text)
        and not _is_nonlexical_vocalization_token(source_tokens)
    ):
        if not (
            len(source_tokens) == 1
            and (
                len(next(iter(source_tokens))) <= 2
                or bool(re.fullmatch(r"(.)\1{2,}", next(iter(source_tokens))))
            )
        ):
            return False, "candidate_equals_source"
    forbidden = []
    for index, token in enumerate(translated_tokens):
        if token in {"A", "O", "E", "DO", "DA", "DOS", "DAS"}:
            continue
        if (
            token in COMMON_ENGLISH_WORDS
            and token in source_tokens
            and not _is_token_inside_hyphenated_entity_fragment(translated_infos, index)
            and not _is_portuguese_folded_token(translated_infos, index)
        ):
            forbidden.append(token)

    longest_english_run = 0
    current_run = 0
    for index, token in enumerate(translated_tokens):
        if (
            token in COMMON_ENGLISH_WORDS
            and token not in {"A", "I", "O", "E"}
            and not _is_token_inside_hyphenated_entity_fragment(translated_infos, index)
            and not _is_portuguese_folded_token(translated_infos, index)
        ):
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

    preserved_english_phrase_tokens = [
        token
        for index, token in enumerate(translated_tokens)
        if token in source_tokens
        and token in SOURCE_PRESERVED_ENGLISH_PHRASE_WORDS
        and token not in allowed_names
        and not _is_token_inside_hyphenated_entity_fragment(translated_infos, index)
        and not _is_portuguese_folded_token(translated_infos, index)
    ]
    if (
        translatable_context
        and target_language_signal
        and len(preserved_english_phrase_tokens) >= 3
    ):
        return (
            False,
            "mixed_language_tokens:"
            + ",".join(sorted(set(preserved_english_phrase_tokens))[:6]),
        )

    partial_source_fragment = _residual_source_hyphen_fragment(
        source_text,
        translated,
        allowed_names,
    )
    if translatable_context and partial_source_fragment:
        return (
            False,
            "multilingual_partial_translation:" + partial_source_fragment,
        )

    residual_spanish_tokens = sorted(
        {
            info["token"]
            for info in translated_infos
            if (
                info["token"] in HIGH_CONFIDENCE_RESIDUAL_SPANISH_MARKERS
                or info["normalized"]
                in HIGH_CONFIDENCE_RESIDUAL_SPANISH_FORMS
            )
            and info["token"] not in allowed_names
            and not info["quoted"]
        }
    )
    if translatable_context and residual_spanish_tokens and (
        portuguese_hits or len(residual_spanish_tokens) >= 2
    ):
        return (
            False,
            "residual_spanish_token:" + ",".join(residual_spanish_tokens[:6]),
        )

    residual_english_tokens = [
        token
        for index, token in enumerate(translated_tokens)
        if token not in {"A", "O", "E", "I"}
        and token not in allowed_names
        and token not in PORTUGUESE_MARKERS
        and _is_english_residual_candidate(
            translated_infos,
            index,
            RESIDUAL_TRANSLATION_ENGLISH_WORDS,
        )
    ]
    residual_unique = sorted(set(residual_english_tokens))
    if translatable_context and residual_unique:
        if portuguese_hits or len(translated_tokens) >= 4:
            return (
                False,
                "residual_english_token:" + ",".join(residual_unique[:6]),
            )
        if len(residual_english_tokens) >= 2:
            return (
                False,
                "untranslated_english_text:" + ",".join(residual_unique[:6]),
            )
        if len(translated_tokens) <= 3:
            return (
                False,
                "untranslated_single_english_token:" + residual_unique[0],
            )

    residual_inflected_english = sorted(
        {
            token
            for index, token in enumerate(translated_tokens)
            if token in source_tokens
            and token not in allowed_names
            and token not in PORTUGUESE_MARKERS
            and not _is_token_inside_hyphenated_entity_fragment(translated_infos, index)
            and not _is_portuguese_folded_token(translated_infos, index)
            and _looks_like_inflected_english_token(token)
        }
    )
    if residual_inflected_english and (
        portuguese_hits or len(translated_tokens) >= 4
    ):
        return (
            False,
            "residual_inflected_english:"
            + ",".join(residual_inflected_english[:6]),
        )

    folded_translation = _ascii_fold(translated).upper()
    folded_source = _ascii_fold(source_text).upper()
    if translatable_context and target_language_signal:
        if re.search(r"\bVOCE\s+(FAZER|SER|ESTAR|TER|IR)\b", folded_translation):
            return False, "unnatural_ptbr_verb_mood:voce_infinitive"
        stray_parenthetical = re.search(r"\b([A-Z])\)", folded_translation)
        if stray_parenthetical and stray_parenthetical.group(0) not in folded_source:
            return False, "stray_ocr_fragment:" + stray_parenthetical.group(0)

    source_english_tokens = [
        token
        for token in source_tokens
        if token in COMMON_ENGLISH_WORDS and token not in {"I"}
    ]
    translated_english_tokens = [
        token
        for index, token in enumerate(translated_tokens)
        if token in COMMON_ENGLISH_WORDS
        and token not in {"I"}
        and not _is_token_inside_hyphenated_entity_fragment(translated_infos, index)
        and not _is_portuguese_folded_token(translated_infos, index)
    ]
    if (
        source_english_tokens
        and translated_english_tokens
        and not portuguese_hits
        and len(translated_english_tokens) >= max(1, min(2, len(source_english_tokens)))
    ):
        return False, "untranslated_english_text"

    if (
        len(translated_tokens) == 1
        and translated_tokens[0] in COMMON_ENGLISH_WORDS
        and not _is_portuguese_folded_token(translated_infos, 0)
    ):
        return False, "untranslated_single_english_token"

    repeated_fragment = _translation_repeated_fragment(
        translated_tokens,
        allowed_names,
    )
    if repeated_fragment:
        return False, "repeated_translation_fragment:" + repeated_fragment

    return True, "ok"


def _translation_repeated_fragment(tokens, allowed_names=None):
    """Detect a malformed token followed shortly by a longer near-duplicate."""
    allowed = set(allowed_names or [])
    for index, left in enumerate(tokens):
        if (
            len(left) < 5
            or left in allowed
            or not _translation_fragment_token_looks_malformed(left)
        ):
            continue
        for right in tokens[index + 1 : index + 4]:
            if len(right) < 5 or right in allowed or left == right:
                continue
            shorter, longer = sorted((left, right), key=len)
            prefix = 0
            for lchar, rchar in zip(shorter, longer):
                if lchar != rchar:
                    break
                prefix += 1
            if prefix >= 4 and prefix / len(shorter) >= 0.6:
                return f"{left}->{right}"
    return ""


def _translation_fragment_token_looks_malformed(token):
    """Whether a repeated-fragment candidate looks like OCR/provider damage.

    Prefix similarity alone is too broad for Portuguese: a valid pair such as
    ``PROVOCA``/``PROVAS`` shares a long stem but expresses different story
    semantics.  Keep the retry guard for compact malformed fragments such as
    ``PROVDE`` before ``PROVINCIA`` by requiring an internal consonant join that
    is unusual in ordinary PT-BR morphology.
    """

    value = re.sub(r"[^A-Z]", "", str(token or "").upper())
    if len(value) < 5:
        return False
    if re.search(r"(?:[BCDFGHJKLMNPQRSTVWXYZ]{4,}|VD|TD|DB|PB|KG|GQ|QG|QJ)", value):
        return True
    return False


def _english_inflection_base(token):
    token = str(token or "").upper().strip("'")
    if len(token) < 4 or not re.search(
        r"(?:ING|ED|LY|TION|NESS|MENT|ERS|IES|S)$",
        token,
    ):
        return ""

    candidates = []

    def add(candidate):
        # Some common English verbs have a two-character base.  Keep those
        # candidates and let the fixed lexical reference set decide whether
        # they are actual English bases.
        if len(candidate) >= 2 and candidate not in candidates:
            candidates.append(candidate)

    if token.endswith("IES") and len(token) > 4:
        add(token[:-3] + "Y")
    if token.endswith("IED") and len(token) > 4:
        add(token[:-3] + "Y")
    if token.endswith("ING") and len(token) > 4:
        stem = token[:-3]
        add(stem)
        add(stem + "E")
        if len(stem) >= 2 and stem[-1] == stem[-2]:
            add(stem[:-1])
    if token.endswith("ED") and len(token) > 4:
        stem = token[:-2]
        add(stem)
        add(stem + "E")
        if len(stem) >= 2 and stem[-1] == stem[-2]:
            add(stem[:-1])
    if token.endswith("ERS") and len(token) > 4:
        stem = token[:-3]
        add(token[:-1])
        add(stem)
        if len(stem) >= 2 and stem[-1] == stem[-2]:
            add(stem[:-1])
    for suffix in ("TION", "NESS", "MENT", "LY"):
        if token.endswith(suffix) and len(token) > len(suffix) + 2:
            add(token[: -len(suffix)])
    if token.endswith("ES") and len(token) > 4:
        add(token[:-2])
    if token.endswith("S"):
        add(token[:-1])
    add(token)

    return next(
        (
            candidate
            for candidate in candidates
            if candidate in ENGLISH_INFLECTION_BASE_WORDS
        ),
        "",
    )


def _looks_like_inflected_english_token(token):
    return bool(_english_inflection_base(token))


def _needs_isolated_retry(group, reason):
    """True when a final, fully-translating attempt is justified for a group.

    The strict retry tells the model it may keep proper names, so a source word
    it mistakes for a name survives and the candidate keeps failing validation.
    When the pipeline detected no proper name in this group, nothing justifies
    leaving a source-language word, so one last isolated attempt may demand a
    full translation. Groups with a known proper name are left alone, so the name
    is never translated away.
    """
    if group_proper_name_spans(group):
        return False
    if group.classification not in {"speech", "thought", "narration", "unknown"}:
        return False
    return str(reason or "").startswith(
        (
            "mixed_language_tokens",
            "residual_source_language",
            "residual_spanish_token",
            # A candidate identical to its source is either a word the model failed
            # to translate or a name it correctly refused to. Asking once more, with
            # names forbidden, is what separates the two.
            "candidate_equals_source",
        )
    )


def _selective_translation_retry_budget(groups):
    """Global production retry budget for one validation pass.

    The initial translator already batched the chapter. This gate may ask for a
    bounded selective correction, but it must never turn many validation
    findings into one request per region. For the 96-region production fixture
    this yields 12 retries, keeping total logical calls near 12 + 12 = 24.
    """
    total = len([group for group in (groups or []) if getattr(group, "sent_to_translation", False)])
    if total <= 0 or int(config.TRANSLATION_MAX_RETRIES) <= 0:
        return 0
    return max(1, min(total, math.ceil(total / 8)))


def _chapter_consistency_reason(ledger, source, candidate):
    """Why a candidate contradicts something the chapter already established.

    Terminology first, then character identity: both are chapter-scoped facts
    the pipeline decided earlier, and both are reported with the same structured
    ``reason:subject`` shape so the retry can name the constraint.
    """
    if ledger is None:
        return ""
    reason = str(ledger.drift_reason(source, candidate) or "")
    if reason or not hasattr(ledger, "character_conflict_reason"):
        return reason
    return str(ledger.character_conflict_reason(source, candidate) or "")


def _retry_terminology_drift(
    group, translator, ledger, force, retry_records, fidelity_check=None
):
    """One extra call when a candidate contradicts a decision already taken.

    Chapter facts are part of the trust boundary. If the only available target
    keeps contradicting established terminology or character identity after the
    bounded retry, the region is review-required instead of silently trusting a
    known-conflicting candidate.
    """
    if ledger is None or not hasattr(translator, "translate_strict"):
        return
    drift = _chapter_consistency_reason(ledger, group.text, group.translation)
    if not drift:
        return
    reason_code = drift.split(":", 1)[0]
    if reason_code != "terminology_conflict" and hasattr(ledger, "note_character_retry"):
        ledger.note_character_retry()
    name_spans = group_proper_name_spans(group)
    try:
        candidate = translator.translate_strict(
            group.text,
            previous_translation=group.translation,
            validation_reason=reason_code,
            force=force,
            proper_names=name_spans,
            retry_origin="terminology_retry",
            retry_attempt=group.translation_retry_count + 1,
        )
    except Exception as exc:  # noqa: BLE001 - one region must not sink the chapter.
        retry_records.append({
            "group_id": group.group_id,
            "source": group.text,
            "previous_translation": group.translation,
            "candidate_translation": "",
            "attempt": group.translation_retry_count + 1,
            "valid": False,
            "reason": f"terminology_retry_error:{type(exc).__name__}",
            "terminology": drift,
        })
        group.translation_retry_count += 1
        _finalize_translation_failure(
            group,
            f"{reason_code}_after_retries",
            candidate="",
            rejected_candidate=group.translation,
            validator_reason=drift,
        )
        return
    candidate = _match_source_case(group.text, clean_ocr_text(candidate))
    valid = False
    if candidate:
        valid, _reason = validate_translation_text(
            group.text,
            candidate,
            group.classification,
            _group_validation_allowed_proper_names(group),
            required_name_spans=name_spans,
        )
        valid = bool(valid) and not _chapter_consistency_reason(
            ledger, group.text, candidate
        )
        # This retry may only ever improve the region. A consistency fix that
        # arrived with a changed meaning is not an improvement.
        if valid and fidelity_check is not None:
            valid = not fidelity_check(candidate)
    resolved_reason = f"{reason_code.removesuffix('_conflict')}_retry_ok"
    retry_records.append({
        "group_id": group.group_id,
        "source": group.text,
        "previous_translation": group.translation,
        "candidate_translation": candidate,
        "attempt": group.translation_retry_count + 1,
        "valid": valid,
        "reason": resolved_reason if valid else f"{reason_code}_unresolved",
        "terminology": drift,
    })
    group.translation_retry_count += 1
    if valid:
        group.translation = candidate
        group.translation_candidate = candidate
        group.translation_validation_reason = resolved_reason
        _set_translation_terminal_state(group, "translated", resolved_reason)
        return
    _finalize_translation_failure(
        group,
        f"{reason_code}_after_retries",
        candidate=candidate,
        rejected_candidate=candidate or group.translation,
        validator_reason=drift,
    )


NATURALIZATION_ELIGIBILITY_SIGNALS = frozenset({
    "ptbr_naturalization_needed",
    "naturalization_needed",
    "style_refinement_needed",
    "literal_translation",
    "awkward_ptbr",
})


def _bump_counter(stats, name, value=1):
    if stats is None:
        return
    stats[name] = int(stats.get(name, 0)) + value


def _naturalization_signal(group):
    evidence = {}
    for source in (
        getattr(group, "quality_evidence", None),
        getattr(group, "classification_evidence", None),
        getattr(group, "visual_validation", None),
    ):
        if isinstance(source, dict):
            evidence.update(source)
    reasons = set(str(item) for item in getattr(group, "quality_reasons", []) or [])
    for signal in NATURALIZATION_ELIGIBILITY_SIGNALS:
        if bool(evidence.get(signal)) or signal in reasons:
            return signal
    return ""


def _naturalization_candidate_text(value):
    if isinstance(value, dict):
        for key in ("natural_ptbr", "text", "candidate", "translation"):
            text = clean_ocr_text(value.get(key) or "")
            if text:
                return text
        return ""
    return clean_ocr_text(value)


def _naturalization_rejection_reason(reason):
    reason = str(reason or "").strip()
    if reason.startswith(f"{semantic_fidelity.FIDELITY_UNCERTAIN}:"):
        details = [item for item in reason.split(":", 1)[1].split(",") if item]
        return details[0] if details else semantic_fidelity.FIDELITY_UNCERTAIN
    return reason.split(":", 1)[0] if reason else "naturalization_rejected"


def _naturalization_request(group, trusted_translation, ledger, reason):
    context_lines, terms = _fidelity_context(ledger)
    request = {
        "source_text": str(group.text or ""),
        "trusted_translation": str(trusted_translation or ""),
        "target_locale": "pt-BR",
        "classification": str(group.classification or ""),
        "region_type": str(group.region_type or ""),
        "group_id": str(group.group_id or ""),
        "region_id": str(group.region_id or ""),
        "reason": str(reason or ""),
        "terminology": dict(terms or {}),
        "protected_proper_names": list(group_proper_name_spans(group)),
        "character_context": list(context_lines or ()),
        "max_context_lines": FIDELITY_CONTEXT_MAX_LINES,
        "max_terms": FIDELITY_CONTEXT_MAX_TERMS,
    }
    group.naturalization_context = {
        "reason": request["reason"],
        "term_count": len(request["terminology"]),
        "character_context_count": len(request["character_context"]),
        "bounded": True,
    }
    return request


def _naturalizer_call(naturalizer, request):
    if hasattr(naturalizer, "naturalize_ptbr"):
        return naturalizer.naturalize_ptbr(request)
    if hasattr(naturalizer, "naturalize"):
        return naturalizer.naturalize(request)
    if callable(naturalizer):
        return naturalizer(request)
    raise RuntimeError("naturalizer_unavailable")


def _maybe_naturalize_translation(
    group,
    naturalizer,
    *,
    terminology_ledger,
    fidelity_verifier,
    fidelity_stats,
):
    group.naturalization_selected_version = "trusted_translation"
    if (
        naturalizer is None
        or not group.translation_valid
        or group.translation_final_state != "translated"
        or group.ocr_quality_blocked
        or group.classification not in semantic_fidelity.TRANSLATABLE_CLASSIFICATIONS
    ):
        _bump_counter(fidelity_stats, "naturalization_skipped")
        return
    signal = _naturalization_signal(group)
    if not signal:
        _bump_counter(fidelity_stats, "naturalization_skipped")
        return

    trusted_translation = clean_ocr_text(group.translation)
    if not trusted_translation:
        _bump_counter(fidelity_stats, "naturalization_skipped")
        return
    group.naturalization_eligible = True
    _bump_counter(fidelity_stats, "naturalization_eligible")

    request = _naturalization_request(
        group, trusted_translation, terminology_ledger, signal
    )
    try:
        raw = _naturalizer_call(naturalizer, request)
    except Exception as exc:  # noqa: BLE001 - enhancement failure falls back.
        group.naturalization_attempted = True
        group.naturalization_status = "failed"
        group.naturalization_rejected_reason = f"naturalizer_error:{type(exc).__name__}"
        _bump_counter(fidelity_stats, "naturalization_attempted")
        _bump_counter(fidelity_stats, "naturalization_failed")
        _bump_counter(fidelity_stats, "naturalization_fallback_to_translation")
        return

    group.naturalization_attempted = True
    _bump_counter(fidelity_stats, "naturalization_attempted")
    candidate = _match_source_case(
        group.text,
        _naturalization_candidate_text(raw),
    )
    group.naturalization_candidate = candidate
    if not candidate:
        group.naturalization_status = "failed"
        group.naturalization_rejected_reason = "naturalization_candidate_empty"
        _bump_counter(fidelity_stats, "naturalization_failed")
        _bump_counter(fidelity_stats, "naturalization_fallback_to_translation")
        return

    name_spans = group_proper_name_spans(group)
    valid, reason = validate_translation_text(
        group.text,
        candidate,
        group.classification,
        _group_validation_allowed_proper_names(group),
        required_name_spans=name_spans,
    )
    if valid:
        drift = _chapter_consistency_reason(
            terminology_ledger, group.text, candidate
        )
        if drift:
            valid, reason = False, drift
    if valid:
        local_post_fidelity = semantic_fidelity.evaluate_local_fidelity(
            group.text,
            candidate,
            classification=group.classification,
            protected_entities=_fidelity_protected_entities(
                group, terminology_ledger
            ),
            proper_names=name_spans,
        )
        post_budget = {"verifier_calls": 0}
        reason = _fidelity_reason_for(
            group,
            candidate,
            ledger=terminology_ledger,
            verifier=fidelity_verifier,
            budget=post_budget,
            name_spans=name_spans,
            stats=fidelity_stats,
        )
        if (
            reason == semantic_fidelity.FIDELITY_UNCERTAIN
            and local_post_fidelity.primary_reason
        ):
            reason = local_post_fidelity.primary_reason
        valid = not bool(reason)
    if valid:
        group.translation = candidate
        group.translation_candidate = candidate
        group.naturalization_accepted = True
        group.naturalization_status = "accepted"
        group.naturalization_selected_version = "naturalized"
        group.translation_validation_reason = "naturalization_ok"
        _set_translation_terminal_state(group, "translated", "naturalization_ok")
        _bump_counter(fidelity_stats, "naturalization_accepted")
        return

    group.naturalization_status = "rejected"
    group.naturalization_rejected_reason = _naturalization_rejection_reason(reason)
    _bump_counter(fidelity_stats, "naturalization_rejected_fidelity")
    _bump_counter(fidelity_stats, "naturalization_fallback_to_translation")


FIDELITY_VERIFIER_CALLS_PER_GROUP = 1
FIDELITY_CONTEXT_MAX_LINES = 4
FIDELITY_CONTEXT_MAX_TERMS = 20


def _bump_fidelity(stats, name, value=1):
    if stats is None:
        return
    stats[name] = int(stats.get(name, 0)) + value


def _fidelity_context(ledger):
    """The bounded facts the adjudicator may see - never the chapter."""
    lines, terms = (), {}
    if ledger is None:
        return lines, terms
    if hasattr(ledger, "character_prompt_lines"):
        lines = tuple(ledger.character_prompt_lines() or ())[:FIDELITY_CONTEXT_MAX_LINES]
    if hasattr(ledger, "term_bindings"):
        terms = {
            str(entry.get("source") or key): str(entry.get("target") or "")
            for key, entry in list((ledger.term_bindings() or {}).items())[
                :FIDELITY_CONTEXT_MAX_TERMS
            ]
            if isinstance(entry, dict)
        }
    return lines, terms


def _fidelity_protected_entities(group, ledger):
    """Chapter identities strong enough to block a translation on their own.

    The ledger files a name under its source spelling, and an ordinary source
    word that once survived a region is filed the same way. Blocking on those
    would reject correct translations of common nouns, so the same lexical
    authority that decides whether a token may be preserved as a name decides
    here too - this adds no proper-name detector of its own.
    """
    return tuple(
        entity
        for entity in semantic_fidelity.ledger_entities_for(group.text, ledger)
        if not all(
            _token_is_source_vocabulary(token.upper())
            for token in re.findall(r"[^\W\d_]+", entity, flags=re.UNICODE)
        )
    )


def _fidelity_reason_for(group, candidate, *, ledger, verifier, budget, name_spans, stats):
    """Why this candidate may not be trusted, or '' when it may.

    Local invariants first and free; the semantic verifier only for what they
    cannot settle, at most once per region, and its uncertainty is never read as
    a yes.
    """
    protected = _fidelity_protected_entities(group, ledger)
    finding = semantic_fidelity.evaluate_local_fidelity(
        group.text,
        candidate,
        classification=group.classification,
        protected_entities=protected,
        proper_names=name_spans,
    )
    if finding.faithful:
        _bump_fidelity(stats, "fidelity_fast_path_pass")
        return ""
    if finding.status == semantic_fidelity.BLOCKED:
        _bump_fidelity(stats, "fidelity_local_block")
        return finding.reason()

    can_adjudicate = verifier is not None and hasattr(verifier, "verify_fidelity")
    if not can_adjudicate or budget["verifier_calls"] >= FIDELITY_VERIFIER_CALLS_PER_GROUP:
        # No adjudication available or already spent: unproven, so not trusted.
        _bump_fidelity(stats, "fidelity_verifier_uncertain")
        return semantic_fidelity.FidelityFinding(
            semantic_fidelity.VERIFY,
            (semantic_fidelity.FIDELITY_UNCERTAIN,) + tuple(finding.reason_codes),
        ).reason()

    budget["verifier_calls"] += 1
    _bump_fidelity(stats, "fidelity_verifier_requested")
    context_lines, terms = _fidelity_context(ledger)
    verdict = semantic_fidelity.adjudicate(
        verifier,
        source_text=group.text,
        candidate=candidate,
        finding=finding,
        target_language=getattr(config, "TARGET_LANGUAGE", "pt"),
        protected_entities=protected + tuple(name_spans or ()),
        terminology=terms,
        context=context_lines,
    )
    if verdict.faithful:
        _bump_fidelity(stats, "fidelity_verifier_pass")
        return ""
    _bump_fidelity(
        stats,
        "fidelity_verifier_fail"
        if verdict.status == semantic_fidelity.BLOCKED
        else "fidelity_verifier_uncertain",
    )
    return verdict.reason()


def validate_and_retry_translations(
    groups,
    translator,
    force=False,
    terminology_ledger=None,
    fidelity_verifier=None,
    fidelity_stats=None,
    ptbr_naturalizer=None,
):
    retry_records = []
    selective_retry_budget_remaining = _selective_translation_retry_budget(groups)
    # Scanned once, and only if some region actually hands its source back: a chapter
    # with nothing to explain never pays for the scan.
    chapter_entities = {}

    def chapter_entity_tokens():
        if "tokens" not in chapter_entities:
            chapter_entities["tokens"] = _chapter_preserved_entity_tokens(groups)
        return chapter_entities["tokens"]

    for group in groups:
        if not group.sent_to_translation:
            continue
        group.initial_translation_calls = 1
        group.selective_retry_calls = 0
        group.retry_reasons_seen = []
        group.retry_budget_remaining = 1 if selective_retry_budget_remaining > 0 else 0
        name_spans = group_proper_name_spans(group)
        # One budget per region: no module state, so two workers translating two
        # chapters can never spend each other's calls or contaminate a verdict.
        fidelity_budget = {"verifier_calls": 0}

        def fidelity_reason_for(candidate):
            return _fidelity_reason_for(
                group,
                candidate,
                ledger=terminology_ledger,
                verifier=fidelity_verifier,
                budget=fidelity_budget,
                name_spans=name_spans,
                stats=fidelity_stats,
            )

        valid, reason = validate_translation_text(
            group.text,
            group.translation,
            group.classification,
            _group_validation_allowed_proper_names(group),
            required_name_spans=name_spans,
        )
        if valid:
            # Everything above answered "is this the target language". This asks
            # whether it still says what the source said.
            fidelity_reason = fidelity_reason_for(group.translation)
            if fidelity_reason:
                valid, reason = False, fidelity_reason
        group.translation_valid = valid
        group.translation_validation_reason = reason
        if reason:
            group.retry_reasons_seen.append(reason)
        if valid:
            _set_translation_terminal_state(group, "translated", reason)
            _retry_terminology_drift(
                group,
                translator,
                terminology_ledger,
                force,
                retry_records,
                fidelity_check=fidelity_reason_for,
            )
            _maybe_naturalize_translation(
                group,
                ptbr_naturalizer,
                terminology_ledger=terminology_ledger,
                fidelity_verifier=fidelity_verifier,
                fidelity_stats=fidelity_stats,
            )
            continue
        if semantic_fidelity.is_fidelity_reason(reason):
            _bump_fidelity(fidelity_stats, "fidelity_retry")
        original_candidate = group.translation_candidate or clean_ocr_text(
            group.translation
        )
        latest_candidate = original_candidate
        had_retry_error = False
        names_were_forbidden = False
        retry_enabled = bool(
            config.TRANSLATION_VALIDATION
            and config.TRANSLATION_RETRY_ON_MIXED_LANGUAGE
            and int(config.TRANSLATION_MAX_RETRIES) > 0
            and hasattr(translator, "translate_strict")
            and group.retry_budget_remaining > 0
            and selective_retry_budget_remaining > 0
        )
        if retry_enabled:
            selective_retry_budget_remaining -= 1
            attempt = group.translation_retry_count + 1
            isolated_first = _needs_isolated_retry(group, reason)
            names_were_forbidden = bool(isolated_first)
            try:
                candidate = translator.translate_strict(
                    group.text,
                    previous_translation=latest_candidate,
                    validation_reason=reason,
                    force=force,
                    allow_proper_names=not isolated_first,
                    proper_names=[] if isolated_first else name_spans,
                    retry_origin="isolated_retry" if isolated_first else "quality_retry",
                    retry_attempt=attempt,
                )
            except Exception as exc:  # noqa: BLE001 - keep the caller on failure.
                candidate = ""
                had_retry_error = True
                reason = (
                    f"isolated_retry_error:{type(exc).__name__}"
                    if isolated_first
                    else f"strict_retry_error:{type(exc).__name__}"
                )
            candidate = _repair_translation_candidate_before_validation(
                group.text,
                clean_ocr_text(candidate),
                group.classification,
                required_name_spans=name_spans,
            )
            candidate = _match_source_case(group.text, candidate)
            group.retry_candidate = candidate
            group.retry_candidate_class = _candidate_forensic_class(
                group.text,
                candidate,
                group.classification,
            )
            if candidate:
                latest_candidate = candidate
                group.translation_candidate = candidate
            valid, new_reason = validate_translation_text(
                group.text,
                candidate,
                group.classification,
                _group_validation_allowed_proper_names(group),
                required_name_spans=name_spans,
            )
            if valid and candidate:
                # A retry is a new candidate, not a correction we may assume
                # worked: it faces the same fidelity gate as the first one.
                fidelity_reason = fidelity_reason_for(candidate)
                if fidelity_reason:
                    valid, new_reason = False, fidelity_reason
            retry_record = {
                "group_id": group.group_id,
                "source": group.text,
                "previous_translation": group.translation,
                "candidate_translation": candidate,
                "attempt": attempt,
                "valid": valid,
                "reason": new_reason,
                "retry_type": "isolated" if isolated_first else "quality",
            }
            if isolated_first:
                retry_record["isolated"] = True
            retry_records.append(retry_record)
            group.translation_retry_count = attempt
            group.selective_retry_calls += 1
            group.retry_budget_remaining = 0
            if new_reason:
                group.retry_reasons_seen.append(new_reason)
            if valid:
                group.translation = candidate
                group.translation_candidate = candidate
                group.translation_valid = True
                group.translation_validation_reason = "retry_ok"
                group.rejected_translation = ""
                group.manual_review_required = False
                _set_translation_terminal_state(
                    group,
                    "translated",
                    "retry_ok",
                )
                _maybe_naturalize_translation(
                    group,
                    ptbr_naturalizer,
                    terminology_ledger=terminology_ledger,
                    fidelity_verifier=fidelity_verifier,
                    fidelity_stats=fidelity_stats,
                )
            else:
                reason = new_reason

        if group.translation_valid:
            continue
        # The model was told this text held no names and that every word had to be
        # translated, and it handed the text straight back. For a lone
        # out-of-vocabulary token that is not a failure to translate: it is the
        # model reporting there is nothing to translate, which is what a name is.
        if names_were_forbidden and _finalize_proper_name_only(group, latest_candidate):
            continue
        # A provider that cannot be instructed never produces the answer above, so the
        # chapter answers instead: every word kept here may already have been proved a
        # name by a region the translator turned into valid target text while keeping
        # that word verbatim. One unproven word and the group is still held.
        if _group_is_proven_entity_echo(
            group, latest_candidate, chapter_entity_tokens()
        ):
            _finalize_proper_name_only(group, latest_candidate, proven=True)
            continue
        if semantic_fidelity.is_fidelity_reason(reason):
            # Both attempts stayed materially unfaithful. The least bad of two
            # wrong meanings is still a wrong meaning: the region is held.
            _bump_fidelity(fidelity_stats, "fidelity_blocked")
        failure_reason = _terminal_translation_failure_reason(
            group,
            "strict_retry_error" if had_retry_error and not latest_candidate else reason,
            latest_candidate,
        )
        _finalize_translation_failure(
            group,
            failure_reason,
            candidate=latest_candidate,
            rejected_candidate=original_candidate,
            validator_reason=reason,
        )
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


def _external_narration_evidence(group, image_bgr, words, reading_phrase, page_index=None):
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

    with profile_step("external_narration.classification_roi", page_index=page_index):
        roi, _ = _classification_roi(image_bgr, group.box)
    if roi.size == 0:
        return False
    with profile_step("external_narration.gray_stats", page_index=page_index):
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        local_std = float(np.std(gray))
        local_mean = float(np.mean(gray))
    uniform_background = local_std <= 58 or local_mean >= 168 or local_mean <= 88
    with profile_step("external_narration.horizontal_whitespace_band", page_index=page_index):
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


def _enclosure_evidence(image_bgr, group_box, page_index=None):
    with profile_step("enclosure.classification_roi", page_index=page_index):
        roi, local_box = _classification_roi(image_bgr, group_box)
    if roi.size == 0:
        return False, False

    with profile_step("enclosure.uniform_container_evidence", page_index=page_index):
        component = _uniform_container_evidence(roi, local_box)
    with profile_step("enclosure.contour_container_evidence", page_index=page_index):
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
            # A dense component clipped by one or two ROI edges can still be
            # a real container. A component spanning three or more edges is
            # connected to the exterior field and cannot establish enclosure.
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
        touches = sum(
            (
                x <= 1,
                y <= 1,
                x + w >= roi.shape[1] - 1,
                y + h >= roi.shape[0] - 1,
            )
        )
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.025 * perimeter, True)
        rectangularity = area / max(1, w * h)
        # A contour may meet one or two ROI edges simply because the crop is
        # tight. Treat it as external art only when it spans at least three
        # borders *and* has too little filled area to describe a closed
        # container. Edge contact is therefore evidence, not a sole verdict.
        if touches >= 3 and rectangularity < 0.45:
            continue
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


def _classify_background_region(img_bgr, group, page_index=None):
    if group.classification == "sfx":
        return "sfx_area", {"reason": "group_classified_as_sfx"}

    with profile_step("background.safe_draw_box", page_index=page_index):
        draw_box = _safe_draw_box(group.box, img_bgr.shape, group)
    x, y, w, h = draw_box
    roi = img_bgr[y : y + h, x : x + w]
    if roi.size == 0:
        return "unknown", {"reason": "empty_region"}

    with profile_step("background.color_and_text_mask", page_index=page_index):
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
    with profile_step("background.context_sampling", page_index=page_index):
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

    with profile_step("background.texture_edges_gradients", page_index=page_index):
        blurred = cv2.GaussianBlur(gray, (7, 7), 0)
        local_texture = cv2.absdiff(gray, blurred)
        local_texture_mean = float(np.mean(local_texture[background_pixels]))
        edges = cv2.Canny(gray, 55, 155)
        edge_density = float(np.mean((edges > 0)[background_pixels]))
        sobel_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        sobel_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        gradient_magnitude = cv2.magnitude(sobel_x, sobel_y)
        gradient_strength = float(np.mean(gradient_magnitude[background_pixels]))

    # An OCR polygon can cover almost the whole draw box, in which case the
    # sampled "background" falls back to every pixel and the group's own glyphs
    # dominate every statistic above.  Measure the dark backdrop on its own,
    # away from the glyph halo, so the region can be judged by what it actually
    # is instead of by the lettering printed on it.
    with profile_step("background.interior_backdrop", page_index=page_index):
        dark_backdrop = ((gray <= 75) & background_pixels).astype(np.uint8)
        interior_core = cv2.erode(
            dark_backdrop,
            np.ones((5, 5), dtype=np.uint8),
            iterations=1,
        ) > 0
        if np.count_nonzero(interior_core) < 32:
            interior_core = dark_backdrop > 0
        if np.any(interior_core):
            core_values = gray[interior_core]
            interior_dark_std = float(np.std(core_values))
            interior_value_span = float(
                np.percentile(core_values, 97) - np.percentile(core_values, 3)
            )
            interior_dark_texture = float(np.mean(local_texture[interior_core]))
            interior_dark_gradient = float(np.mean(gradient_magnitude[interior_core]))
        else:
            interior_dark_std = 255.0
            interior_value_span = 255.0
            interior_dark_texture = 255.0
            interior_dark_gradient = 255.0

    # Hough must inspect the background, not strokes from the OCR text itself.
    # Otherwise ordinary letters inside a clean white balloon look like dozens
    # of diagonal/action lines and incorrectly disable flat-fill cleanup.
    with profile_step("background.hough_lines", page_index=page_index):
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
        "image_width": int(image_w),
        "image_height": int(image_h),
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
    relaxed_white_context = (
        context_brightness >= 248.0
        and context_white_ratio >= 0.975
        and context_dark_ratio <= 0.015
        and context_saturation_mean <= 5.0
    )
    short_text_white_context = (
        group.classification in {"narration", "unknown"}
        and 3 <= len(compact_text) <= 14
        and w >= max(38, int(h * 1.0))
        and h <= 96
        and context_brightness >= 242.0
        and context_white_ratio >= 0.94
        and context_dark_ratio <= 0.04
        and context_saturation_mean <= 8.0
        and long_lines == 0
        and diagonal_lines <= 1
    )
    open_white_narration = (
        group.classification in {"narration", "unknown"}
        and w >= max(50, int(h * 1.5))
        and (
            len(compact_text) >= 8
            or semantic_short_narration
            or short_text_white_context
            or (white_context and len(compact_text) >= 3)
            or (relaxed_white_context and len(compact_text) >= 5)
        )
        and (
            (
                brightness >= 190.0
                and white_ratio >= 0.70
                and dark_ratio <= 0.21
                and saturation_mean <= 8.0
            )
            or short_text_white_context
            or white_context
        )
    )
    dark_context = (
        context_brightness <= 32.0
        and context_dark_ratio >= 0.96
        and context_white_ratio <= 0.02
        and context_saturation_mean <= 12.0
    )
    # ``dark_context`` samples the ring *around* the group, so bright artwork
    # next to an open narration caption contaminates it.  Measure the group's
    # own interior as an independent signal: a region that is dark, flat and
    # free of local structure is a uniform dark backdrop regardless of what
    # surrounds it.  The bar is deliberately stricter than ``uniform_dark``
    # (used for enclosed dark balloons) because no balloon contour proves the
    # extent of the region here.
    uniform_dark_interior = bool(
        # The non-dark minority has to be small enough to be the lettering
        # itself; the uniformity evidence below is measured on the dark
        # backdrop only, so it stays valid however the glyphs are sampled.
        dark_ratio >= 0.80
        and interior_dark_std <= 18.0
        and interior_value_span <= 40.0
        and interior_dark_texture <= 6.0
        and interior_dark_gradient <= 30.0
        and saturation_mean <= 60.0
    )
    open_dark_narration = (
        group.classification in {"narration", "unknown"}
        and w >= max(80, int(h * 1.8))
        and len(compact_text) >= 8
        and (
            dark_context
            or (uniform_dark_interior and not group.inside_balloon_like_region)
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
    open_light_art_caption = bool(
        group.classification in {"speech", "narration", "unknown"}
        and (
            enclosure_like
            or (
                brightness >= 205.0
                and saturation_mean <= 30.0
                and white_ratio >= 0.40
                and dark_ratio <= 0.03
                and context_brightness >= 180.0
                and context_brightness < 238.0
                and 0.35 <= context_white_ratio < 0.86
                and context_dark_ratio <= 0.04
                and context_saturation_mean <= 30.0
                and local_texture_mean <= 2.0
                and edge_density <= 0.02
                and gradient_strength <= 28.0
            )
        )
        and not strongly_uniform_white
        and not open_white_narration
        and brightness >= 170.0
        and white_ratio < 0.96
        and dark_ratio <= 0.08
        and context_white_ratio < 0.86
        and context_brightness < 238.0
        and long_lines <= 1
        and diagonal_lines <= 1
    )

    if open_light_art_caption:
        background_type = "textured_art"
    elif open_white_narration or open_dark_narration:
        background_type = "narration_box"
    elif strongly_uniform_white:
        background_type = (
            "white_balloon"
            if group.classification == "speech"
            else "narration_box"
        )
    elif uniform_light and group.inside_balloon_like_region:
        background_type = "white_balloon"
    elif uniform_light and group.inside_narration_box_like_region:
        background_type = "narration_box"
    elif speed_lines:
        background_type = "speed_lines"
    elif (uniform_dark or dark_context) and group.inside_balloon_like_region:
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
    metrics["open_dark_narration"] = bool(open_dark_narration)
    metrics["open_light_art_caption"] = bool(open_light_art_caption)
    metrics["dark_context"] = bool(dark_context)
    metrics["uniform_dark_interior"] = bool(uniform_dark_interior)
    metrics["interior_dark_std"] = round(interior_dark_std, 3)
    metrics["interior_value_span"] = round(interior_value_span, 3)
    metrics["interior_dark_texture"] = round(interior_dark_texture, 3)
    metrics["interior_dark_gradient"] = round(interior_dark_gradient, 3)
    metrics["relaxed_white_context"] = bool(relaxed_white_context)
    metrics["short_text_white_context"] = bool(short_text_white_context)
    metrics["background_edge_density"] = round(float(np.mean(background_edges > 0)), 4)
    return background_type, metrics


def _source_scoped_speech_reason(group):
    """Empty when the group is proven ordinary speech we may clean narrowly.

    The narrow fallback is only allowed where the source itself is the evidence:
    an ordinary speech group whose translation candidate is usable, whose source
    ownership is complete, and whose lines are individually traceable.  Anything
    unproven - a stylized title, an SFX, a preserved entity, an unreadable OCR
    result, an incomplete source - keeps the legacy behaviour and fails closed.
    """

    if not config.SOURCE_SCOPED_SPEECH_CLEANUP:
        return "source_scoped_disabled"
    classification = str(getattr(group, "classification", "") or "").strip().lower()
    if classification in {"logo", "sfx"} or (
        classification == "decorative"
        and not _decorative_textured_story_promotion_allowed(group)
    ):
        return "source_scoped_requires_story_translation_authority"
    if getattr(group, "preserve_as_name", False):
        return "source_scoped_excludes_preserved_entity"
    if not _group_has_story_translation_authority(group):
        return "source_scoped_requires_story_translation_authority"
    candidate = str(group.translation or group.translation_candidate or "").strip()
    if not candidate:
        return "source_scoped_requires_translation_candidate"
    if group.ocr_quality_blocked or _ocr_unintelligible_source_text(group.text):
        return "source_scoped_requires_intelligible_source"
    status = str((getattr(group, "source_completeness", None) or {}).get("status") or "")
    if status != source_completeness.STATUS_PASS:
        return "source_scoped_requires_source_completeness_pass"
    lines = _cleanup_lines_for_group(group)
    if not lines:
        return "source_scoped_requires_owned_source_lines"
    for line in lines:
        if not (getattr(line, "metadata", None) or {}).get("ocr_line_id"):
            return "source_scoped_requires_line_provenance"
    return ""


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
    if strategy in {"glyph_overlay", "caption_overlay"} and not tight_background:
        return current_bgr.copy(), np.zeros(original_bgr.shape[:2], dtype=np.uint8), {
            "mask_valid": False,
            "reason": f"{strategy}_not_needed_on_uniform_background",
        }
    if strategy == "caption_overlay" and not config.TEXTURED_CAPTION_OVERLAY:
        return current_bgr.copy(), np.zeros(original_bgr.shape[:2], dtype=np.uint8), {
            "mask_valid": False,
            "reason": "textured_caption_overlay_disabled",
        }
    if strategy == "source_scoped":
        if not tight_background:
            return current_bgr.copy(), np.zeros(original_bgr.shape[:2], dtype=np.uint8), {
                "mask_valid": False,
                "reason": "source_scoped_not_needed_on_uniform_background",
            }
        scope_reason = _source_scoped_speech_reason(group)
        if scope_reason:
            return current_bgr.copy(), np.zeros(original_bgr.shape[:2], dtype=np.uint8), {
                "mask_valid": False,
                "reason": scope_reason,
            }
    base_mask = _build_text_mask(original_bgr.shape, [group], padding=0)
    if not np.any(base_mask):
        return current_bgr.copy(), base_mask, {
            "mask_valid": False,
            "reason": "empty_source_polygon",
        }

    if strategy == "caption_overlay":
        limit_padding = min(3, config.MAX_MASK_EXPANSION)
    else:
        limit_padding = 1 if (strategy == "conservative" or tight_background) else (
            _glyph_footprint_padding(
                group,
                min(config.MAX_MASK_EXPANSION, config.TEXT_MASK_PADDING + 1),
            )
        )
    maximum_mask = base_mask.copy()
    if limit_padding > 0:
        maximum_mask = cv2.dilate(
            maximum_mask,
            np.ones((limit_padding * 2 + 1, limit_padding * 2 + 1), np.uint8),
            iterations=1,
        )
    mask_strategy = "conservative" if tight_background else strategy
    if strategy == "caption_overlay":
        component_mask, component_metrics = _caption_overlay_mask(
            original_bgr,
            group,
            maximum_mask=maximum_mask,
        )
    elif strategy == "glyph_overlay":
        component_mask, component_metrics = _outlined_light_text_mask(
            original_bgr,
            group,
            maximum_mask=maximum_mask,
        )
    else:
        component_mask, component_metrics = _component_text_mask(
            original_bgr,
            group,
            maximum_mask=maximum_mask,
            strategy=mask_strategy,
        )
    cleanup_mask = component_mask
    if strategy == "source_scoped" and tight_background:
        outline_mask, outline_metrics = _outlined_light_text_mask(
            original_bgr,
            group,
            maximum_mask=maximum_mask,
        )
        if np.any(outline_mask):
            new_outline_pixels = int(
                np.count_nonzero((outline_mask > 0) & (cleanup_mask == 0))
            )
            cleanup_mask = cv2.bitwise_or(cleanup_mask, outline_mask)
            component_metrics["text_component_pixels"] = int(
                component_metrics.get("text_component_pixels", 0)
                + new_outline_pixels
            )
            component_metrics["source_scoped_outline_light_pixels"] = new_outline_pixels
            component_metrics["source_scoped_outline_light_components"] = int(
                outline_metrics.get("accepted_text_components", 0)
            )
            owned_geometry_pixels = int(np.count_nonzero(base_mask))
            # Coverage has to be measured against the source *glyphs* inside the
            # owned polygons, not against the polygon area.  An accurate glyph
            # mask never fills a whole OCR quadrilateral, so measuring against
            # its area made this fallback fire on every textured region and
            # replace the artwork with the quadrilateral itself - the flat
            # rectangular patch this strategy exists to avoid.
            removal = _uncovered_source_text_evidence(
                original_bgr,
                original_bgr,
                group,
                cleanup_mask,
            )
            current_coverage = (
                float(removal["source_owned_geometry_coverage"])
                if removal.get("measured")
                else float(
                    np.count_nonzero((cleanup_mask > 0) & (base_mask > 0))
                    / max(1, owned_geometry_pixels)
                )
            )
            if current_coverage < 0.75 and not _proven_uniform_dark_region(
                background_metrics
            ):
                geometry_pixels = int(
                    np.count_nonzero((base_mask > 0) & (cleanup_mask == 0))
                )
                cleanup_mask = cv2.bitwise_or(cleanup_mask, base_mask)
                component_metrics["text_component_pixels"] = int(
                    component_metrics.get("text_component_pixels", 0)
                    + geometry_pixels
                )
                component_metrics["source_scoped_owned_geometry_fallback"] = True
                component_metrics["source_scoped_owned_geometry_pixels"] = (
                    owned_geometry_pixels
                )
                component_metrics["source_scoped_owned_geometry_coverage_before"] = (
                    round(current_coverage, 4)
                )
    dark_line_mask, dark_line_metrics = _uniform_dark_line_text_mask(
        original_bgr,
        group,
    )
    if np.any(dark_line_mask):
        new_dark_line_pixels = int(
            np.count_nonzero((dark_line_mask > 0) & (cleanup_mask == 0))
        )
        cleanup_mask = cv2.bitwise_or(cleanup_mask, dark_line_mask)
        component_metrics["text_component_pixels"] = int(
            component_metrics.get("text_component_pixels", 0)
            + new_dark_line_pixels
        )
    component_metrics.update(dark_line_metrics)
    light_line_mask, light_line_metrics = _uniform_light_line_text_mask(
        original_bgr,
        group,
    )
    if np.any(light_line_mask):
        new_light_line_pixels = int(
            np.count_nonzero((light_line_mask > 0) & (cleanup_mask == 0))
        )
        cleanup_mask = cv2.bitwise_or(cleanup_mask, light_line_mask)
        component_metrics["text_component_pixels"] = int(
            component_metrics.get("text_component_pixels", 0)
            + new_light_line_pixels
        )
    component_metrics.update(light_line_metrics)
    detached_dark_mask, detached_dark_metrics = _detached_dark_text_components_mask(
        original_bgr,
        group,
        cleanup_mask,
    )
    if np.any(detached_dark_mask):
        cleanup_mask = cv2.bitwise_or(cleanup_mask, detached_dark_mask)
        component_metrics["text_component_pixels"] = int(
            component_metrics.get("text_component_pixels", 0)
            + detached_dark_metrics["detached_dark_text_pixels"]
        )
    component_metrics.update(detached_dark_metrics)
    detached_mask, detached_metrics = _detached_light_text_components_mask(
        original_bgr,
        group,
        cleanup_mask,
    )
    if np.any(detached_mask):
        cleanup_mask = cv2.bitwise_or(cleanup_mask, detached_mask)
        component_metrics["text_component_pixels"] = int(
            component_metrics.get("text_component_pixels", 0)
            + detached_metrics["detached_text_pixels"]
        )
    component_metrics.update(detached_metrics)
    source_evidence_mask = None
    if strategy == "source_scoped":
        # The owned OCR line polygons expanded by the existing text-scale padding
        # are the only artwork this strategy is ever allowed to touch.
        source_evidence_mask = _build_text_mask(original_bgr.shape, [group])
        component_metrics["source_evidence_pixels"] = int(
            np.count_nonzero(source_evidence_mask)
        )
        component_metrics["mask_pixels_clipped_to_source_evidence"] = int(
            np.count_nonzero((cleanup_mask > 0) & (source_evidence_mask == 0))
        )
        cleanup_mask = cv2.bitwise_and(cleanup_mask, source_evidence_mask)
        component_metrics["mask_pixels_outside_source_evidence"] = int(
            np.count_nonzero((cleanup_mask > 0) & (source_evidence_mask == 0))
        )
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

    if strategy == "source_scoped":
        # ``mask_to_group_area_ratio`` measures the mask against the source text
        # bbox, so an accurate glyph mask is structurally penalised for filling
        # the region the glyphs themselves define.  Here the risk that matters is
        # how much artwork the mask reaches outside the owned source evidence -
        # which the clip above already pins to zero - plus a bound on how much of
        # the page that evidence is allowed to be at all.
        evidence_pixels = max(1, int(np.count_nonzero(source_evidence_mask)))
        page_area = max(1, int(original_bgr.shape[0] * original_bgr.shape[1]))
        metrics["source_evidence_to_page_ratio"] = round(
            float(evidence_pixels / page_area),
            6,
        )
        metrics["source_scoped_mask_to_page_ratio"] = round(
            float(mask_area / page_area),
            6,
        )
        metrics["mask_to_source_evidence_ratio"] = round(
            float(mask_area / evidence_pixels),
            4,
        )
        if mask_area / page_area > config.MAX_SOURCE_SCOPED_PAGE_AREA_RATIO:
            metrics["mask_valid"] = False
            metrics["reason"] = "source_scoped_region_too_large_for_safe_cleanup"
            return current_bgr.copy(), cleanup_mask, metrics
    textured_group_ratio_limit = (
        min(0.30, config.MAX_TEXTURED_MASK_GROUP_RATIO + 0.12)
        if strategy in {"glyph_overlay", "caption_overlay"}
        else config.MAX_TEXTURED_MASK_GROUP_RATIO
    )
    textured_component_ratio_limit = config.MAX_TEXTURED_MASK_COMPONENT_RATIO
    if strategy != "source_scoped" and tight_background and (
        shape_metrics["mask_to_group_area_ratio"]
        > textured_group_ratio_limit
        or shape_metrics["largest_mask_component_to_group_ratio"]
        > textured_component_ratio_limit
        or (
            shape_metrics["broad_rectangular_mask"]
            and strategy not in {"caption_overlay"}
        )
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
    if strategy == "caption_overlay":
        metrics["reconstruction_method"] = "glyph_inpaint_telea"
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
    flat_patch_metrics = _flat_patch_artifact_metrics(
        original_bgr,
        cleaned,
        group,
        cleanup_mask,
    )
    metrics.update(flat_patch_metrics)
    if flat_patch_metrics.get("flat_patch_rejected"):
        metrics["mask_valid"] = False
        metrics["reason"] = "flat_reconstruction_patch_on_textured_background"
        return current_bgr.copy(), cleanup_mask, metrics
    dark_patch_metrics = _dark_blotch_artifact_metrics(
        original_bgr,
        cleaned,
        group,
        cleanup_mask,
        background_type,
        strategy,
    )
    metrics.update(dark_patch_metrics)
    if dark_patch_metrics.get("dark_blotch_rejected"):
        metrics["mask_valid"] = False
        metrics["reason"] = "dark_blotch_created_on_textured_art"
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
    if residual_pixels > residual_limit and strategy not in {
        "glyph_overlay",
        "caption_overlay",
    }:
        metrics["mask_valid"] = False
        metrics["reason"] = "residual_source_text_after_cleanup"
    elif strategy in {"glyph_overlay", "caption_overlay"}:
        metrics["residual_validation_deferred_to_post_render_ocr"] = True
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
    if background_type in {"white_balloon", "narration_box"} or _proven_light_cleanup_region(
        getattr(group, "background_metrics", {}) or {}
    ):
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


def _flat_patch_artifact_metrics(original_bgr, cleaned_bgr, group, cleanup_mask):
    """Reject a reconstruction that replaced artwork with a synthetic flat block.

    This is independent of source-text removal: the source glyphs can be gone and
    the result still be unacceptable.  A genuine flat balloon passes because its
    own surrounding ring is flat too - the comparison is always against the local
    source context, never against an absolute "white is suspicious" rule.
    """

    empty = {
        "flat_patch_source_ring_texture": None,
        "flat_patch_reconstruction_texture": None,
        "flat_patch_texture_ratio": None,
        "flat_patch_largest_component_area": 0,
        "flat_patch_rejected": False,
    }
    if cleanup_mask is None or not np.any(cleanup_mask):
        return empty
    flatness = _local_background_flatness(original_bgr, cleanup_mask)
    if flatness["flat_fill_supported"]:
        return {**empty, **flatness}
    mask = np.asarray(cleanup_mask) > 0
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8),
        8,
    )
    largest = max(
        (int(stats[label, cv2.CC_STAT_AREA]) for label in range(1, count)),
        default=0,
    )
    radius = int(config.FLAT_FILL_RING_RADIUS)
    ring = (
        cv2.dilate(
            mask.astype(np.uint8) * 255,
            cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE,
                (radius * 2 + 1, radius * 2 + 1),
            ),
            iterations=1,
        ) > 0
    ) & ~mask
    original_gray = cv2.cvtColor(original_bgr, cv2.COLOR_BGR2GRAY)
    cleaned_gray = cv2.cvtColor(cleaned_bgr, cv2.COLOR_BGR2GRAY)
    source_texture = float(
        np.abs(cv2.Laplacian(original_gray, cv2.CV_32F))[ring].mean()
    ) if np.any(ring) else 0.0
    # Measure the reconstruction's *interior*: the mask boundary always carries a
    # strong Laplacian response, which would hide a perfectly uniform fill.
    interior = cv2.erode(
        mask.astype(np.uint8) * 255,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)),
        iterations=1,
    ) > 0
    if not np.any(interior):
        interior = mask
    reconstruction_texture = float(
        np.abs(cv2.Laplacian(cleaned_gray, cv2.CV_32F))[interior].mean()
    )
    ratio = (
        reconstruction_texture / source_texture if source_texture > 0.0 else None
    )
    rejected = bool(
        config.REJECT_FLAT_PATCH_ON_TEXTURED_ART
        and ratio is not None
        and ratio < config.MAX_FLAT_PATCH_TEXTURE_RATIO
        # Absolute near-uniformity is what makes a patch read as synthetic. The
        # ratio alone condemns any reconstruction of grainy artwork, because the
        # context ring still holds the source lettering and inpainting cannot
        # rebuild per-pixel noise.  Smoother than the original is
        # reconstruction; one flat tone is a synthetic block.
        and reconstruction_texture <= config.MAX_FLAT_PATCH_ABSOLUTE_TEXTURE
        and largest >= config.MIN_FLAT_PATCH_COMPONENT_AREA
    )
    return {
        **flatness,
        "flat_patch_source_ring_texture": round(source_texture, 4),
        "flat_patch_reconstruction_texture": round(reconstruction_texture, 4),
        "flat_patch_texture_ratio": (
            None if ratio is None else round(float(ratio), 4)
        ),
        "flat_patch_largest_component_area": int(largest),
        "flat_patch_rejected": rejected,
    }


def _dark_blotch_artifact_metrics(
    original_bgr,
    cleaned_bgr,
    group,
    cleanup_mask,
    background_type,
    strategy,
):
    """Reject new opaque dark islands created while cleaning textured artwork."""
    if background_type not in {"textured_art", "speed_lines", "unknown"}:
        return {
            "new_dark_patch_pixels": 0,
            "largest_new_dark_component_area": 0,
            "new_dark_patch_to_group_ratio": 0.0,
            "dark_blotch_rejected": False,
        }

    original_gray = cv2.cvtColor(original_bgr, cv2.COLOR_BGR2GRAY)
    cleaned_gray = cv2.cvtColor(cleaned_bgr, cv2.COLOR_BGR2GRAY)
    new_dark = (
        (cleanup_mask > 0)
        & (original_gray >= 90)
        & (cleaned_gray <= 35)
        & ((original_gray.astype(np.int16) - cleaned_gray.astype(np.int16)) >= 55)
    )
    new_dark_u8 = new_dark.astype(np.uint8) * 255
    count, _, stats, _ = cv2.connectedComponentsWithStats(new_dark_u8, 8)
    largest = max(
        (int(stats[label, cv2.CC_STAT_AREA]) for label in range(1, count)),
        default=0,
    )
    pixels = int(np.count_nonzero(new_dark))
    group_area = max(1, int(group.box[2] * group.box[3]))
    ratio = pixels / group_area
    # Light lettering on a proven dark backdrop turns dark when it is removed,
    # because the backdrop it exposes is dark.  That is the intended repair, not
    # an invented island, and only the source-evidence-pinned strategy - whose
    # mask cannot reach past the owned OCR lines - is trusted with it.
    dark_backdrop_restored = bool(
        strategy == "source_scoped"
        and _proven_uniform_dark_region(getattr(group, "background_metrics", {}) or {})
    )
    rejected = bool(
        config.REJECT_DARK_BLOTCH_ON_TEXTURED_ART
        and not dark_backdrop_restored
        and (
            largest > config.MAX_NEW_DARK_COMPONENT_AREA
            or ratio > config.MAX_NEW_DARK_PIXEL_RATIO
        )
    )
    return {
        "new_dark_patch_pixels": pixels,
        "largest_new_dark_component_area": int(largest),
        "new_dark_patch_to_group_ratio": round(float(ratio), 6),
        "dark_backdrop_restored": dark_backdrop_restored,
        "dark_blotch_rejected": rejected,
    }


def _glyph_footprint_padding(group, base):
    """How far past the OCR polygon the visual glyph footprint may reach.

    Anti-aliasing, an outline and a drop shadow all scale with the lettering, so
    a constant padding is either wasteful on small captions or too tight on
    display lettering - where it truncated glyphs and left ghosts behind.  The
    result is still bounded by ``MAX_MASK_EXPANSION`` and only ever admits
    pixels that evidence connects to an accepted glyph component.
    """

    heights = [
        int(line.box[3])
        for line in _cleanup_lines_for_group(group)
        if int(line.box[3]) > 0
    ]
    if not heights:
        return base
    return int(max(
        base,
        min(config.MAX_MASK_EXPANSION, int(round(float(np.median(heights)) * 0.10))),
    ))


def _local_line_background_level(gray, line, fallback):
    """Median luminance of the clean band just outside one OCR line box."""
    x, y, w, h = [int(value) for value in line.box]
    radius = int(config.FLAT_FILL_RING_RADIUS)
    height, width = gray.shape[:2]
    x1, y1 = max(0, x - radius), max(0, y - radius)
    x2 = min(width, x + max(1, w) + radius)
    y2 = min(height, y + max(1, h) + radius)
    band = np.ones((y2 - y1, x2 - x1), dtype=bool)
    ix1, iy1 = max(0, x - x1), max(0, y - y1)
    ix2, iy2 = min(band.shape[1], ix1 + max(1, w)), min(band.shape[0], iy1 + max(1, h))
    band[iy1:iy2, ix1:ix2] = False
    values = gray[y1:y2, x1:x2][band]
    if values.size < int(config.MIN_FLAT_FILL_RING_PIXELS):
        return float(fallback)
    return float(np.median(values))


def _component_text_mask(img_bgr, group, maximum_mask, strategy="primary"):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    mask = np.zeros(gray.shape, dtype=np.uint8)
    raw_components = np.zeros(gray.shape, dtype=np.uint8)
    accepted_components = 0

    for line in _cleanup_lines_for_group(group):
        polygon_mask = np.zeros(gray.shape, dtype=np.uint8)
        cv2.fillPoly(polygon_mask, [np.asarray(line.polygon, dtype=np.int32)], 255)
        line_limit = polygon_mask.copy()
        line_limit_padding = 1 if strategy == "conservative" else (
            _glyph_footprint_padding(
                group,
                min(config.MAX_MASK_EXPANSION, config.TEXT_MASK_PADDING + 1),
            )
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
            # The glyph footprint is everything that differs from the *local
            # background*, not from the polygon median - the median is dragged
            # towards the lettering by the lettering itself, which used to hide
            # a soft glow just under it and leave the glow behind as a ghost.
            background_level = _local_line_background_level(gray, line, median)
            if median >= 138:
                weak_foreground = roi_gray <= min(252.0, background_level - 3.0)
            elif median <= 118:
                weak_foreground = roi_gray >= max(3.0, background_level + 3.0)
            else:
                weak_foreground = (blackhat >= 4) | (tophat >= 4)
            # Glyph pixels do not stop at the OCR quadrilateral: a full stop or a
            # descender routinely pokes a couple of pixels past it, and clipping
            # the halo to the polygon leaves that sliver behind as a ghost.  The
            # halo stays bounded by ``line_limit`` (polygon plus the existing
            # padding) and by the support of an accepted glyph component, so this
            # is evidence-driven local growth, not a wider mask.
            weak_foreground &= line_limit[y1:y2, x1:x2] > 0
            # The visual footprint of lettering scales with the lettering: small
            # captions carry a one-pixel anti-alias edge, display lettering
            # carries an outline and a soft glow several pixels wide.  A fixed
            # 3px support left that glow behind as a ghost on large text, so the
            # support radius follows the observed line height instead.
            support_radius = max(3, min(16, int(round(h * 0.10))))
            support = cv2.dilate(
                selected,
                cv2.getStructuringElement(
                    cv2.MORPH_ELLIPSE,
                    (support_radius * 2 + 1, support_radius * 2 + 1),
                ),
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


def _outlined_light_text_mask(img_bgr, group, maximum_mask):
    """Build a tight mask for white outlined lettering over artwork."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    result = np.zeros(gray.shape, dtype=np.uint8)
    raw = np.zeros(gray.shape, dtype=np.uint8)
    accepted = 0
    for line in _cleanup_lines_for_group(group):
        polygon = np.zeros(gray.shape, dtype=np.uint8)
        cv2.fillPoly(polygon, [np.asarray(line.polygon, dtype=np.int32)], 255)
        line_limit = cv2.dilate(polygon, np.ones((3, 3), np.uint8), iterations=1)
        line_limit = cv2.bitwise_and(line_limit, maximum_mask)
        x, y, w, h = line.box
        x1, y1 = max(0, x - 1), max(0, y - 1)
        x2 = min(gray.shape[1], x + w + 1)
        y2 = min(gray.shape[0], y + h + 1)
        if x2 <= x1 or y2 <= y1:
            continue
        roi_gray = gray[y1:y2, x1:x2]
        roi_sat = hsv[y1:y2, x1:x2, 1]
        roi_limit = line_limit[y1:y2, x1:x2] > 0
        bright = ((roi_gray >= 238) & (roi_sat <= 55) & roi_limit).astype(np.uint8) * 255
        nearby_dark = cv2.dilate(
            ((roi_gray <= 95) & roi_limit).astype(np.uint8) * 255,
            np.ones((5, 5), np.uint8),
            iterations=1,
        )
        bright = cv2.bitwise_and(bright, nearby_dark)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(bright, 8)
        selected = np.zeros_like(bright)
        line_area = max(1, w * h)
        for label in range(1, count):
            area = int(stats[label, cv2.CC_STAT_AREA])
            cw = int(stats[label, cv2.CC_STAT_WIDTH])
            ch = int(stats[label, cv2.CC_STAT_HEIGHT])
            if not 2 <= area <= line_area * 0.16:
                continue
            if cw > w * 0.48 or ch > h * 0.95:
                continue
            selected[labels == label] = 255
            accepted += 1
        if not np.any(selected):
            continue
        raw[y1:y2, x1:x2] = cv2.bitwise_or(raw[y1:y2, x1:x2], selected)
        halo = cv2.dilate(selected, np.ones((3, 3), np.uint8), iterations=1)
        halo = cv2.bitwise_and(halo, line_limit[y1:y2, x1:x2])
        result[y1:y2, x1:x2] = cv2.bitwise_or(result[y1:y2, x1:x2], halo)
    result = cv2.bitwise_and(result, maximum_mask)
    return result, {
        "text_component_pixels": int(np.count_nonzero(raw)),
        "accepted_text_components": int(accepted),
        "component_based": True,
        "outlined_glyph_mask": True,
    }


def _caption_overlay_mask(img_bgr, group, maximum_mask):
    """Build a tight glyph mask for captions over artwork.

    The former implementation used each OCR polygon as an opaque backing.  That
    made a fallback intended for difficult lettering paint dark rectangles over
    the illustration.  Caption cleanup now uses the same component-level
    foreground detector as the normal textured-art path; if no glyph evidence is
    available, returning an empty mask fails closed and lets the caller preserve
    the original for review.
    """
    result, component_metrics = _component_text_mask(
        img_bgr,
        group,
        maximum_mask=maximum_mask,
        strategy="conservative",
    )
    line_areas = []
    for line in _cleanup_lines_for_group(group):
        polygon = np.asarray(line.polygon, dtype=np.int32)
        if polygon.shape[0] < 3:
            continue
        line_mask = np.zeros_like(maximum_mask)
        cv2.fillPoly(line_mask, [polygon], 255)
        line_mask = cv2.bitwise_and(line_mask, maximum_mask)
        if np.any(result & line_mask):
            line_areas.append(int(np.count_nonzero(result & line_mask)))
    pixels = int(np.count_nonzero(result))
    return result, {
        **component_metrics,
        "text_component_pixels": pixels,
        "accepted_text_components": len(line_areas),
        "component_based": True,
        "caption_overlay_mask": True,
        "caption_overlay_line_count": len(line_areas),
        "caption_overlay_largest_line_area": max(line_areas, default=0),
    }


def _proven_uniform_dark_region(metrics):
    """Uniform dark backdrop proven by the ring around the group or by its interior.

    ``dark_context`` only samples the surrounding ring, which adjacent bright
    artwork contaminates.  ``uniform_dark_interior`` proves the same property on
    the region itself and is accepted as equivalent evidence.
    """

    metrics = metrics or {}
    return bool(
        metrics.get("dark_context")
        or metrics.get("uniform_dark_interior")
        or (
            float(metrics.get("brightness_mean", 255.0)) <= 35.0
            and float(metrics.get("dark_pixel_ratio", 0.0)) >= 0.95
            and float(metrics.get("interior_dark_std", 255.0)) <= 18.0
            and float(metrics.get("interior_value_span", 255.0)) <= 45.0
            and float(metrics.get("interior_dark_texture", 255.0)) <= 6.0
            and float(metrics.get("interior_dark_gradient", 255.0)) <= 30.0
        )
        or (
            float(metrics.get("context_dark_pixel_ratio", 0.0)) >= 0.90
            and float(metrics.get("context_saturation_mean", 255.0)) <= 18.0
        )
    )


def _proven_light_cleanup_region(metrics):
    """Light backdrop proven by region metrics, even if conservatively typed.

    Some real speech regions sit in a white/enclosed panel whose surrounding art
    keeps the coarse background type at ``textured_art``.  A source-scoped mask
    pinned to owned OCR geometry may safely restore that proven light backdrop;
    the white-patch guard remains active for speed-lines/textured art without
    this positive evidence.
    """

    metrics = metrics or {}
    return bool(
        (
            metrics.get("strict_uniform_light")
            and metrics.get("uniform_light")
            and (
                metrics.get("dominant_white_enclosure")
                or metrics.get("stylized_white_enclosure")
                or metrics.get("strongly_uniform_white")
            )
        )
        or (
            metrics.get("open_light_art_caption")
            and float(metrics.get("brightness_mean", 0.0)) >= 170.0
            and float(metrics.get("dark_pixel_ratio", 1.0)) <= 0.08
        )
    )


def _local_background_flatness(img_bgr, mask, *, ring_radius=None):
    """Measure whether the artwork around ``mask`` is one flat tone.

    ``_classify_background_region`` only measures high-frequency texture, so a
    smooth gradient (smoke, shading, fabric falloff) reads as "flat" there and
    used to authorise a single-colour fill that lands as a visible rectangle.
    The evidence that actually matters is how much the luminance of the clean
    ring immediately around the cleanup mask varies: a genuine white balloon or
    narration box is uniform, illustration is not.
    """

    empty = {
        "flat_fill_ring_pixels": 0,
        "flat_fill_ring_luminance_spread": None,
        "flat_fill_supported": False,
    }
    if mask is None or not np.any(mask):
        return empty
    mask_u8 = (np.asarray(mask) > 0).astype(np.uint8) * 255
    radius = int(ring_radius or config.FLAT_FILL_RING_RADIUS)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (radius * 2 + 1, radius * 2 + 1),
    )
    ring = (cv2.dilate(mask_u8, kernel, iterations=1) > 0) & (mask_u8 == 0)
    gray = (
        cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        if np.asarray(img_bgr).ndim == 3
        else np.asarray(img_bgr)
    )
    values = gray[ring].astype(np.float32)
    if values.size < int(config.MIN_FLAT_FILL_RING_PIXELS):
        return {**empty, "flat_fill_ring_pixels": int(values.size)}
    low, high = np.percentile(values, [5.0, 95.0])
    spread = float(high - low)
    return {
        "flat_fill_ring_pixels": int(values.size),
        "flat_fill_ring_luminance_spread": round(spread, 3),
        "flat_fill_supported": bool(spread <= config.MAX_FLAT_FILL_RING_SPREAD),
    }


def _detached_light_text_components_mask(img_bgr, group, source_mask):
    """Recover isolated bright glyphs beside OCR lines on a uniform dark region."""
    metrics = getattr(group, "background_metrics", {}) or {}
    dark_context = _proven_uniform_dark_region(metrics)
    empty = np.zeros(source_mask.shape, dtype=np.uint8)
    if not dark_context or not np.any(source_mask):
        return empty, {
            "detached_text_components": 0,
            "detached_text_pixels": 0,
        }

    line_heights = [line.box[3] for line in group.lines if line.box[3] > 0]
    median_height = float(np.median(line_heights)) if line_heights else group.box[3]
    horizontal_radius = max(16, min(64, int(median_height * 1.25)))
    vertical_radius = max(3, min(10, int(median_height * 0.18)))
    near = cv2.dilate(
        source_mask,
        np.ones(
            (vertical_radius * 2 + 1, horizontal_radius * 2 + 1),
            np.uint8,
        ),
        iterations=1,
    )

    x, y, w, h = _safe_draw_box(group.box, img_bgr.shape, group)
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    bright = np.zeros_like(source_mask)
    bright[y : y + h, x : x + w] = (
        gray[y : y + h, x : x + w] >= 178
    ).astype(np.uint8) * 255
    count, labels, stats, _ = cv2.connectedComponentsWithStats(bright, 8)
    selected = np.zeros_like(source_mask)
    components = 0
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        width = int(stats[label, cv2.CC_STAT_WIDTH])
        height = int(stats[label, cv2.CC_STAT_HEIGHT])
        component = labels == label
        if not np.any(component & (near > 0)):
            continue
        missing_component = component & (source_mask == 0)
        if not np.any(missing_component):
            continue
        if not (
            2 <= area <= max(1600, int(group.box[2] * group.box[3] * 0.08))
            and 1 <= width <= max(18, int(median_height * 1.2))
            and 3 <= height <= max(24, int(median_height * 1.35))
        ):
            continue
        selected[missing_component] = 255
        components += 1
    if np.any(selected):
        selected = cv2.dilate(selected, np.ones((3, 3), np.uint8), iterations=1)
    return selected, {
        "detached_text_components": int(components),
        "detached_text_pixels": int(np.count_nonzero(selected)),
    }


def _uniform_dark_line_text_mask(img_bgr, group):
    """Cover each OCR line polygon on a proven uniform dark region."""
    metrics = getattr(group, "background_metrics", {}) or {}
    dark_context = _proven_uniform_dark_region(metrics)
    result = np.zeros(img_bgr.shape[:2], dtype=np.uint8)
    if not dark_context:
        return result, {
            "uniform_dark_line_pixels": 0,
            "uniform_dark_line_count": 0,
        }

    line_count = 0
    for line in _cleanup_lines_for_group(group):
        polygon = np.asarray(line.polygon, dtype=np.int32)
        if polygon.shape[0] < 3:
            continue
        line_limit = np.zeros_like(result)
        cv2.fillPoly(line_limit, [polygon], 255)
        # Filling one OCR-line quadrilateral is safe here because the sampled
        # context is almost entirely dark and low-saturation.  It also covers
        # anti-alias pixels that threshold-based masks can leave behind.
        selected = cv2.dilate(
            line_limit,
            np.ones((3, 3), np.uint8),
            iterations=1,
        )
        result = cv2.bitwise_or(result, selected)
        line_count += 1
    flatness = _local_background_flatness(img_bgr, result)
    spread = {
        "uniform_dark_line_ring_luminance_spread":
            flatness["flat_fill_ring_luminance_spread"],
    }
    if not flatness["flat_fill_supported"]:
        return np.zeros_like(result), {
            "uniform_dark_line_pixels": 0,
            "uniform_dark_line_count": 0,
            "uniform_dark_line_rejected": True,
            **spread,
        }
    return result, {
        "uniform_dark_line_pixels": int(np.count_nonzero(result)),
        "uniform_dark_line_count": int(line_count),
        **spread,
    }


def _uniform_light_line_text_mask(img_bgr, group):
    """Cover each OCR line polygon only on a proven uniform light region."""
    metrics = getattr(group, "background_metrics", {}) or {}
    proven_light = _proven_light_cleanup_region(metrics)
    uniform_light = bool(
        metrics.get("strongly_uniform_white")
        or proven_light
        or (
            metrics.get("uniform_light")
            and float(metrics.get("white_pixel_ratio", 0.0)) >= 0.88
            and float(metrics.get("saturation_mean", 255.0)) <= 14.0
        )
    )
    result = np.zeros(img_bgr.shape[:2], dtype=np.uint8)
    if (
        not uniform_light
        or (
            getattr(group, "background_type", "")
            not in {"white_balloon", "narration_box"}
            and not proven_light
        )
    ):
        return result, {
            "uniform_light_line_pixels": 0,
            "uniform_light_line_count": 0,
        }

    line_count = 0
    for line in _cleanup_lines_for_group(group):
        polygon = np.asarray(line.polygon, dtype=np.int32)
        if polygon.shape[0] < 3:
            continue
        line_mask = np.zeros_like(result)
        cv2.fillPoly(line_mask, [polygon], 255)
        line_mask = cv2.dilate(line_mask, np.ones((3, 3), np.uint8), iterations=1)
        result = cv2.bitwise_or(result, line_mask)
        line_count += 1
    flatness = _local_background_flatness(img_bgr, result)
    if not flatness["flat_fill_supported"]:
        # Replacing a whole OCR quadrilateral is a rectangle, not a glyph mask.
        # It is only safe where the surrounding artwork is provably one tone;
        # otherwise the glyph-level mask has to carry the cleanup.
        return np.zeros_like(result), {
            "uniform_light_line_pixels": 0,
            "uniform_light_line_count": 0,
            "uniform_light_line_rejected": True,
            **flatness,
        }
    return result, {
        "uniform_light_line_pixels": int(np.count_nonzero(result)),
        "uniform_light_line_count": int(line_count),
        **flatness,
    }


def _detached_dark_text_components_mask(img_bgr, group, source_mask):
    """Recover dark glyph edges omitted by OCR polygons on uniform white regions."""
    metrics = getattr(group, "background_metrics", {}) or {}
    uniform_light = bool(
        metrics.get("strongly_uniform_white")
        or _proven_light_cleanup_region(metrics)
    )
    empty = np.zeros(source_mask.shape, dtype=np.uint8)
    if not uniform_light or not np.any(source_mask):
        return empty, {
            "detached_dark_text_components": 0,
            "detached_dark_text_pixels": 0,
        }

    line_heights = [line.box[3] for line in group.lines if line.box[3] > 0]
    median_height = float(np.median(line_heights)) if line_heights else group.box[3]
    horizontal_radius = max(12, min(48, int(median_height * 0.8)))
    vertical_radius = max(3, min(12, int(median_height * 0.22)))
    near = cv2.dilate(
        source_mask,
        np.ones(
            (vertical_radius * 2 + 1, horizontal_radius * 2 + 1),
            np.uint8,
        ),
        iterations=1,
    )
    x, y, w, h = _safe_draw_box(group.box, img_bgr.shape, group)
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    dark = np.zeros_like(source_mask)
    dark[y : y + h, x : x + w] = (
        (gray[y : y + h, x : x + w] <= 190)
        & (hsv[y : y + h, x : x + w, 1] <= 80)
    ).astype(np.uint8) * 255
    count, labels, stats, _ = cv2.connectedComponentsWithStats(dark, 8)
    selected = np.zeros_like(source_mask)
    components = 0
    max_area = max(1800, int(group.box[2] * group.box[3] * 0.08))
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        width = int(stats[label, cv2.CC_STAT_WIDTH])
        height = int(stats[label, cv2.CC_STAT_HEIGHT])
        component = labels == label
        missing_component = component & (source_mask == 0)
        if not np.any(missing_component & (near > 0)):
            continue
        if not (
            2 <= area <= max_area
            and 1 <= width <= max(24, int(median_height * 1.6))
            and 3 <= height <= max(28, int(median_height * 1.5))
        ):
            continue
        selected[missing_component] = 255
        components += 1
    if np.any(selected):
        selected = cv2.dilate(selected, np.ones((3, 3), np.uint8), iterations=1)
    return selected, {
        "detached_dark_text_components": int(components),
        "detached_dark_text_pixels": int(np.count_nonzero(selected)),
    }


def _apply_cleanup_mask(current_bgr, original_bgr, group, cleanup_mask, strategy="primary"):
    result = current_bgr.copy()
    if cleanup_mask is None or not np.any(cleanup_mask):
        return result
    if strategy == "caption_overlay":
        return _apply_textured_caption_overlay(result, cleanup_mask)
    draw_box = _safe_draw_box(group.box, original_bgr.shape, group)
    white_region = (
        config.WHITE_BALLOON_FLAT_FILL
        and (
            (
                group.background_type in {"white_balloon", "narration_box"}
                and bool(group.background_metrics.get("uniform_light"))
            )
            or (
                strategy == "source_scoped"
                and _proven_light_cleanup_region(group.background_metrics)
            )
        )
    )
    dark_region = bool(
        group.background_type in {"dark_balloon", "narration_box"}
        and (
            group.background_metrics.get("open_dark_narration")
            or group.background_metrics.get("dark_context")
        )
    ) or bool(
        # A proven uniform dark backdrop reconstructs exactly, so the narrow
        # source-scoped cleanup restores it instead of leaving inpainting ghosts.
        strategy == "source_scoped"
        and _proven_uniform_dark_region(group.background_metrics)
    )
    if (white_region or dark_region) and not _local_background_flatness(
        original_bgr,
        cleanup_mask,
    )["flat_fill_supported"]:
        # The coarse background type proved the region is not *noisy*; it never
        # proved it is one tone.  Without that proof a single colour lands as a
        # visible patch, so reconstruct from the surrounding artwork instead.
        white_region = False
        dark_region = False
    if white_region:
        fill_color = _estimated_white_region_fill_color(
            original_bgr,
            cleanup_mask,
            draw_box,
        )
        result[cleanup_mask > 0] = fill_color
    elif dark_region:
        fill_color = _estimated_background_color(
            original_bgr,
            cleanup_mask,
            draw_box,
        )
        result[cleanup_mask > 0] = fill_color
    elif strategy == "source_scoped":
        return _apply_textured_caption_overlay(result, cleanup_mask)
    else:
        radius = 1 if strategy in {"conservative", "source_scoped"} else 2
        local = cv2.inpaint(result, cleanup_mask, radius, cv2.INPAINT_TELEA)
        result[cleanup_mask > 0] = local[cleanup_mask > 0]
    return result


def _apply_textured_caption_overlay(img_bgr, mask):
    """Remove caption glyphs while retaining the local artwork texture.

    This function deliberately never creates a synthetic dark backing.  A small
    dilation closes antialiased edges and Telea inpainting reconstructs pixels
    from the surrounding illustration.  If the mask is empty the input is
    returned unchanged, allowing the caller to preserve the source safely.
    """
    if mask is None or not np.any(mask):
        return img_bgr.copy()
    inpaint_mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)
    repaired = cv2.inpaint(img_bgr, inpaint_mask, 3, cv2.INPAINT_TELEA)
    result = img_bgr.copy()
    result[mask > 0] = repaired[mask > 0]
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


def _draw_group_translation(img_bgr, group, font_path, strategy="primary"):
    text = group.translation or group.text
    if not text:
        return img_bgr
    if strategy == "caption_overlay":
        return _draw_rotated_caption_translation(img_bgr, group, font_path, text)

    draw_box = _safe_draw_box(group.box, img_bgr.shape, group)
    group.safe_area = tuple(draw_box)
    style = (
        _caption_overlay_text_style(img_bgr, draw_box)
        if strategy == "caption_overlay"
        else _text_style_for_region(img_bgr, draw_box)
    )
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

    preview_font_role = ""
    preview_font_path = ""
    for line in getattr(group, "lines", []) or []:
        metadata = getattr(line, "metadata", None) or {}
        candidate_role = str(metadata.get("preview_font_role") or "").strip().lower()
        if candidate_role in {"regular", "bold", "shout", "decorative"}:
            preview_font_role = candidate_role
            preview_font_path = str(metadata.get("preview_font_path") or "").strip()
            break

    if preview_font_role:
        font_role = preview_font_role
    elif style.name == "decorative_purple":
        font_role = "decorative"
    elif "!" in group.text:
        font_role = "shout"
    else:
        font_role = "regular"

    lines = []
    spacing = 2
    text_bbox = (0, 0, 0, 0)

    overflow_ratio = 1.0
    prefer_preview_role = bool(preview_font_role and not preview_font_path)
    while font_size >= config.MIN_FONT_SIZE:
        font = get_font(preview_font_path or font_path, font_size, role=font_role,
                        prefer_role=prefer_preview_role, text=text)
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
    font = get_font(preview_font_path or font_path, font_size, role=font_role,
                    prefer_role=prefer_preview_role, text=text)
    group.font_runtime_validation = dict(getattr(font, "tradutor_font_runtime", {}) or {})
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


def _draw_rotated_caption_translation(img_bgr, group, font_path, text):
    """Render translated caption lines along the original OCR line geometry."""
    source_lines = sorted(
        group.lines,
        key=lambda line: float(np.mean(np.asarray(line.polygon)[:, 1])),
    )
    words = str(text).split()
    if not source_lines or not words:
        group.text_overflow_ratio = 1.0
        group.translation_box = None
        return img_bgr

    region_count = min(len(source_lines), len(words))
    source_lines = source_lines[:region_count]
    geometries = [_caption_line_geometry(line) for line in source_lines]
    role = "shout" if "!" in group.text or "?" in group.text else "regular"
    fitted = _fit_caption_lines(words, geometries, font_path, role)
    if not fitted:
        group.text_overflow_ratio = 1.0
        group.translation_box = None
        return img_bgr

    caption_lines, font, font_size = fitted
    canvas = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)).convert("RGBA")
    paste_boxes = []
    for caption, geometry in zip(caption_lines, geometries):
        probe = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        probe_draw = ImageDraw.Draw(probe)
        bbox = probe_draw.textbbox(
            (0, 0),
            caption,
            font=font,
            stroke_width=2,
        )
        tw = max(1, bbox[2] - bbox[0])
        th = max(1, bbox[3] - bbox[1])
        pad = 4
        tile = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
        tile_draw = ImageDraw.Draw(tile)
        tile_draw.text(
            (pad - bbox[0], pad - bbox[1]),
            caption,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=2,
            stroke_fill=(10, 10, 16, 255),
        )
        rotated = tile.rotate(
            geometry["pillow_angle"],
            resample=Image.Resampling.BICUBIC,
            expand=True,
        )
        cx, cy = geometry["center"]
        px = int(round(cx - rotated.width / 2))
        py = int(round(cy - rotated.height / 2))
        canvas.alpha_composite(rotated, (px, py))
        paste_boxes.append((px, py, rotated.width, rotated.height))

    translation_box = _union_boxes(paste_boxes)
    safe_area = _safe_draw_box(group.box, img_bgr.shape, group)
    overflow = _box_overflow_ratio(translation_box, safe_area)
    group.safe_area = tuple(safe_area)
    group.draw_box = tuple(safe_area)
    group.translation_box = tuple(translation_box)
    group.font_size = int(font_size)
    group.text_overflow_ratio = float(overflow)
    group.color_name = "caption_overlay"
    group.region_brightness = float(np.mean(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)))
    group.region_saturation = 0.0
    group.region_hue = 0.0
    if config.REJECT_TEXT_OVERFLOW and overflow > config.MAX_TEXT_OVERFLOW_RATIO:
        group.translation_valid = False
        group.translation_validation_reason = "caption_translation_outside_safe_area"
        return img_bgr
    return cv2.cvtColor(np.asarray(canvas.convert("RGB")), cv2.COLOR_RGB2BGR)


def _caption_line_geometry(line):
    polygon = np.asarray(line.polygon, dtype=np.float32)
    center = tuple(np.mean(polygon, axis=0).tolist())
    if polygon.shape[0] >= 4:
        top = float(np.linalg.norm(polygon[1] - polygon[0]))
        bottom = float(np.linalg.norm(polygon[2] - polygon[3]))
        left = float(np.linalg.norm(polygon[3] - polygon[0]))
        right = float(np.linalg.norm(polygon[2] - polygon[1]))
        width = max(top, bottom)
        height = max(left, right)
        dx, dy = polygon[1] - polygon[0]
        pillow_angle = -math.degrees(math.atan2(float(dy), float(dx)))
    else:
        _, _, width, height = line.box
        pillow_angle = 0.0
    return {
        "center": center,
        "width": max(12.0, width - 8.0),
        "height": max(10.0, height - 6.0),
        "pillow_angle": pillow_angle,
    }


def _fit_caption_lines(words, geometries, font_path, role):
    line_count = min(len(words), len(geometries))
    geometries = geometries[:line_count]
    max_size = min(
        config.MAX_FONT_SIZE,
        max(
            config.MIN_FONT_SIZE,
            int(np.median([item["height"] for item in geometries]) * 0.72),
        ),
    )
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8), "white"))
    for size in range(max_size, config.MIN_FONT_SIZE - 1, -1):
        font = get_font(font_path, size, role=role)
        partition = _partition_caption_words(
            probe,
            words,
            font,
            [item["width"] for item in geometries],
        )
        if not partition:
            continue
        fits = True
        for caption, geometry in zip(partition, geometries):
            bbox = probe.textbbox((0, 0), caption, font=font, stroke_width=2)
            if (
                bbox[2] - bbox[0] > geometry["width"]
                or bbox[3] - bbox[1] > geometry["height"]
            ):
                fits = False
                break
        if fits:
            return partition, font, size
    return None


def _partition_caption_words(draw, words, font, widths):
    line_count = min(len(words), len(widths))
    if line_count <= 0:
        return None
    states = {(0, 0): (0.0, [])}
    for line_index in range(line_count):
        next_states = {}
        remaining_lines = line_count - line_index - 1
        for (used_lines, start), (score, parts) in states.items():
            if used_lines != line_index:
                continue
            max_end = len(words) - remaining_lines
            for end in range(start + 1, max_end + 1):
                caption = " ".join(words[start:end])
                measured = _text_width(draw, caption, font)
                ratio = measured / max(1.0, widths[line_index])
                overflow_penalty = max(0.0, ratio - 1.0) * 100.0
                candidate_score = score + (ratio - 0.82) ** 2 + overflow_penalty
                key = (line_index + 1, end)
                existing = next_states.get(key)
                if existing is None or candidate_score < existing[0]:
                    next_states[key] = (candidate_score, parts + [caption])
        states = next_states
    final = states.get((line_count, len(words)))
    return final[1] if final else None


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
    background_metrics = (
        getattr(group, "background_metrics", {}) or {}
        if group is not None
        else {}
    )
    dark_context = bool(
        background_metrics.get("dark_context")
        or (
            float(background_metrics.get("context_dark_pixel_ratio", 0.0)) >= 0.90
            and float(background_metrics.get("context_saturation_mean", 255.0)) <= 18.0
        )
    )
    uniform_light = bool(
        background_metrics.get("strongly_uniform_white")
        or (
            background_metrics.get("uniform_light")
            and float(background_metrics.get("white_pixel_ratio", 0.0)) >= 0.88
            and float(background_metrics.get("saturation_mean", 255.0)) <= 14.0
        )
    )
    if group and dark_context:
        # A detector can omit a narrow detached glyph (for example a leading
        # pronoun) while still locating the rest of a light-on-dark line.  The
        # larger horizontal search window is used only to find components; the
        # cleanup mask remains component-based and tightly bounded.
        pad_x = max(12, min(28, int(w * 0.12)))
        pad_y = max(4, min(10, int(h * 0.08)))
    elif group and uniform_light:
        pad_x = max(12, min(28, int(w * 0.12)))
        pad_y = max(4, min(10, int(h * 0.08)))
    elif group and group.inside_balloon_like_region:
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


def _ascii_folded_tokens(text):
    """Accent-folded upper-case word tokens, in reading order."""
    return re.findall(r"[A-Z']+", _ascii_fold(text or "").upper())


def _ocr_shape_similarity(observed_joined, reference_joined):
    """Similarity of two accent-folded, space-free renderings of a text.

    Dropping whitespace absorbs the space insertions and merges that this OCR
    pass produces; accent folding absorbs lost diacritics; the ratio absorbs
    the remaining character-level noise.
    """

    if not observed_joined or not reference_joined:
        return 0.0
    return difflib.SequenceMatcher(
        None,
        observed_joined,
        reference_joined,
    ).ratio()


def _uncovered_source_text_evidence(original_bgr, rendered_bgr, group, cleanup_mask):
    """Source glyph pixels the cleanup never touched and the render left intact.

    This is the positive half of the physical proof.  It reads the *boxes* the
    group's OCR lines own - not their polygons - because a polygon is exactly
    what a stale recovery geometry gets wrong, and a gate that trusts the same
    geometry the mask was built from cannot catch the mask being wrong.

    A pixel counts as surviving source only when all three hold: it looks like
    source text in the original, the cleaning mask never covered it, and the
    render left it unchanged.  Anything the mask covered was flattened, so a
    bright pixel there is the new translation rather than the old source.
    """

    empty = {
        "source_text_pixels": 0,
        "source_owned_geometry_uncovered_pixels": 0,
        "largest_unmasked_source_component": 0,
        "source_owned_geometry_coverage": 1.0,
        "source_geometry_uncovered_line_ids": [],
        "uncovered_source_text_pixels": 0,
        "largest_uncovered_source_component": 0,
        "source_text_coverage": 1.0,
        "uncovered_source_line_ids": [],
        "measured": False,
    }
    if original_bgr is None or cleanup_mask is None:
        return empty
    if original_bgr.shape[:2] != rendered_bgr.shape[:2]:
        return empty
    if original_bgr.shape[:2] != cleanup_mask.shape[:2]:
        return empty

    original_gray = cv2.cvtColor(original_bgr, cv2.COLOR_BGR2GRAY).astype(np.int16)
    rendered_gray = cv2.cvtColor(rendered_bgr, cv2.COLOR_BGR2GRAY).astype(np.int16)
    height, width = original_gray.shape
    total = 0
    unmasked_total = 0
    uncovered_total = 0
    largest_unmasked = 0
    largest = 0
    unmasked_lines = []
    uncovered_lines = []
    measured = False
    for line in _cleanup_lines_for_group(group):
        x, y, w, h = [int(value) for value in line.box]
        x1 = max(0, x)
        y1 = max(0, y)
        x2 = min(width, x + max(1, w))
        y2 = min(height, y + max(1, h))
        if x2 - x1 < 4 or y2 - y1 < 4:
            continue
        roi = original_gray[y1:y2, x1:x2]
        # Same foreground rule the component mask uses, so "source text" means
        # the same thing to the gate and to the cleanup it is judging.
        median = float(np.median(roi))
        contrast = max(
            8.0,
            float(np.percentile(roi, 80)) - float(np.percentile(roi, 20)),
        )
        if median >= 138:
            foreground = roi <= max(25.0, min(205.0, median - max(16.0, contrast * 0.28)))
        elif median <= 118:
            foreground = roi >= min(235.0, max(48.0, median + max(16.0, contrast * 0.28)))
        else:
            continue
        line_total = int(np.count_nonzero(foreground))
        if line_total < 24:
            continue
        measured = True
        unchanged = (
            np.abs(roi - rendered_gray[y1:y2, x1:x2]) <= config.VISUAL_DIFF_THRESHOLD
        )
        unmasked = (foreground & (cleanup_mask[y1:y2, x1:x2] == 0)).astype(np.uint8) * 255
        unmasked_count, _unmasked_labels, unmasked_stats, _unmasked_centroids = (
            cv2.connectedComponentsWithStats(unmasked, 8)
        )
        line_largest_unmasked = max(
            (
                int(unmasked_stats[label, cv2.CC_STAT_AREA])
                for label in range(1, unmasked_count)
            ),
            default=0,
        )
        surviving = (
            foreground & unchanged & (cleanup_mask[y1:y2, x1:x2] == 0)
        ).astype(np.uint8) * 255
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
            surviving, 8
        )
        line_largest = max(
            (int(stats[label, cv2.CC_STAT_AREA]) for label in range(1, count)),
            default=0,
        )
        total += line_total
        unmasked_total += int(np.count_nonzero(unmasked))
        uncovered_total += int(np.count_nonzero(surviving))
        largest_unmasked = max(largest_unmasked, line_largest_unmasked)
        largest = max(largest, line_largest)
        line_id = str((getattr(line, "metadata", None) or {}).get("ocr_line_id") or "")
        if line_largest_unmasked >= _glyph_scale_component_area(h):
            unmasked_lines.append(line_id)
        if line_largest >= _glyph_scale_component_area(h):
            uncovered_lines.append(
                line_id
            )
    if not measured:
        return empty
    return {
        "source_text_pixels": int(total),
        "source_owned_geometry_uncovered_pixels": int(unmasked_total),
        "largest_unmasked_source_component": int(largest_unmasked),
        "source_owned_geometry_coverage": round(
            float(1.0 - unmasked_total / max(1, total)), 4
        ),
        "source_geometry_uncovered_line_ids": unmasked_lines,
        "uncovered_source_text_pixels": int(uncovered_total),
        "largest_uncovered_source_component": int(largest),
        "source_text_coverage": round(
            float(1.0 - uncovered_total / max(1, total)), 4
        ),
        "uncovered_source_line_ids": uncovered_lines,
        "measured": True,
    }


def _glyph_scale_component_area(line_height):
    """Smallest surviving blob that is a glyph rather than anti-alias debris.

    Scaled to the line, because "a letter" is a fraction of the line box on any
    page.  Below it lives the speckle a correct cleanup always leaves where the
    new text happens to land on old strokes; above it lives a readable word.
    """

    return max(24, int(round(max(1.0, float(line_height)) ** 2 * 0.05)))


def _source_removal_incomplete(group, removal):
    """True when owned source glyphs demonstrably survived the cleanup.

    Ordinary source dialogue whose glyphs the cleaning mask never reached is
    still on the page, whatever any OCR pass managed to read back.  Unreadable
    source held for manual review keeps its own taxonomy and is not judged here.
    """

    if not removal.get("measured"):
        return False
    if group.translation_final_reason == OCR_UNINTELLIGIBLE_SOURCE_REASON:
        return False
    line_height = max(
        (line.box[3] for line in _cleanup_lines_for_group(group)),
        default=0,
    )
    threshold = _glyph_scale_component_area(line_height)
    return (
        removal.get("largest_uncovered_source_component", 0) >= threshold
        or removal.get("largest_unmasked_source_component", 0) >= threshold
    )


def _post_render_source_text_check(
    rendered_bgr,
    group,
    page_index=None,
    original_bgr=None,
    cleanup_mask=None,
    removal=None,
):
    """Decide whether source text is still physically visible after cleanup.

    Two independent kinds of evidence answer that, and only one of them used to
    exist.  The post-render OCR pass is *positive* evidence: a token it reads
    back is residual.  Its silence is not proof of absence - it merges glyphs
    into neighbouring words and misses what it cannot segment - so absence has
    to be carried by the cleanup itself: the mask covered the source text, so
    the source text is gone.  When neither holds the answer is inconclusive,
    and inconclusive is never collapsed into a pass.
    """

    preserved_names = {
        token
        for name in group.detected_proper_names
        for token in re.findall(r"[A-Z']+", _ascii_fold(name).upper())
    }
    intended_translation_tokens = set(
        re.findall(
            r"[A-Z']+",
            _ascii_fold(group.translation or "").upper(),
        )
    )
    # ``group.text`` is the representation that may already have lost part of the
    # source, so it cannot be the only expectation: a token dropped upstream would
    # be excused precisely because it went missing.  The immutable OCR provenance
    # the group owns is the source of truth; the group text only adds to it.
    provenance_tokens, completeness = source_completeness.expected_physical_tokens(
        group
    )
    # Unreadable source held for manual review is not ordinary English residual and
    # must keep its own taxonomy, so it never enters the expectation here.
    if group.translation_final_reason == OCR_UNINTELLIGIBLE_SOURCE_REASON:
        provenance_tokens = set(_ascii_folded_tokens(group.text))
    excluded_preserved = sorted(
        token
        for token in provenance_tokens
        if token in preserved_names or token in SFX_WORDS
    )
    source_tokens = {
        token
        for token in provenance_tokens
        if len(token) >= 3
        and token not in preserved_names
        and token not in SFX_WORDS
        and token not in intended_translation_tokens
    }
    # The source geometry the provenance recorded can extend past a region that was
    # itself truncated.  Searching only the truncated region is how source pixels
    # stayed invisible to this pass; the union stays group-owned, so a neighbouring
    # balloon never widens it.
    region = source_completeness.union_box(
        [
            group.safe_area or group.draw_box or group.box,
            completeness.get("source_bbox"),
        ]
    ) or tuple(group.safe_area or group.draw_box or group.box)
    x, y, w, h = region
    pad = min(8, config.MAX_MASK_EXPANSION + 2)
    x1 = max(0, int(x) - pad)
    y1 = max(0, int(y) - pad)
    x2 = min(rendered_bgr.shape[1], int(x + w) + pad)
    y2 = min(rendered_bgr.shape[0], int(y + h) + pad)
    crop = rendered_bgr[y1:y2, x1:x2]
    if crop.size == 0:
        return {
            "checked": False,
            "passed": False,
            "reason": "empty_post_render_crop",
        }

    try:
        engine = OCREngine("en", engine="rapidocr", fallback_engine="")
        lines = engine._detect_with_rapidocr(crop)
    except Exception as exc:
        return {
            "checked": False,
            "passed": False,
            "reason": f"post_render_ocr_unavailable:{type(exc).__name__}",
        }

    final_text = clean_ocr_text(" ".join(line.text for line in lines))
    final_tokens = set(
        re.findall(r"[A-Z']+", _ascii_fold(final_text).upper())
    )
    residual = set(source_tokens & final_tokens)
    language_valid, language_reason = validate_translation_text(
        "",
        final_text,
        classification=group.classification,
        allowed_proper_names=group.detected_proper_names,
    )
    source_language_detected = not language_valid and language_reason.startswith(
        (
            "mixed_language_tokens:",
            "residual_english_token:",
            "residual_inflected_english:",
            "untranslated_english_text",
            "untranslated_single_english_token",
        )
    )
    # Three-way evidence: the source text, the translation that was rendered and
    # the text this independent pass observed.  A token the language validator
    # flags is only forgiven when the *expected* translation explains it as OCR
    # noise (lost accent, inserted or dropped space) and the source text does
    # not.  The observed output is never replaced by the expected string: it
    # still has to carry evidence of the translation, and any token the
    # expectation cannot explain keeps failing.
    expected_sequence = _ascii_folded_tokens(group.translation)
    expected_joined = "".join(expected_sequence)
    observed_joined = "".join(_ascii_folded_tokens(final_text))
    source_joined = "".join(_ascii_folded_tokens(group.text))
    expected_similarity = _ocr_shape_similarity(observed_joined, expected_joined)
    source_similarity = _ocr_shape_similarity(observed_joined, source_joined)
    # The observed output as a whole reads as the expected translation, and
    # reads less like the source than like the translation.  That is what makes
    # the flagged tokens OCR noise rather than surviving source text.
    flagged_tokens = {
        token
        for token in language_reason.partition(":")[2].split(",")
        if token
    }
    # Forgiveness needs a positive reason, not just the absence of one.  A token
    # is OCR noise when the expected translation itself contains those letters -
    # which is what a mid-word split such as ANDANDO -> "AND ANDO" produces.  A
    # token the source provenance expected is never noise, however short it is,
    # however uncertain the read was, and however well the line as a whole
    # matches the translation.  ``CONTROL`` sits inside ``CONTROLE``; only the
    # provenance can tell the surviving source word from the rendered one.
    forgiven_ocr_noise = sorted(
        token
        for token in flagged_tokens
        if token not in provenance_tokens and token in expected_joined
    )
    rendered_matches_expected = bool(
        expected_joined
        and expected_similarity >= (0.95 if len(expected_joined) < 4 else 0.90)
        and expected_similarity > source_similarity
        and (
            len(expected_joined) >= 4
            or (
                forgiven_ocr_noise
                and len(forgiven_ocr_noise) == len(flagged_tokens)
                and observed_joined == expected_joined
            )
        )
    )
    if (
        source_language_detected
        and rendered_matches_expected
        and flagged_tokens
        and len(forgiven_ocr_noise) == len(flagged_tokens)
    ):
        source_language_detected = False

    if source_language_detected:
        for info in _translation_token_infos(final_text):
            token = info["token"]
            if (
                len(token) >= 2
                and token not in preserved_names
                and token not in intended_translation_tokens
                and token not in PORTUGUESE_MARKERS
                and token not in SFX_WORDS
            ):
                residual.add(token)
    residual = sorted(residual)

    # The rendered translation must actually be visible.  Correct text in memory
    # is not evidence that it reached the page.
    expected_evidence = [token for token in expected_sequence if len(token) >= 3]
    target_text_found = not expected_evidence or any(
        token in final_tokens or token in observed_joined
        for token in expected_evidence
    )

    if removal is None:
        removal = _uncovered_source_text_evidence(
            original_bgr,
            rendered_bgr,
            group,
            cleanup_mask,
        )
    removal_incomplete = _source_removal_incomplete(group, removal)

    passed = (
        not residual
        and not source_language_detected
        and target_text_found
        and not removal_incomplete
    )
    if passed:
        reason = "ok"
    elif residual or source_language_detected:
        reason = (
            "source_language_detected_after_render"
            if source_language_detected
            else "source_tokens_detected_after_render"
        )
    elif removal_incomplete:
        reason = "uncovered_source_text_evidence"
    elif not observed_joined:
        reason = "translated_text_missing_after_render"
    else:
        # Something was rendered, but it matches neither the expected
        # translation nor the source text.  Fail closed for manual review
        # instead of guessing which of the two it is.
        reason = "post_render_ocr_inconclusive"
    if residual or source_language_detected:
        physical_decision = "residual_detected"
    elif passed:
        physical_decision = "pass"
    else:
        physical_decision = "review"
    return {
        "checked": True,
        "passed": passed,
        "reason": reason,
        "physical_decision": physical_decision,
        # Why absence is believed, in the gate's own words: either the cleanup
        # provably covered the owned source text, or it did not and this is not
        # a pass.  ``post_render_ocr_only`` is the historical weak state and is
        # never enough on its own to clear ordinary dialogue.
        "removal_evidence": (
            "source_text_coverage_complete"
            if removal["measured"] and not removal_incomplete
            else "source_text_coverage_incomplete"
            if removal["measured"]
            else "post_render_ocr_only"
        ),
        **{key: value for key, value in removal.items() if key != "measured"},
        "validator_reason": language_reason,
        "source_tokens_checked": sorted(source_tokens),
        "detected_text": final_text,
        "residual_source_tokens": residual,
        "expected_translation_tokens": expected_sequence,
        "expected_shape_similarity": round(expected_similarity, 4),
        "source_shape_similarity": round(source_similarity, 4),
        "rendered_matches_expected": bool(rendered_matches_expected),
        "forgiven_ocr_noise_tokens": (
            forgiven_ocr_noise if rendered_matches_expected else []
        ),
        "target_text_found": bool(target_text_found),
        # Why each token was expected: the basis, the exact source lines behind it
        # and the region actually searched.  Without this a residual result cannot
        # be argued with, only believed.
        "expected_source_basis": completeness.get(
            "expected_source_basis", source_completeness.EXPECTED_SOURCE_BASIS
        ),
        "source_completeness_status": completeness.get("status", ""),
        "source_line_ids": completeness.get("source_line_ids", []),
        "expected_tokens": sorted(source_tokens),
        "detected_residual_tokens": residual,
        "excluded_preserved_tokens": excluded_preserved,
        "searched_region": [int(part) for part in region],
        "page": page_index,
    }


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
    largest_changed_border_component = 0
    if group is not None and group.safe_area:
        original_gray = cv2.cvtColor(original_bgr, cv2.COLOR_BGR2GRAY)
        original_edges = cv2.Canny(original_gray, 55, 155) > 0
        text_exclusion = _build_text_mask(
            original_bgr.shape,
            [group],
            padding=max(
                6,
                min(
                    12,
                    int(config.MAX_MASK_EXPANSION) + 2,
                ),
            ),
        ) > 0
        # A text safe-area is not a balloon contour.  Protect real structural
        # edges that were changed inside the allowed mask, excluding source
        # glyphs and their anti-alias halo.  Only the perimeter band of the
        # safe area is treated as balloon/box border; large hand-drawn letters
        # or tiny character icons inside a balloon must not be mistaken for the
        # outer contour.
        sx, sy, sw, sh = [int(value) for value in group.safe_area]
        safe_band = np.zeros(original_bgr.shape[:2], dtype=np.uint8)
        sx1 = max(0, sx)
        sy1 = max(0, sy)
        sx2 = min(original_bgr.shape[1], sx + max(1, sw))
        sy2 = min(original_bgr.shape[0], sy + max(1, sh))
        if sx2 > sx1 and sy2 > sy1:
            safe_band[sy1:sy2, sx1:sx2] = 255
            band_width = max(8, min(18, int(min(sw, sh) * 0.06)))
            kernel_size = max(3, band_width * 2 + 1)
            inner = cv2.erode(
                safe_band,
                np.ones((kernel_size, kernel_size), dtype=np.uint8),
                iterations=1,
            )
            perimeter_band = (safe_band > 0) & ~(inner > 0)
        else:
            perimeter_band = allowed
        protected_border_edges = (
            allowed & perimeter_band & original_edges & ~text_exclusion
        )
        if np.any(protected_border_edges):
            border_change_ratio = float(np.mean(changed[protected_border_edges]))
            changed_border = (
                protected_border_edges & changed
            ).astype(np.uint8) * 255
            border_count, _, border_stats, _ = cv2.connectedComponentsWithStats(
                changed_border,
                8,
            )
            largest_changed_border_component = max(
                (
                    int(border_stats[label, cv2.CC_STAT_AREA])
                    for label in range(1, border_count)
                ),
                default=0,
            )

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
        # ``broad_rectangular_mask`` is measured against the source text bbox, so
        # a mask pinned to the owned source lines is always "broad" by that
        # metric.  The collateral-damage reasons above still police this path.
        and metrics.get("strategy") != "source_scoped"
        and metrics.get("background_type")
        not in {"white_balloon", "dark_balloon", "narration_box"}
    ):
        reasons.append("broad_rectangular_or_polygonal_mask")
    if metrics.get("white_patch_rejected"):
        reasons.append("large_white_patch_on_nonwhite_background")
    if config.REJECT_TEXT_OVERFLOW and overflow_ratio > config.MAX_TEXT_OVERFLOW_RATIO:
        reasons.append("translation_outside_safe_area")
    structural_border_threshold = max(
        72,
        int(min(group.box[2], group.box[3]) * 0.35) if group is not None else 72,
    )
    if (
        config.REJECT_BALLOON_BORDER_DAMAGE
        and border_change_ratio > 0.12
        and largest_changed_border_component > structural_border_threshold
    ):
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
        "largest_changed_balloon_border_component_area": int(
            largest_changed_border_component
        ),
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


def _caption_overlay_text_style(img_bgr, box):
    """High-contrast style used only on the non-destructive artwork overlay."""
    x, y, w, h = box
    roi = img_bgr[y : y + h, x : x + w]
    if roi.size:
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        brightness = float(np.mean(gray))
        saturation = float(np.mean(hsv[:, :, 1]))
        hue = float(np.median(hsv[:, :, 0]))
    else:
        brightness, saturation, hue = 0.0, 0.0, 0.0
    return TextStyle(
        name="caption_overlay",
        fill=(255, 255, 255),
        stroke_fill=(12, 12, 18),
        stroke_width=2,
        shadow_fill=(10, 10, 16),
        shadow_offset=(2, 2),
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
                "classification_reason": "line_ignored_before_grouping",
                "classification_confidence": 0.0,
                "classification_evidence": {},
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
        _ensure_translation_terminal_state(group)
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
                "font_runtime_validation": dict(group.font_runtime_validation),
                "region_brightness": round(group.region_brightness, 2),
                "region_saturation": round(group.region_saturation, 2),
                "region_hue": round(group.region_hue, 2),
                "background_type": group.background_type,
                "background_metrics": dict(group.background_metrics),
                "classification": group.classification,
                "classification_reason": group.classification_reason,
                "classification_confidence": round(
                    group.classification_confidence,
                    4,
                ),
                "classification_evidence": dict(group.classification_evidence),
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
                "raw_provider_candidate": group.raw_provider_candidate,
                "raw_provider_candidate_class": group.raw_provider_candidate_class,
                "retry_candidate": group.retry_candidate,
                "retry_candidate_class": group.retry_candidate_class,
                "translation_candidate": group.translation_candidate,
                "translation_final_state": group.translation_final_state,
                "translation_final_reason": group.translation_final_reason,
                "translation_quality_impact": group.translation_quality_impact,
                "translation_unresolved": bool(group.translation_unresolved),
                "translation_attempts_exhausted": bool(
                    group.translation_attempts_exhausted
                ),
                "trusted_translation_missing": bool(group.trusted_translation_missing),
                "source_fallback_prevented": bool(group.source_fallback_prevented),
                "preserved_original": bool(group.preserved_original),
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
                "source_completeness": dict(group.source_completeness),
                "source_completeness_status": str(
                    group.source_completeness.get("status", "")
                ),
                "visual_validation": group.visual_validation,
                "visual_attempts": list(group.visual_attempts),
                "mask_metrics": dict(group.mask_metrics),
                "manual_review_required": bool(group.manual_review_required),
                "art_reconstruction_status": str(group.art_reconstruction_status),
                "art_reconstruction_reason": str(group.art_reconstruction_reason),
                "detected_proper_names": list(group.detected_proper_names),
                "preserve_as_name": bool(group.preserve_as_name),
                "quality_evidence": dict(group.quality_evidence),
                "naturalization": {
                    "eligible": bool(group.naturalization_eligible),
                    "attempted": bool(group.naturalization_attempted),
                    "accepted": bool(group.naturalization_accepted),
                    "status": group.naturalization_status,
                    "candidate": group.naturalization_candidate,
                    "rejected_reason": group.naturalization_rejected_reason,
                    "selected_version": group.naturalization_selected_version,
                    "context": dict(group.naturalization_context),
                },
                "translated": group.translation_final_state == "translated",
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
        "translated_group_count": sum(
            1
            for group in groups
            if group.translation_final_state == "translated"
        ),
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
