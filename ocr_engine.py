import re
import unicodedata
from dataclasses import dataclass

import cv2
import numpy as np

import config


PADDLE_LANG_BY_CHOICE = {
    "1": "japan",
    "2": "korean",
    "3": "en",
    "jpn": "japan",
    "japan": "japan",
    "ja": "japan",
    "kor": "korean",
    "korean": "korean",
    "ko": "korean",
    "eng": "en",
    "en": "en",
}

TESSERACT_LANG_BY_CHOICE = {
    "1": "jpn",
    "2": "kor",
    "3": "eng",
    "japan": "jpn",
    "jpn": "jpn",
    "ja": "jpn",
    "korean": "kor",
    "kor": "kor",
    "ko": "kor",
    "en": "eng",
    "eng": "eng",
}


@dataclass
class OCRLine:
    text: str
    confidence: float
    polygon: np.ndarray
    box: tuple
    raw_text: str
    engine: str = ""
    page: int | None = None
    metadata: dict | None = None
    original_text: str = ""
    repaired_text: str = ""
    repair_reason: str = ""


class OCREngine:
    _paddle_instances = {}
    _rapidocr_instances = {}

    def __init__(self, lang_choice, engine=None, fallback_engine=None):
        self.lang_choice = str(lang_choice).strip().lower()
        self.engine = (engine or config.OCR_ENGINE or "paddle").lower()
        self.fallback_engine = (fallback_engine or config.OCR_FALLBACK_ENGINE or "").lower()
        self.paddle_lang = PADDLE_LANG_BY_CHOICE.get(self.lang_choice, "en")
        self.tesseract_lang = TESSERACT_LANG_BY_CHOICE.get(self.lang_choice, "eng")
        self.last_run_metadata = {}

    def read_text(self, img_crop):
        lines = self.detect_lines(img_crop)
        if lines:
            return clean_ocr_text(" ".join(line.text for line in lines))

        if self.engine == "tesseract" or self.fallback_engine == "tesseract":
            return self._read_with_tesseract(img_crop)

        return ""

    def detect_lines(self, img_bgr, upscale=True, page=None):
        if img_bgr is None or img_bgr.size == 0:
            self.last_run_metadata = self._run_metadata(
                page,
                original_engine=self.engine,
                final_engine=self.engine,
                fallback_reason="empty_image",
            )
            return []

        if self.engine == "rapidocr":
            return self._detect_rapidocr_hybrid(img_bgr, page=page)

        if self.engine not in {"paddle", "paddle_mobile", "paddle_no_upscale"}:
            text = self._read_with_tesseract(img_bgr)
            return [self._line_from_whole_image(text, img_bgr)] if text else []

        try:
            lines = self._detect_with_paddle(img_bgr, upscale=upscale)
            self.last_run_metadata = self._run_metadata(
                page,
                original_engine=self.engine,
                final_engine=self.engine,
            )
            return self._annotate_lines(lines, page, self.engine)
        except Exception as exc:
            print(f"PaddleOCR falhou nesta imagem/regiao: {exc}")
            if self.fallback_engine == "tesseract":
                text = self._read_with_tesseract(img_bgr)
                lines = [self._line_from_whole_image(text, img_bgr)] if text else []
                self.last_run_metadata = self._run_metadata(
                    page,
                    original_engine=self.engine,
                    final_engine="tesseract",
                    fallback_reason=f"paddle_error:{type(exc).__name__}",
                )
                return self._annotate_lines(lines, page, "tesseract")
            return []

    def _detect_rapidocr_hybrid(self, img_bgr, page=None):
        if not config.RAPIDOCR_ENABLED:
            return self._fallback_from_rapidocr(
                img_bgr,
                page,
                "rapidocr_disabled",
            )

        try:
            lines = self._detect_with_rapidocr(img_bgr)
        except Exception as exc:
            print(f"RapidOCR falhou nesta imagem/regiao: {exc}")
            return self._fallback_from_rapidocr(
                img_bgr,
                page,
                f"rapidocr_error:{type(exc).__name__}",
            )

        lines, repairs = _repair_ocr_lines(lines)
        suspicion = _rapidocr_suspicion(img_bgr, lines)
        if suspicion["reasons"] and config.RAPIDOCR_PAGE_FALLBACK:
            return self._fallback_from_rapidocr(
                img_bgr,
                page,
                ";".join(suspicion["reasons"]),
                rapid_metrics=suspicion["metrics"],
                repairs=repairs,
            )

        self.last_run_metadata = self._run_metadata(
            page,
            original_engine="rapidocr",
            final_engine="rapidocr",
            rapid_metrics=suspicion["metrics"],
            repairs=repairs,
        )
        return self._annotate_lines(lines, page, "rapidocr")

    def _fallback_from_rapidocr(
        self,
        img_bgr,
        page,
        reason,
        rapid_metrics=None,
        repairs=None,
    ):
        can_use_paddle = (
            config.OCR_HYBRID_FALLBACK
            and self.fallback_engine == "paddle"
        )
        if not can_use_paddle:
            self.last_run_metadata = self._run_metadata(
                page,
                original_engine="rapidocr",
                final_engine="rapidocr",
                fallback_reason=reason,
                rapid_metrics=rapid_metrics,
                repairs=repairs,
            )
            return []

        paddle = OCREngine(
            self.lang_choice,
            engine="paddle_mobile",
            fallback_engine="",
        )
        lines = paddle.detect_lines(img_bgr, page=page)
        self.last_run_metadata = self._run_metadata(
            page,
            original_engine="rapidocr",
            final_engine="paddle",
            fallback_used=True,
            fallback_reason=reason,
            rapid_metrics=rapid_metrics,
            repairs=repairs,
        )
        self.last_run_metadata["fallback_variant"] = "paddle_mobile"
        return self._annotate_lines(lines, page, "paddle")

    def _detect_with_paddle(self, img_bgr, upscale=True):
        ocr = self._get_paddle()
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        # The mobile models are tuned for their native input pipeline. Upscaling
        # these Webtoon pages increased duplicate/noise detections in benchmarks.
        use_upscale = (
            upscale
            and self.engine not in {"paddle_mobile", "paddle_no_upscale"}
        )
        scale = _ocr_scale(rgb.shape[1], rgb.shape[0]) if use_upscale else 1.0

        if scale != 1.0:
            rgb_for_ocr = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        else:
            rgb_for_ocr = rgb

        results = None
        if hasattr(ocr, "predict"):
            try:
                results = ocr.predict(input=rgb_for_ocr)
            except TypeError:
                results = None

        if not results and hasattr(ocr, "ocr"):
            results = ocr.ocr(rgb_for_ocr)

        lines = _extract_lines(results, scale)
        return [line for line in lines if line.text]

    def _detect_with_rapidocr(self, img_bgr):
        rapidocr = self._get_rapidocr()
        output = rapidocr(img_bgr)
        results = output[0] if isinstance(output, tuple) else output
        lines = []
        for item in results or []:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            polygon, raw_text, confidence = item[:3]
            text = clean_ocr_text(str(raw_text))
            if not text:
                continue
            polygon = np.asarray(polygon, dtype=np.float32).reshape(-1, 2)
            polygon = np.round(polygon).astype(np.int32)
            lines.append(
                OCRLine(
                    text=text,
                    confidence=float(confidence),
                    polygon=polygon,
                    box=_box_from_poly(polygon),
                    raw_text=str(raw_text),
                )
            )
        return sorted(lines, key=lambda line: (line.box[1], line.box[0]))

    def _read_with_tesseract(self, img_crop):
        try:
            import pytesseract
        except ImportError:
            print("pytesseract nao instalado. Fallback Tesseract ignorado.")
            return ""

        try:
            gray = cv2.cvtColor(img_crop, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)
            th = cv2.adaptiveThreshold(
                gray,
                255,
                cv2.ADAPTIVE_THRESH_MEAN_C,
                cv2.THRESH_BINARY,
                11,
                2,
            )
            text = pytesseract.image_to_string(th, lang=self.tesseract_lang, config="--psm 6")
            return clean_ocr_text(text)
        except Exception as exc:
            print(f"Tesseract falhou neste balao: {exc}")
            return ""

    def _get_paddle(self):
        cache_key = (self.engine, self.paddle_lang)
        if cache_key not in self._paddle_instances:
            from paddleocr import PaddleOCR

            kwargs = {
                "lang": self.paddle_lang,
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
            }
            if self.engine == "paddle_mobile":
                kwargs.update(
                    text_detection_model_name="PP-OCRv4_mobile_det",
                    text_recognition_model_name=(
                        "en_PP-OCRv4_mobile_rec"
                        if self.paddle_lang == "en"
                        else "PP-OCRv4_mobile_rec"
                    ),
                )

            label = {
                "paddle_mobile": "PaddleOCR mobile",
                "paddle_no_upscale": "PaddleOCR sem upscale",
            }.get(self.engine, "PaddleOCR")
            print(f"Inicializando {label} ({self.paddle_lang})...")
            self._paddle_instances[cache_key] = PaddleOCR(
                **kwargs,
            )
        return self._paddle_instances[cache_key]

    def _get_rapidocr(self):
        cache_key = "default"
        if cache_key not in self._rapidocr_instances:
            from rapidocr_onnxruntime import RapidOCR

            print("Inicializando RapidOCR / ONNX Runtime...")
            self._rapidocr_instances[cache_key] = RapidOCR()
        return self._rapidocr_instances[cache_key]

    def _annotate_lines(self, lines, page, engine):
        metadata = dict(self.last_run_metadata)
        for line in lines:
            line.engine = engine
            line.page = page
            existing = dict(line.metadata or {})
            existing.update(metadata)
            line.metadata = existing
            if not line.original_text:
                line.original_text = line.raw_text or line.text
            if not line.repaired_text:
                line.repaired_text = line.text
        return lines

    @staticmethod
    def _run_metadata(
        page,
        original_engine,
        final_engine,
        fallback_used=False,
        fallback_reason="",
        rapid_metrics=None,
        repairs=None,
    ):
        return {
            "page": page,
            "original_engine": original_engine,
            "final_engine": final_engine,
            "fallback_used": bool(fallback_used),
            "fallback_reason": fallback_reason or "",
            "rapidocr_metrics": rapid_metrics or {},
            "text_repairs": repairs or [],
        }

    @staticmethod
    def _line_from_whole_image(text, img_bgr):
        h, w = img_bgr.shape[:2]
        polygon = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.int32)
        return OCRLine(clean_ocr_text(text), 1.0, polygon, (0, 0, w, h), text)


def clean_ocr_text(text):
    text = (text or "").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return text.strip()


COMMON_REPEATED_WORDS = {
    "AGAIN",
    "ALWAYS",
    "BABY",
    "COME",
    "DON'T",
    "FINE",
    "GOOD",
    "GREAT",
    "HAPPY",
    "HELLO",
    "HELP",
    "HERE",
    "LOOK",
    "LOVE",
    "MOM",
    "NEVER",
    "PLEASE",
    "QUICK",
    "REALLY",
    "RIGHT",
    "SORRY",
    "STOP",
    "THANK",
    "THERE",
    "THINK",
    "TODAY",
    "WAIT",
    "WHAT",
    "WHY",
    "YES",
}


def _repair_ocr_lines(lines):
    repairs = []
    if (
        not config.OCR_TEXT_REPAIR
        or config.OCR_TEXT_REPAIR_MODE != "conservative"
    ):
        return lines, repairs

    for line in lines:
        original = line.text
        repaired, reason = repair_ocr_text(original)
        line.original_text = original
        line.repaired_text = repaired
        line.repair_reason = reason
        if repaired != original:
            line.text = repaired
            repair = {
                "original_text": original,
                "repaired_text": repaired,
                "repair_reason": reason,
            }
            repairs.append(repair)
            line.metadata = {**(line.metadata or {}), **repair}
    return lines, repairs


def repair_ocr_text(text):
    if (
        not config.OCR_TEXT_REPAIR
        or config.OCR_TEXT_REPAIR_MODE != "conservative"
    ):
        return text, ""
    repaired, compact_reason = _repair_compact_age(text)
    repaired, repeated_reason = _repair_repeated_word(repaired)
    reasons = [
        reason
        for reason in (compact_reason, repeated_reason)
        if reason
    ]
    return repaired, ";".join(reasons)


def _repair_compact_age(text):
    repaired = re.sub(
        r"\b(age)(\d{1,3})\b",
        lambda match: f"{match.group(1)} {match.group(2)}",
        text,
        flags=re.IGNORECASE,
    )
    replacements = {
        "THATWAS": "THAT WAS",
        "PURPOSEIN": "PURPOSE IN",
        "THATISUNTIL": "THAT IS UNTIL",
        "THELETTER": "THE LETTER",
        "FROMTHE": "FROM THE",
        "EMPIRE'SCAPITAL": "EMPIRE'S CAPITAL",
        "MAJESTYHAS": "MAJESTY HAS",
    }
    replacement_applied = False
    for source, target in replacements.items():
        updated = re.sub(
            rf"\b{re.escape(source)}\b",
            target,
            repaired,
            flags=re.IGNORECASE,
        )
        replacement_applied = replacement_applied or updated != repaired
        repaired = updated
    reasons = []
    if re.search(r"\b(age)\s+\d{1,3}\b", repaired, re.IGNORECASE) and re.search(
        r"\b(age)\d{1,3}\b",
        text,
        re.IGNORECASE,
    ):
        reasons.append("separate_age_number")
    if replacement_applied:
        reasons.append("split_known_glued_words")
    return repaired, ";".join(reasons)


def _repair_repeated_word(text):
    parts = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|[^A-Za-z]+", text)
    word_indexes = [
        index
        for index, part in enumerate(parts)
        if re.fullmatch(r"[A-Za-z]+(?:'[A-Za-z]+)?", part)
    ]
    for left_index, right_index in zip(word_indexes, word_indexes[1:]):
        if right_index - left_index > 2:
            continue
        left = parts[left_index]
        right = parts[right_index]
        left_upper = left.upper()
        right_upper = right.upper()
        if (
            left_upper not in COMMON_REPEATED_WORDS
            or right_upper == left_upper
            or abs(len(left_upper) - len(right_upper)) > 1
            or _edit_distance(left_upper, right_upper) > 2
        ):
            continue
        if _similarity(left_upper, right_upper) < 0.66:
            continue
        parts[right_index] = _match_word_case(left, left_upper)
        return "".join(parts), "adjacent_common_word_ocr_typo"
    return text, ""


def _edit_distance(left, right):
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


def _similarity(left, right):
    distance = _edit_distance(left, right)
    return 1.0 - distance / max(1, len(left), len(right))


def _match_word_case(source, upper):
    if source.islower():
        return upper.lower()
    if source.istitle():
        return upper.title()
    return upper


def _rapidocr_suspicion(img_bgr, lines):
    height, width = img_bgr.shape[:2]
    reasons = []
    invalid_boxes = sum(
        not _valid_line_box(line, width, height)
        for line in lines
    )
    confidences = [float(line.confidence) for line in lines]
    average_confidence = (
        sum(confidences) / len(confidences)
        if confidences
        else 0.0
    )
    strange_lines = sum(_strange_text_ratio(line.text) > 0.22 for line in lines)
    improbable_tokens, total_tokens = _improbable_token_counts(lines)
    text_regions = _estimate_text_regions(img_bgr)

    if not lines and text_regions >= 2:
        reasons.append("zero_lines_on_text_like_page")
    if lines and average_confidence < config.RAPIDOCR_MIN_CONFIDENCE:
        reasons.append("low_average_confidence")
    if invalid_boxes:
        reasons.append("invalid_boxes")
    if (
        config.RAPIDOCR_SUSPICIOUS_TEXT_FALLBACK
        and len(lines) >= 3
        and strange_lines / len(lines) >= 0.35
    ):
        reasons.append("many_strange_characters")
    if (
        config.RAPIDOCR_SUSPICIOUS_TEXT_FALLBACK
        and total_tokens >= 6
        and improbable_tokens / total_tokens >= 0.45
    ):
        reasons.append("many_improbable_tokens")
    if text_regions >= 5 and len(lines) <= 1:
        reasons.append("too_few_lines_for_text_regions")

    return {
        "reasons": reasons,
        "metrics": {
            "line_count": len(lines),
            "average_confidence": round(average_confidence, 6),
            "invalid_boxes": int(invalid_boxes),
            "strange_lines": int(strange_lines),
            "improbable_tokens": int(improbable_tokens),
            "total_tokens": int(total_tokens),
            "estimated_text_regions": int(text_regions),
        },
    }


def _valid_line_box(line, width, height):
    x, y, w, h = line.box
    return (
        len(np.asarray(line.polygon).reshape(-1, 2)) >= 4
        and w > 0
        and h > 0
        and x >= 0
        and y >= 0
        and x + w <= width + 2
        and y + h <= height + 2
    )


def _strange_text_ratio(text):
    if not text:
        return 1.0
    strange = sum(
        1
        for char in text
        if not (
            char.isalnum()
            or char.isspace()
            or char in "'!?.,:;+-&/()"
        )
    )
    return strange / max(1, len(text))


def _improbable_token_counts(lines):
    total = 0
    improbable = 0
    for line in lines:
        for token in re.findall(r"[A-Za-z]{4,}", line.text):
            total += 1
            normalized = unicodedata.normalize("NFKD", token).upper()
            vowels = sum(char in "AEIOUY" for char in normalized)
            if vowels == 0:
                improbable += 1
    return improbable, total


def _estimate_text_regions(img_bgr):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    scale = min(1.0, 640.0 / max(gray.shape[:2]))
    if scale < 1.0:
        gray = cv2.resize(
            gray,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_AREA,
        )
    adaptive = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        11,
    )
    count, _, stats, _ = cv2.connectedComponentsWithStats(
        255 - adaptive,
        connectivity=8,
    )
    candidates = 0
    for index in range(1, count):
        _, _, component_width, component_height, area = stats[index]
        if (
            4 <= component_width <= gray.shape[1] * 0.18
            and 4 <= component_height <= gray.shape[0] * 0.08
            and 12 <= area <= gray.size * 0.008
        ):
            candidates += 1
    return min(20, candidates // 3)


def _ocr_scale(width, height):
    if width <= 0 or height <= 0:
        return 1.0

    scale = min(2.0, 1800 / width, 3800 / height)
    if scale < 1.0:
        return max(0.35, scale)
    return max(1.0, scale)


def _extract_lines(results, scale):
    lines = []
    for result in results or []:
        data = _result_to_dict(result)
        if data:
            lines.extend(_extract_lines_from_dict(data, scale))
        else:
            lines.extend(_extract_lines_from_legacy(result, scale))

    return sorted(lines, key=lambda line: (line.box[1], line.box[0]))


def _result_to_dict(result):
    if isinstance(result, dict):
        return result

    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        return to_dict()

    json_value = getattr(result, "json", None)
    if isinstance(json_value, dict):
        return json_value

    return None


def _extract_lines_from_dict(data, scale):
    texts = _first_present(data, "rec_texts", "texts") or []
    scores = _first_present(data, "rec_scores", "scores") or []
    polys = _first_present(data, "rec_polys", "dt_polys")
    boxes = _first_present(data, "rec_boxes", "boxes")
    lines = []

    for idx, raw_text in enumerate(texts):
        text = clean_ocr_text(str(raw_text))
        if not text:
            continue

        score = float(scores[idx]) if idx < len(scores) else 0.0
        poly = _poly_for_index(polys, boxes, idx)
        if poly is None:
            continue

        poly = np.array(poly, dtype=np.float32) / float(scale)
        poly = np.round(poly).astype(np.int32)
        box = _box_from_poly(poly)
        lines.append(OCRLine(text=text, confidence=score, polygon=poly, box=box, raw_text=str(raw_text)))

    return lines


def _first_present(data, *keys):
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _poly_for_index(polys, boxes, idx):
    if polys is not None and len(polys) > idx:
        poly = polys[idx]
        if poly is not None:
            return np.array(poly).reshape(-1, 2)

    if boxes is not None and len(boxes) > idx:
        x1, y1, x2, y2 = np.array(boxes[idx]).astype(float).tolist()
        return np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]])

    return None


def _extract_lines_from_legacy(result, scale):
    lines = []

    if not isinstance(result, (list, tuple)):
        return lines

    for item in result:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue

        poly = np.array(item[0], dtype=np.float32)
        info = item[1]
        if not isinstance(info, (list, tuple)) or not info:
            continue

        text = clean_ocr_text(str(info[0]))
        score = float(info[1]) if len(info) > 1 else 0.0
        if not text:
            continue

        poly = np.round(poly / float(scale)).astype(np.int32)
        lines.append(OCRLine(text=text, confidence=score, polygon=poly, box=_box_from_poly(poly), raw_text=text))

    return lines


def _box_from_poly(poly):
    xs = poly[:, 0]
    ys = poly[:, 1]
    x1 = int(xs.min())
    y1 = int(ys.min())
    x2 = int(xs.max())
    y2 = int(ys.max())
    return x1, y1, max(1, x2 - x1), max(1, y2 - y1)
