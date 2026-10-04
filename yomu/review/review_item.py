"""Normalized linguistic-QC Review model (Mission 8).

Review is a *linguistic* QC tool: locate pending regions, compare source vs translation, edit the
translation, approve/resolve. Visual/typographic QC belongs in the editable PSD, not here.

This module gives the UI/tests a small, stable :class:`ReviewItem` decoupled from the large
``quality_review`` payload, plus human-readable PT-BR labels for the warning codes. It does not
persist anything and does not change job terminal semantics (that is Mission 9). REVIEW_MANIFEST
stays PLANNED (Mission 10) -- no capability is promoted here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# --- QC status of a single review item (NOT the job's terminal status) -------------------
class ReviewStatus:
    PENDING = "pending"
    APPROVED = "approved"     # translation accepted as linguistically OK
    RESOLVED = "resolved"     # edited/corrected or warning consciously accepted


# Map the pipeline's per-item state strings onto the small QC status set above.
_STATE_TO_STATUS = {
    "pending": ReviewStatus.PENDING,
    "reviewed": ReviewStatus.APPROVED,
    "approved": ReviewStatus.APPROVED,
    "edited": ReviewStatus.RESOLVED,
    "rejected": ReviewStatus.RESOLVED,
    "preserved_original": ReviewStatus.RESOLVED,
    "manual_review": ReviewStatus.PENDING,
}


# --- warning code -> human PT-BR label (§14/§15). Codes stay in English internally. -------
WARNING_LABELS = {
    "OCR_LOW_CONFIDENCE": "OCR com baixa confiança",
    "OCR_MISSED": "Possível texto não detectado",
    "OCR_INCORRECT": "OCR possivelmente incorreto",
    "ENGLISH_RESIDUAL": "Possível texto em inglês",
    "SEMANTIC_VALIDATION": "Tradução requer revisão",
    "TERMINOLOGY_WARNING": "Terminologia a verificar",
    "TRANSLATION_SKIPPED": "Tradução não aplicada",
    "SOURCE_FALLBACK": "Texto original mantido",
    "CROSS_PAGE": "Continuidade entre páginas",
    "FOOTNOTE": "Nota de rodapé",
    "TITLE": "Título",
    "VISUAL_REVIEW": "Revisão visual recomendada no PSD",
    "OTHER": "Este trecho precisa de atenção",
}
_GENERIC_WARNING = "Este trecho precisa de atenção antes de confirmar o resultado."


def humanize_warning(code: str) -> str:
    """Human PT-BR label for a warning code. Unknown/among-non-code text falls back to a generic
    message, so a new code is never shown raw to the user but is also never silently dropped."""
    key = str(code or "").strip()
    if not key:
        return _GENERIC_WARNING
    if key in WARNING_LABELS:
        return WARNING_LABELS[key]
    # already human text (has lowercase/spaces and is not an ALL_CAPS_CODE) -> pass through
    if not key.replace("_", "").isupper() or " " in key:
        return key
    return _GENERIC_WARNING


@dataclass
class ReviewItem:
    """One linguistic-QC unit. Small and UI-facing; built from a quality_review payload item."""
    page: int
    region_id: str
    semantic_type: str
    source_text: str
    translated_text: str
    warnings: list = field(default_factory=list)      # warning codes (English, internal)
    confidence: float | None = None
    status: str = ReviewStatus.PENDING

    @property
    def warning_labels(self) -> list:
        return [humanize_warning(w) for w in self.warnings]

    @property
    def has_translation(self) -> bool:
        return bool(str(self.translated_text or "").strip())

    def to_dict(self) -> dict:
        return {
            "page": self.page, "region_id": self.region_id,
            "semantic_type": self.semantic_type,
            "source_text": self.source_text, "translated_text": self.translated_text,
            "warnings": list(self.warnings), "warning_labels": self.warning_labels,
            "confidence": self.confidence, "status": self.status,
        }


def _derive_warnings(item: dict) -> list:
    """Collect warning codes from the payload without fabricating any. Only signals actually
    present (a reason code, a stale flag, a visual reason, an explicit fallback) become warnings."""
    warnings: list[str] = []
    code = str(item.get("review_reason_code") or item.get("reason_code") or "").strip()
    if code and code.replace("_", "").isupper():
        warnings.append(code)
    if item.get("translation_stale"):
        warnings.append("SEMANTIC_VALIDATION")
    if item.get("preserved_original") and not str(item.get("translation") or "").strip():
        warnings.append("SOURCE_FALLBACK")
    vcode = str(item.get("visual_reason_code") or "").strip()
    if vcode:
        warnings.append("VISUAL_REVIEW")
    # de-duplicate, preserve order
    seen, out = set(), []
    for w in warnings:
        if w not in seen:
            seen.add(w); out.append(w)
    return out


def from_quality_review_item(item: dict) -> ReviewItem:
    """Adapt one ``quality_review`` payload item into a normalized :class:`ReviewItem`.

    Tolerant of legacy/missing fields (§26): absent source/translation/confidence/status all get
    safe defaults so historical runs still open."""
    if not isinstance(item, dict):
        raise TypeError("review item must be a dict")
    try:
        page = int(item.get("page") if item.get("page") is not None else item.get("page_index") or 0)
    except (TypeError, ValueError):
        page = 0
    source = item.get("source_text_effective")
    if source is None:
        source = item.get("original") or item.get("source_text_original") or ""
    target = item.get("target_text_effective")
    if target is None:
        target = item.get("translation") or item.get("target_text_original") or ""
    conf = item.get("confidence")
    try:
        conf = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf = None
    status = _STATE_TO_STATUS.get(str(item.get("state") or "pending"), ReviewStatus.PENDING)
    return ReviewItem(
        page=page,
        region_id=str(item.get("region_id") or item.get("key") or ""),
        semantic_type=str(item.get("classification") or item.get("region_type_effective") or "unknown"),
        source_text=str(source or ""),
        translated_text=str(target or ""),
        warnings=_derive_warnings(item),
        confidence=conf,
        status=status,
    )
