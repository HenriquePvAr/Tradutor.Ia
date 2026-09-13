"""NVIDIA OCR provider -- encapsulates ALL NVIDIA OCR HTTP communication.

Scope guard (see CLAUDE.md / mission brief): this module talks to an NVIDIA OCR
*vision* endpoint only. It must never call a chat/completions LLM endpoint --
translation, review and classification stay on DeepL/Nemotron exactly as they
are today. ``_assert_not_llm_model`` fails loudly if ``NVIDIA_OCR_MODEL`` is
ever pointed at the translation model by mistake.

Nothing here logs a secret: the API key is only ever placed in the
``Authorization`` header of the outgoing request, never in an exception
message, a print, or the returned telemetry.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import cv2
import requests

import config
from ocr_contract import OCRPageResult, OCRRegion
from provider_transport import ProviderTransportError

# Translation/LLM models (or chat-style substrings) this OCR provider must
# never resolve to (mission §5/§15) -- deliberately specific, not a bare
# "nemotron" substring, since the correct hosted OCR model is itself named
# nvidia/nemotron-ocr-v2.
_FORBIDDEN_LLM_MODEL_SUBSTRINGS = ("nemotron-3-super", "riva-translate", "chat/completions", "chat-completions")

# The only model the hosted (NVIDIA-hosted Build/NIM) deployment may use.
_HOSTED_OCR_MODEL = "nvidia/nemotron-ocr-v2"

# The only host the hosted deployment's invoke URL may point at.
_HOSTED_OCR_ALLOWED_HOST = "ai.api.nvidia.com"

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Nemotron OCR v2 "merge_levels" contract -- fail closed on anything else.
_VALID_MERGE_LEVELS = ("word", "sentence", "paragraph")


class NvidiaOCRConfigError(RuntimeError):
    """Provider misconfigured (missing key/model, or model is an LLM)."""


class NvidiaOCRProviderError(ProviderTransportError):
    """OCR request failed. ``retryable`` tells the caller whether it is safe
    to requeue the page onto another engine (see ocr_hybrid_scheduler)."""

    def __init__(self, reason_code: str, *, status_code=None, retryable=False):
        super().__init__(reason_code, status_code=status_code)
        self.retryable = retryable


def _assert_not_llm_model(model: str, *, deployment: str) -> None:
    lowered = str(model or "").lower()
    if any(token in lowered for token in _FORBIDDEN_LLM_MODEL_SUBSTRINGS):
        raise NvidiaOCRConfigError(
            "nvidia_ocr_model_is_translation_llm: OCR provider must use a "
            "dedicated OCR model, not the translation LLM"
        )
    if deployment == "hosted" and lowered != _HOSTED_OCR_MODEL:
        raise NvidiaOCRConfigError(
            f"nvidia_ocr_hosted_model_must_be_{_HOSTED_OCR_MODEL.replace('/', '_')}"
        )


def _resolve_invoke_url(deployment: str) -> str:
    """Hosted: use the configured invoke URL AS-IS (no concatenation),
    validating its host. Self-hosted: an explicit NVIDIA_OCR_ENDPOINT wins
    (back-compat full URL), else base_url + "/v1/ocr"."""
    if deployment == "self_hosted":
        explicit = str(config.NVIDIA_OCR_ENDPOINT or "").strip()
        if explicit:
            return explicit
        base = str(getattr(config, "NVIDIA_OCR_SELF_HOSTED_BASE_URL", "") or "").rstrip("/")
        return base + "/v1/ocr"

    url = str(config.NVIDIA_OCR_INVOKE_URL or "").strip()
    host = urlparse(url).hostname or ""
    if host != _HOSTED_OCR_ALLOWED_HOST:
        raise NvidiaOCRConfigError("invalid_nvidia_ocr_endpoint")
    return url


@dataclass
class _RawRegion:
    text: str
    bbox: tuple
    confidence: float


def _parse_regions(payload: dict, *, page_width: int, page_height: int) -> list:
    """Parse the official Nemotron OCR v2 response contract:

    {"data": [{"index": 0, "text_detections": [
        {"text_prediction": {"text": ..., "confidence": ...},
         "bounding_box": {"points": [{"x": .., "y": ..}, ...4]}}
    ]}], "usage": {...}}

    ``bounding_box.points`` are normalized to [0, 1]; they are converted to
    pixel space here and clamped to the page's actual bounds, never with axes
    swapped.
    """
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise NvidiaOCRProviderError("nvidia_ocr_malformed_response:missing_data")
    entry = data[0]
    if not isinstance(entry, dict):
        raise NvidiaOCRProviderError("nvidia_ocr_malformed_response:data_entry_not_object")
    detections = entry.get("text_detections")
    if not isinstance(detections, list):
        raise NvidiaOCRProviderError("nvidia_ocr_malformed_response:missing_text_detections")

    parsed = []
    for raw in detections:
        if not isinstance(raw, dict):
            raise NvidiaOCRProviderError("nvidia_ocr_malformed_response:detection_not_object")

        text_prediction = raw.get("text_prediction")
        if not isinstance(text_prediction, dict):
            raise NvidiaOCRProviderError("nvidia_ocr_malformed_response:bad_text_prediction")
        text = text_prediction.get("text")
        if not isinstance(text, str):
            raise NvidiaOCRProviderError("nvidia_ocr_malformed_response:bad_text")
        confidence = text_prediction.get("confidence", 0.0)
        if not isinstance(confidence, (int, float)):
            raise NvidiaOCRProviderError("nvidia_ocr_malformed_response:bad_confidence")

        bounding_box = raw.get("bounding_box")
        if not isinstance(bounding_box, dict):
            raise NvidiaOCRProviderError("nvidia_ocr_malformed_response:bad_bounding_box")
        points = bounding_box.get("points")
        if not isinstance(points, list) or len(points) != 4:
            raise NvidiaOCRProviderError("nvidia_ocr_malformed_response:bad_points")

        xs, ys = [], []
        for point in points:
            if not isinstance(point, dict):
                raise NvidiaOCRProviderError("nvidia_ocr_malformed_response:bad_point")
            x, y = point.get("x"), point.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                raise NvidiaOCRProviderError("nvidia_ocr_malformed_response:bad_point")
            xs.append(float(x))
            ys.append(float(y))

        # Normalized [0, 1] -> pixels (axes never swapped), then clamped to bounds.
        x0 = max(0.0, min(min(xs) * page_width, page_width))
        x1 = max(0.0, min(max(xs) * page_width, page_width))
        y0 = max(0.0, min(min(ys) * page_height, page_height))
        y1 = max(0.0, min(max(ys) * page_height, page_height))

        parsed.append(_RawRegion(text=text, bbox=(x0, y0, x1, y1), confidence=float(confidence)))
    return parsed


class NvidiaOCRProvider:
    """Encapsulates the NVIDIA OCR HTTP contract. Only entry point downstream
    code should use: ``recognize_page``."""

    def __init__(self, *, session=None):
        self.deployment = str(getattr(config, "NVIDIA_OCR_DEPLOYMENT", "hosted") or "hosted").strip().lower()
        self.model = str(config.NVIDIA_OCR_MODEL or "").strip()
        self.api_key = str(config.NVIDIA_API_KEY or "").strip()
        _assert_not_llm_model(self.model, deployment=self.deployment)
        self.endpoint = _resolve_invoke_url(self.deployment)
        self.merge_level = str(getattr(config, "NVIDIA_OCR_MERGE_LEVEL", "sentence") or "").strip().lower()
        if self.merge_level not in _VALID_MERGE_LEVELS:
            raise NvidiaOCRConfigError(
                f"nvidia_ocr_invalid_merge_level: must be one of {_VALID_MERGE_LEVELS}"
            )
        self.connect_timeout = float(getattr(config, "NVIDIA_OCR_CONNECT_TIMEOUT_SECONDS", 10.0))
        self.read_timeout = float(getattr(config, "NVIDIA_OCR_READ_TIMEOUT_SECONDS", 30.0))
        self.max_retries = max(0, int(getattr(config, "NVIDIA_OCR_MAX_RETRIES", 2)))
        self.retry_backoff_seconds = max(
            0.0, float(getattr(config, "NVIDIA_OCR_RETRY_BACKOFF_SECONDS", 1.0))
        )
        self._session = session or requests

    def is_configured(self) -> bool:
        return bool(self.model) and bool(self.api_key)

    def recognize_page(self, image, context=None) -> OCRPageResult:
        """image: BGR numpy array (as returned by cv2.imread). context: optional
        dict with ``page_id`` used only for telemetry/labelling."""
        if not self.is_configured():
            raise NvidiaOCRConfigError("nvidia_ocr_not_configured")

        context = context or {}
        page_id = context.get("page_id", 0)
        height, width = image.shape[0], image.shape[1]

        ok, buffer = cv2.imencode(".png", image)
        if not ok:
            raise NvidiaOCRProviderError("nvidia_ocr_image_encode_failed")
        image_b64 = base64.b64encode(buffer.tobytes()).decode("ascii")
        data_url = f"data:image/png;base64,{image_b64}"

        # Official Nemotron OCR v2 contract: "input" array of image_url items
        # plus "merge_levels" -- no model/image/image_format at the root (the
        # model is selected by the hosted invoke URL itself).
        body = {
            "input": [{"type": "image_url", "url": data_url}],
            "merge_levels": [self.merge_level],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        last_error = None
        for attempt in range(1, self.max_retries + 2):  # +1 initial + N retries
            try:
                response = self._session.post(
                    self.endpoint,
                    json=body,
                    headers=headers,
                    timeout=(self.connect_timeout, self.read_timeout),
                )
            except requests.exceptions.Timeout as exc:
                # Ambiguous: the request may have been processed server-side.
                # Not safe to blindly requeue -- surface as non-retryable so
                # the caller records OCR_PROVIDER_OUTCOME_UNKNOWN instead of
                # silently repeating a possibly-billed call.
                last_error = NvidiaOCRProviderError(
                    "nvidia_ocr_timeout", retryable=False
                )
                break
            except requests.exceptions.RequestException:
                # Connection never reached the server: safe to retry/requeue.
                last_error = NvidiaOCRProviderError(
                    "nvidia_ocr_connection_error", retryable=True
                )
                if attempt <= self.max_retries:
                    time.sleep(self.retry_backoff_seconds * attempt)
                    continue
                break

            if response.status_code in _RETRYABLE_STATUS_CODES:
                last_error = NvidiaOCRProviderError(
                    "nvidia_ocr_http_error",
                    status_code=response.status_code,
                    retryable=True,
                )
                if attempt <= self.max_retries:
                    time.sleep(self.retry_backoff_seconds * attempt)
                    continue
                break

            if response.status_code != 200:
                # Client errors (401/403/404/422/...) are not transient: fail
                # closed immediately, do not retry, do not requeue.
                raise NvidiaOCRProviderError(
                    "nvidia_ocr_http_error",
                    status_code=response.status_code,
                    retryable=False,
                )

            try:
                payload = response.json()
            except ValueError:
                raise NvidiaOCRProviderError("nvidia_ocr_malformed_response:not_json")
            if not isinstance(payload, dict):
                raise NvidiaOCRProviderError("nvidia_ocr_malformed_response:not_object")

            raw_regions = _parse_regions(payload, page_width=width, page_height=height)
            regions = [
                OCRRegion(
                    region_id=f"{page_id}-{index}",
                    bbox=tuple(int(round(v)) for v in raw.bbox),
                    text=raw.text,
                    confidence=raw.confidence,
                    engine="nvidia",
                )
                for index, raw in enumerate(raw_regions)
            ]
            return OCRPageResult(
                page_id=page_id, engine="nvidia", width=width, height=height, regions=regions
            )

        raise last_error or NvidiaOCRProviderError("nvidia_ocr_unknown_failure")
