"""Explicit source-language residual classification for final quality gates."""
from __future__ import annotations

import re
from typing import Any

ALLOWED_SOURCE_TEXT = {"SFX_PRESERVED", "PROPER_NAME_PRESERVED", "DECORATIVE_TEXT_PRESERVED"}
UNEXPECTED_SOURCE_DIALOGUE = "UNTRANSLATED_DIALOGUE"


def classify_source_residual(item: dict[str, Any]) -> str | None:
    """Return a review reason only for unexpected story text still on the page."""
    classification = str(item.get("classification") or "").strip().lower()
    text = re.sub(r"\s+", " ", str(item.get("clean_text") or item.get("raw_text") or "")).strip()
    if not text:
        return None
    if classification == "sfx" or str(item.get("sfx_preserved") or "").upper() == "YES":
        return None
    if bool(item.get("preserve_as_name")) or str(item.get("translation_final_reason") or "") == "proper_name_only":
        return None
    if classification in {"decorative", "decorative_text"} and not item.get("story_display_promoted"):
        return None
    return UNEXPECTED_SOURCE_DIALOGUE
