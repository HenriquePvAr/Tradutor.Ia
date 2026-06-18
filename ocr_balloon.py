import json
import os
import re
import unicodedata
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import config
from ocr_engine import OCREngine, OCRLine, clean_ocr_text

try:
    from config import FONT_PATH, TEMP_FOLDER, TEMP_OUT
except Exception:
    FONT_PATH = None
    TEMP_FOLDER = "capitulo_temp"
    TEMP_OUT = TEMP_FOLDER + "_out"


SHORT_REAL_TEXTS = {
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
    "HA",
    "HA!",
}

SFX_WORDS = {
    "BANG",
    "BOOM",
    "BUMP",
    "CLANG",
    "CRASH",
    "GONG",
    "GRR",
    "GULP",
    "HISS",
    "KNOCK",
    "SLAM",
    "SNIFF",
    "SNIFFLE",
    "SOB",
    "THUD",
    "UGH",
    "WHAM",
    "WHOOSH",
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
    classification: str = "unknown"
    inside_balloon_like_region: bool = False
    inside_narration_box_like_region: bool = False
    main_text_score: float = 0.0
    angle_degrees: float = 0.0
    near_image_edge: bool = False
    alignment_score: float = 0.0

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
    candidates = [_candidate_from_line(line, original.shape) for line in raw_lines]
    usable_lines = [candidate.line for candidate in candidates if not candidate.ignored]
    groups = _group_lines(usable_lines)
    _filter_groups(groups, original.shape)
    _classify_groups(groups, original)

    valid_groups = [group for group in groups if _should_translate_group(group)]
    translations = _translate_texts(translator, [group.text for group in valid_groups])

    for group, translation in zip(valid_groups, translations):
        translated = clean_ocr_text(translation) or group.text
        group.translation = _match_source_case(group.text, translated)
        group.sent_to_translation = True

    text_mask = _build_text_mask(original.shape, valid_groups)
    inpainted = _remove_text_with_mask(original, text_mask)
    final = inpainted.copy()

    for group in valid_groups:
        final = _draw_group_translation(final, group, font_path)
        group.redrawn = True

    debug_data = _debug_payload(image_path, raw_lines, candidates, groups)

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

    alpha_ratio = len(useful_chars) / max(1, len(re.sub(r"\s+", "", text)))
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


def _line_belongs_to_group(line, group):
    gx, gy, gw, gh = group.box
    lx, ly, lw, lh = line.box
    group_bottom = gy + gh
    vertical_gap = ly - group_bottom
    avg_height = max(lh, gh / max(1, len(group.lines)))

    if vertical_gap < -max(lh, avg_height) * 0.8:
        return False

    if vertical_gap > max(18, avg_height * 1.85):
        return False

    overlap = max(0, min(gx + gw, lx + lw) - max(gx, lx))
    min_width = max(1, min(gw, lw))
    center_distance = abs((gx + gw / 2) - (lx + lw / 2))

    if overlap / min_width >= 0.18:
        return True

    return center_distance <= max(gw, lw) * 0.65


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

        score = 0.0
        score += 0.38 if narration_like else 0.0
        score += 0.32 if balloon_like else 0.0
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

        if is_known_sfx and strongly_styled and not narration_like:
            group.inside_balloon_like_region = False
            group.inside_narration_box_like_region = False
            group.classification = "sfx"
        elif narration_like and len(group.lines) == 1 and len(words) <= 3:
            group.classification = "speech"
        elif narration_like:
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


def _build_text_mask(image_shape, groups):
    mask = np.zeros(image_shape[:2], dtype=np.uint8)

    for group in groups:
        for line in group.lines:
            poly = _expand_poly(line.polygon, image_shape, padding=_mask_padding(line.box))
            cv2.fillPoly(mask, [poly.astype(np.int32)], 255)

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
    return max(2, min(9, int(h * 0.22)))


def _remove_text_with_mask(img_bgr, mask):
    if mask is None or not np.any(mask):
        return img_bgr.copy()

    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.dilate(mask, kernel, iterations=1)
    return cv2.inpaint(img_bgr, mask, 3, cv2.INPAINT_TELEA)


def _draw_group_translation(img_bgr, group, font_path):
    text = group.translation or group.text
    if not text:
        return img_bgr

    draw_box = _expanded_draw_box(group.box, img_bgr.shape)
    style = _text_style_for_region(img_bgr, draw_box)
    group.color_name = style.name
    group.region_brightness = style.brightness
    group.region_saturation = style.saturation
    group.region_hue = style.hue

    pil_img = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil_img)
    x, y, w, h = draw_box

    inset_x = max(6, min(24, int(w * 0.07)))
    inset_y = max(4, min(16, int(h * 0.1)))
    content_x = x + inset_x
    content_y = y + inset_y
    content_w = max(12, w - inset_x * 2)
    content_h = max(12, h - inset_y * 2)

    source_line_heights = [line.box[3] for line in group.lines if line.box[3] > 0]
    source_height = float(np.median(source_line_heights)) if source_line_heights else h
    size_scale = 0.72 if style.name == "decorative_purple" else 0.78
    font_size = min(34, max(10, int(source_height * size_scale)))
    if len(text) > max(1, len(group.text)) * 1.2:
        font_size = max(10, int(font_size * 0.92))

    if style.name == "decorative_purple":
        font_role = "decorative"
    elif "!" in group.text:
        font_role = "shout"
    else:
        font_role = "regular"

    lines = []
    spacing = 2
    text_bbox = (0, 0, 0, 0)

    while font_size >= 8:
        font = get_font(font_path, font_size, role=font_role)
        spacing = max(1, int(font_size * 0.1))
        wrap_width = int(content_w * 0.8) if style.name == "decorative_purple" else content_w
        lines = _wrap_text(draw, text, font, max_width=max(12, wrap_width))
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
        if text_h <= content_h and text_w <= content_w:
            break
        font_size -= 1

    font = get_font(font_path, font_size, role=font_role)
    group.font_size = font_size
    text_block = "\n".join(lines)
    text_w = text_bbox[2] - text_bbox[0]
    text_h = text_bbox[3] - text_bbox[1]
    draw_x = content_x + (content_w - text_w) / 2 - text_bbox[0]
    draw_y = content_y + (content_h - text_h) / 2 - text_bbox[1]

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


def _wrap_text(draw, text, font, max_width):
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
            chunks = _split_long_word(draw, word, font, max_width)
            lines.extend(chunks[:-1])
            current = chunks[-1]
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
):
    os.makedirs(debug_folder, exist_ok=True)
    prefix = os.path.join(debug_folder, f"page_{page_index:03}")

    cv2.imwrite(prefix + "_original.png", original)
    cv2.imwrite(prefix + "_ocr_boxes.png", _draw_ocr_debug(original, raw_lines, candidates, groups))
    cv2.imwrite(prefix + "_classified_boxes.png", _draw_classified_debug(original, groups))
    cv2.imwrite(prefix + "_text_mask.png", text_mask)
    cv2.imwrite(prefix + "_inpainted.png", inpainted)
    cv2.imwrite(prefix + "_final.png", final)
    cv2.imwrite(prefix + "_compare.png", _compare_image(original, final))

    with open(prefix + "_ocr.json", "w", encoding="utf-8") as file:
        json.dump(debug_data, file, ensure_ascii=False, indent=2)


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
            "text_color": "",
            "classification": "unknown",
            "inside_balloon_like_region": False,
            "inside_narration_box_like_region": False,
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
                "text_color": group.color_name,
                "font_size": group.font_size,
                "region_brightness": round(group.region_brightness, 2),
                "region_saturation": round(group.region_saturation, 2),
                "region_hue": round(group.region_hue, 2),
                "classification": group.classification,
                "inside_balloon_like_region": group.inside_balloon_like_region,
                "inside_narration_box_like_region": group.inside_narration_box_like_region,
                "main_text_score": round(group.main_text_score, 3),
                "angle_degrees": round(group.angle_degrees, 2),
                "near_image_edge": group.near_image_edge,
                "alignment_score": round(group.alignment_score, 3),
                "translated": group.sent_to_translation,
                "ignored": group.ignored,
                "ignore_reason": group.ignore_reason,
                "sent_to_nvidia": group.sent_to_translation,
                "redrawn": group.redrawn,
                "line_count": len(group.lines),
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
