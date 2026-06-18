import re
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


class OCREngine:
    _paddle_instances = {}

    def __init__(self, lang_choice, engine=None, fallback_engine=None):
        self.lang_choice = str(lang_choice).strip().lower()
        self.engine = (engine or config.OCR_ENGINE or "paddle").lower()
        self.fallback_engine = (fallback_engine or config.OCR_FALLBACK_ENGINE or "").lower()
        self.paddle_lang = PADDLE_LANG_BY_CHOICE.get(self.lang_choice, "en")
        self.tesseract_lang = TESSERACT_LANG_BY_CHOICE.get(self.lang_choice, "eng")

    def read_text(self, img_crop):
        lines = self.detect_lines(img_crop)
        if lines:
            return clean_ocr_text(" ".join(line.text for line in lines))

        if self.engine == "tesseract" or self.fallback_engine == "tesseract":
            return self._read_with_tesseract(img_crop)

        return ""

    def detect_lines(self, img_bgr, upscale=True):
        if img_bgr is None or img_bgr.size == 0:
            return []

        if self.engine != "paddle":
            text = self._read_with_tesseract(img_bgr)
            return [self._line_from_whole_image(text, img_bgr)] if text else []

        try:
            return self._detect_with_paddle(img_bgr, upscale=upscale)
        except Exception as exc:
            print(f"PaddleOCR falhou nesta imagem/regiao: {exc}")
            if self.fallback_engine == "tesseract":
                text = self._read_with_tesseract(img_bgr)
                return [self._line_from_whole_image(text, img_bgr)] if text else []
            return []

    def _detect_with_paddle(self, img_bgr, upscale=True):
        ocr = self._get_paddle()
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        scale = _ocr_scale(rgb.shape[1], rgb.shape[0]) if upscale else 1.0

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
        if self.paddle_lang not in self._paddle_instances:
            from paddleocr import PaddleOCR

            print(f"Inicializando PaddleOCR ({self.paddle_lang})...")
            self._paddle_instances[self.paddle_lang] = PaddleOCR(
                lang=self.paddle_lang,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        return self._paddle_instances[self.paddle_lang]

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
