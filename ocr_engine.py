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
                # PaddleOCR's oneDNN path can terminate the Windows process in native
                # code (rather than raising a Python exception) on some CPU/model
                # combinations.  Keep the OCR worker fail-safe and let Paddle use its
                # regular CPU kernels; this is a generic runtime safeguard, not a
                # chapter-specific workaround.
                enable_mkldnn=False,
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


COMMON_ENGLISH_WORD_SCORES = {
    # Compact, generic vocabulary for OCR repair. Scores are relative
    # preference weights, not chapter-specific rules.
    "a": 1.8,
    "about": 1.4,
    "after": 1.5,
    "again": 1.4,
    "all": 1.5,
    "already": 1.2,
    "always": 1.2,
    "am": 1.4,
    "an": 1.4,
    "and": 1.8,
    "anyone": 1.3,
    "are": 1.8,
    "as": 1.6,
    "at": 1.4,
    "back": 1.3,
    "be": 1.5,
    "because": 1.3,
    "before": 1.3,
    "better": 1.4,
    "body": 1.2,
    "but": 1.8,
    "by": 1.4,
    "can": 1.7,
    "cant": 1.4,
    "cannot": 1.2,
    "changed": 1.1,
    "clothes": 1.2,
    "come": 1.5,
    "could": 1.5,
    "day": 1.2,
    "defeat": 1.2,
    "determination": 1.1,
    "did": 1.5,
    "dirty": 1.1,
    "do": 1.6,
    "dollars": 1.1,
    "earthquakes": 1.0,
    "eyes": 1.2,
    "face": 1.1,
    "faced": 1.0,
    "fight": 1.3,
    "find": 1.4,
    "for": 1.8,
    "friend": 1.3,
    "from": 1.7,
    "get": 1.6,
    "gift": 1.2,
    "go": 1.5,
    "going": 1.3,
    "gotta": 1.1,
    "have": 1.7,
    "he": 1.6,
    "head": 1.2,
    "heck": 1.1,
    "her": 1.4,
    "his": 1.6,
    "hurt": 1.2,
    "i": 1.9,
    "if": 1.7,
    "in": 1.6,
    "is": 1.7,
    "it": 1.7,
    "just": 1.4,
    "let": 1.3,
    "like": 1.5,
    "look": 1.4,
    "me": 1.7,
    "meaning": 1.1,
    "met": 1.2,
    "mom": 1.3,
    "my": 1.8,
    "myself": 1.3,
    "natural": 1.1,
    "not": 1.6,
    "of": 1.8,
    "on": 1.5,
    "or": 1.5,
    "out": 1.4,
    "part": 1.2,
    "pathetic": 1.1,
    "plank": 1.0,
    "planks": 1.0,
    "promised": 1.1,
    "really": 1.4,
    "resolute": 1.0,
    "resolve": 1.1,
    "resonated": 1.0,
    "right": 1.2,
    "see": 1.2,
    "she": 1.5,
    "soon": 1.2,
    "spent": 1.2,
    "still": 1.3,
    "such": 1.2,
    "talking": 1.2,
    "take": 1.3,
    "that": 1.7,
    "the": 1.9,
    "their": 1.4,
    "them": 1.5,
    "there": 1.3,
    "these": 1.6,
    "they": 1.5,
    "think": 1.2,
    "this": 1.7,
    "to": 1.8,
    "today": 1.2,
    "too": 1.4,
    "two": 1.4,
    "up": 1.5,
    "voice": 1.1,
    "wake": 1.2,
    "was": 1.6,
    "wash": 1.2,
    "we": 1.6,
    "what": 1.6,
    "when": 1.4,
    "where": 1.4,
    "who": 1.5,
    "why": 1.5,
    "will": 1.7,
    "wings": 1.0,
    "with": 1.6,
    "you": 1.8,
    "your": 1.6,
    "yeah": 1.2,
}

COMMON_ENGLISH_WORDS = set(COMMON_ENGLISH_WORD_SCORES)
_WORDS_BY_LENGTH = {}
for _word in COMMON_ENGLISH_WORDS:
    _WORDS_BY_LENGTH.setdefault(len(_word), []).append(_word)


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
        assessment = assess_ocr_repair(
            original,
            repaired,
            reason,
            confidence=line.confidence,
            source_engine="rapidocr",
        )
        line.original_text = original
        line.repaired_text = repaired if assessment["accepted"] else original
        line.repair_reason = reason if assessment["accepted"] else ""
        if repaired != original:
            repair = {
                "original_text": original,
                "repaired_text": repaired,
                "repair_reason": reason,
                **assessment,
            }
            repairs.append(repair)
            line.metadata = {
                **(line.metadata or {}),
                "repair_candidate": repair,
                "repair_accepted": bool(assessment["accepted"]),
            }
        if repaired != original and assessment["accepted"]:
            line.text = repaired
            line.metadata = {
                **(line.metadata or {}),
                "original_text": original,
                "repaired_text": repaired,
                "repair_reason": reason,
            }
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
    if re.fullmatch(r"[.·•]\s*A", str(text or "").strip(), flags=re.IGNORECASE):
        return "A", "strip_leading_dot_from_article"

    original_text = str(text or "")
    text = _normalize_ocr_punctuation(original_text)
    repaired = re.sub(
        r"\b(age)(\d{1,3})\b",
        lambda match: f"{match.group(1)} {match.group(2)}",
        text,
        flags=re.IGNORECASE,
    )
    reasons = []
    if re.search(r"\b(age)\s+\d{1,3}\b", repaired, re.IGNORECASE) and re.search(
        r"\b(age)\d{1,3}\b",
        text,
        re.IGNORECASE,
    ):
        reasons.append("separate_age_number")
    contraction_repaired, contraction_reasons = _repair_attached_pronoun_contractions(
        repaired
    )
    if contraction_repaired != repaired:
        repaired = contraction_repaired
        reasons.extend(contraction_reasons)
    generic_repaired, generic_reasons = _repair_tokens_with_generic_vocabulary(repaired)
    if generic_repaired != repaired:
        repaired = generic_repaired
        reasons.extend(generic_reasons)
    if text != original_text:
        reasons.insert(0, "normalize_ocr_punctuation")
    return repaired, ";".join(reasons)


def assess_ocr_repair(
    original,
    repaired,
    reason,
    confidence=0.0,
    source_engine="",
    agreeing_engines=None,
):
    """Decide whether a generic OCR repair is safe enough for runtime use.

    The repair generator may propose useful candidates aggressively so they can
    trigger selective OCR fallback. Runtime mutation is deliberately stricter:
    spelling changes require agreement from another engine, while punctuation,
    repeated-word context and segmentation that only inserts spaces are allowed.
    """
    original = str(original or "")
    repaired = str(repaired or "")
    reasons = [item for item in str(reason or "").split(";") if item]
    agreeing_engines = sorted(set(agreeing_engines or []))
    original_letters = re.sub(r"[^A-Za-z]", "", original).upper()
    repaired_letters = re.sub(r"[^A-Za-z]", "", repaired).upper()
    edit_distance = _edit_distance(original_letters, repaired_letters)
    normalized_distance = edit_distance / max(1, len(original_letters), len(repaired_letters))
    score_before = _generic_text_plausibility(original)
    score_after = _generic_text_plausibility(repaired)
    improvement = score_after - score_before

    accepted = repaired != original
    rejection_reason = ""
    if not accepted:
        rejection_reason = "no_change"
    elif normalized_distance > 0.22:
        accepted = False
        rejection_reason = "edit_distance_too_large"
    elif any(_looks_like_proper_name(token) for token in re.findall(r"[A-Za-z]+", original)):
        if original_letters != repaired_letters:
            accepted = False
            rejection_reason = "possible_proper_name"
    elif "dictionary_edit_distance_repair" in reasons and not agreeing_engines:
        accepted = False
        rejection_reason = "dictionary_change_requires_engine_agreement"
    elif "segment_compact_english_word" in reasons and edit_distance > 0 and not agreeing_engines:
        accepted = False
        rejection_reason = "segmentation_spelling_change_requires_engine_agreement"
    elif "adjacent_common_word_ocr_typo" in reasons:
        accepted = edit_distance <= 2 and improvement >= -0.01
        if not accepted:
            rejection_reason = "repeated_word_context_not_strong_enough"
    elif set(reasons).issubset(
        {
            "normalize_ocr_punctuation",
            "strip_leading_dot_from_article",
            "separate_age_number",
            "split_attached_pronoun_contraction",
            "normalize_confused_exclamation_marks",
            "segment_compact_english_word",
        }
    ):
        accepted = edit_distance == 0 or bool(agreeing_engines)
        if not accepted:
            rejection_reason = "structural_repair_changed_letters"
    elif improvement < 0.08 and not agreeing_engines:
        accepted = False
        rejection_reason = "quality_improvement_too_small"

    return {
        "accepted": bool(accepted),
        "rejection_reason": rejection_reason,
        "score_before": round(float(score_before), 4),
        "score_after": round(float(score_after), 4),
        "score_improvement": round(float(improvement), 4),
        "edit_distance": int(edit_distance),
        "normalized_edit_distance": round(float(normalized_distance), 4),
        "ocr_confidence": round(float(confidence or 0.0), 4),
        "source_engine": source_engine or "",
        "agreeing_engines": agreeing_engines,
    }


def _generic_text_plausibility(text):
    tokens = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", str(text or ""))
    if not tokens:
        return 0.0
    points = 0.0
    for token in tokens:
        lower = token.lower().replace("'", "")
        if lower in COMMON_ENGLISH_WORDS:
            points += 1.0
        elif _looks_like_proper_name(token):
            points += 0.72
        elif len(lower) <= 2:
            points += 0.45
        elif _low_vowel_signal(token):
            points += 0.05
        else:
            points += 0.32
    spacing_bonus = min(0.12, max(0, len(tokens) - 1) * 0.025)
    return min(1.0, points / len(tokens) + spacing_bonus)


def _normalize_ocr_punctuation(text):
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00b4": "'",
        "\u02bc": "'",
        "\u2026": "...",
        "\u00a0": " ",
    }
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"\s+([?!.,:;])", r"\1", normalized)
    normalized = re.sub(r"([(\[{])\s+", r"\1", normalized)
    normalized = re.sub(r"\s+([)\]}])", r"\1", normalized)
    return normalized.strip()


def _repair_attached_pronoun_contractions(text):
    """Split compact OCR tokens where a pronoun is glued to a contraction.

    This stays generic: it only handles common auxiliary contractions and
    inserts missing whitespace. It does not translate or special-case any
    chapter phrase.
    """

    reasons = []

    def split_i(match):
        suffix = match.group(1)
        reasons.append("split_attached_pronoun_contraction")
        if match.group(0).islower():
            return f"i {suffix.lower()}"
        if match.group(0).istitle():
            return f"I {suffix.title()}"
        return f"I {suffix.upper()}"

    pattern_i = re.compile(
        r"\bI("
        r"CAN['’]?T|CANT|WON['’]?T|WONT|DON['’]?T|DONT|DIDN['’]?T|DIDNT|"
        r"COULDN['’]?T|COULDNT|WOULDN['’]?T|WOULDNT|SHOULDN['’]?T|SHOULDNT|"
        r"HAVEN['’]?T|HAVENT|HADN['’]?T|HADNT|AM|M|LL|VE|D"
        r")\b",
        flags=re.IGNORECASE,
    )
    repaired = pattern_i.sub(split_i, str(text or ""))
    return repaired, reasons


def _looks_like_proper_name(token):
    token = str(token or "")
    alpha = re.sub(r"[^A-Za-z]", "", token)
    if len(alpha) < 3:
        return False
    return token[:1].isupper() and token[1:].islower() and alpha.lower() not in COMMON_ENGLISH_WORDS


def _match_phrase_case(source, phrase):
    if str(source).islower():
        return phrase.lower()
    if str(source).istitle():
        return phrase.title()
    return phrase.upper()


def _low_vowel_signal(token):
    letters = re.sub(r"[^A-Za-z]", "", str(token or "")).upper()
    if len(letters) < 3:
        return False
    vowels = sum(char in "AEIOUY" for char in letters)
    consonant_run = bool(re.search(r"[BCDFGHJKLMNPQRSTVWXYZ]{3,}", letters))
    return vowels <= max(0, len(letters) // 5) or consonant_run


def _word_candidates(piece, allow_fuzzy=True):
    piece_lower = re.sub(r"[^a-z]", "", str(piece or "").lower())
    if not piece_lower:
        return []
    candidates = []
    if piece_lower in COMMON_ENGLISH_WORDS:
        candidates.append((piece_lower, 0, 1.0))

    if not allow_fuzzy:
        candidates.sort(
            key=lambda item: (
                item[2] + COMMON_ENGLISH_WORD_SCORES.get(item[0], 0.0) * 0.08,
                -item[1],
                len(item[0]),
            ),
            reverse=True,
        )
        return candidates[:8]

    max_distance = 1 if len(piece_lower) <= 5 else 2
    for length in range(
        max(1, len(piece_lower) - max_distance),
        len(piece_lower) + max_distance + 1,
    ):
        for word in _WORDS_BY_LENGTH.get(length, []):
            if word == piece_lower:
                continue
            distance = _edit_distance(piece_lower, word)
            if distance > max_distance:
                continue
            similarity = 1.0 - distance / max(len(piece_lower), len(word))
            if similarity < 0.66:
                continue
            confidence = max(0.0, similarity - 0.08 * distance)
            candidates.append((word, distance, confidence))
    candidates.sort(
        key=lambda item: (
            item[2] + COMMON_ENGLISH_WORD_SCORES.get(item[0], 0.0) * 0.08,
            -item[1],
            len(item[0]),
        ),
        reverse=True,
    )
    return candidates[:8]


def suggest_english_word(token):
    token_text = str(token or "")
    if "'" in token_text:
        return "", 0.0
    alpha = re.sub(r"[^A-Za-z]", "", token_text)
    if len(alpha) < 3:
        return "", 0.0
    if _looks_like_proper_name(token_text):
        return "", 0.0
    lower = alpha.lower()
    if lower in COMMON_ENGLISH_WORDS:
        return "", 0.0

    best_word = ""
    best_score = 0.0
    for word, distance, confidence in _word_candidates(lower):
        if distance == 0:
            continue
        if len(alpha) <= 3 and not _low_vowel_signal(alpha):
            continue
        if distance >= 2 and not (
            len(alpha) >= 6
            and confidence >= 0.66
            and (alpha.isupper() or _low_vowel_signal(alpha))
        ):
            continue
        frequency_bonus = min(0.18, COMMON_ENGLISH_WORD_SCORES.get(word, 0.0) * 0.08)
        score = confidence + frequency_bonus
        if score > best_score:
            best_word = word
            best_score = score
    return best_word, min(1.0, best_score)


def segment_compact_english_word(token):
    token_text = str(token or "")
    alpha = re.sub(r"[^A-Za-z]", "", token_text)
    if len(alpha) < 4 or _looks_like_proper_name(token_text):
        return "", 0.0
    lower = alpha.lower()
    if lower in COMMON_ENGLISH_WORDS:
        return "", 0.0

    max_word_len = max(_WORDS_BY_LENGTH) if _WORDS_BY_LENGTH else 16
    n = len(lower)
    dp = [None] * (n + 1)
    dp[0] = (0.0, [])
    for start in range(n):
        if dp[start] is None:
            continue
        base_score, base_words = dp[start]
        for end in range(start + 1, min(n, start + max_word_len + 2) + 1):
            piece = lower[start:end]
            if len(piece) == 1 and piece not in {"a", "i"}:
                continue
            allow_fuzzy = _low_vowel_signal(piece) or len(piece) >= 7
            for word, distance, confidence in _word_candidates(piece, allow_fuzzy=allow_fuzzy):
                if distance > 0 and len(piece) < 4:
                    continue
                if distance > 0 and not _low_vowel_signal(piece):
                    continue
                word_score = (
                    COMMON_ENGLISH_WORD_SCORES.get(word, 0.8)
                    + len(word) * 0.035
                    + confidence * 0.55
                    - distance * 0.62
                    - 0.08
                )
                if len(word) == 1:
                    word_score -= 0.45
                if distance and word_score < 0.55:
                    continue
                next_score = base_score + word_score
                current = dp[end]
                if current is None or next_score > current[0]:
                    dp[end] = (next_score, base_words + [word])

    if not dp[n]:
        return "", 0.0
    score, words = dp[n]
    if len(words) < 2:
        return "", 0.0
    average_len = sum(len(word) for word in words) / len(words)
    single_positions = [index for index, word in enumerate(words) if len(word) == 1]
    if len(single_positions) > 1:
        return "", 0.0
    if single_positions:
        pos = single_positions[0]
        single = words[pos]
        if not (single == "a" or (single == "i" and pos in {0, len(words) - 1})):
            return "", 0.0
    if average_len < 2.2 and not single_positions:
        return "", 0.0
    confidence = min(1.0, score / max(1.0, len(words) * 1.45))
    return " ".join(words), confidence


def _repair_tokens_with_generic_vocabulary(text):
    source_text = str(text or "")
    parts = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|\d+|[^A-Za-z\d]+", source_text)
    protected_vocatives = _potential_vocative_tokens(source_text)
    reasons = []
    changed = False
    for index, part in enumerate(parts):
        if not re.fullmatch(r"[A-Za-z]+(?:'[A-Za-z]+)?", part):
            continue
        if "'" in part:
            continue
        if part.upper() in protected_vocatives:
            continue
        if _looks_like_proper_name(part):
            continue

        segmented, segment_score = segment_compact_english_word(part)
        if segmented and segmented.upper() != part.upper() and segment_score >= 0.58:
            parts[index] = _match_phrase_case(part, segmented)
            reasons.append("segment_compact_english_word")
            changed = True
            continue

        suggestion, suggestion_score = suggest_english_word(part)
        if suggestion and suggestion.upper() != part.upper() and suggestion_score >= 0.62:
            parts[index] = _match_word_case(part, suggestion.upper())
            reasons.append("dictionary_edit_distance_repair")
            changed = True

    repaired = "".join(parts)
    if re.search(r"\b1{2,}\s*[?!]*\b", repaired):
        updated = re.sub(
            r"\b1{2,}\s*([?!]*)\b",
            lambda match: "!" + (match.group(1) or "!"),
            repaired,
        )
        if updated != repaired:
            repaired = updated
            reasons.append("normalize_confused_exclamation_marks")
            changed = True
    return (repaired if changed else text), reasons


def _potential_vocative_tokens(text):
    """Return tokens in address positions where names must be preserved.

    Compact-word segmentation is useful for OCR, but a token directly before
    terminal punctuation after a comma (or directly before a leading comma)
    is commonly a person's name. Without cross-engine evidence, preserving it
    is safer than splitting it into dictionary words.
    """
    patterns = (
        r",\s*([A-Za-z]{3,})\s*[.!?]*\s*$",
        r"^\s*([A-Za-z]{3,})\s*,",
        r"^\s*([A-Za-z]{4,})\s*[!?]+\s*$",
    )
    return {
        match.group(1).upper()
        for pattern in patterns
        for match in re.finditer(pattern, str(text or ""))
    }


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
