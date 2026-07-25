"""Central semantic taxonomy for detected regions (BLOCO 2).

One source of truth that maps a region's evidence (its legacy label plus the
detected text and a few metadata signals) to a *semantic* category, and decides
preserve-vs-translate, failing closed on anything ambiguous.

Design goals:
- Pure functions, no pipeline side effects: safe to call from the offline audit,
  from a review contract, or from a future classification run.
- Legacy compatible: every historical label normalises to a new category; an
  unrecognised label never becomes silently translatable *or* preservable — it
  becomes ``unknown_review_required``.
- No hardcoded chapter pages or phrases: decisions come from text structure and
  metadata, never from a specific known caption string.
"""
from __future__ import annotations

import re

TAXONOMY_VERSION = "1"

# --- categories -------------------------------------------------------------
# Preserve (never auto-translated).
SFX_PRESERVE = "sfx_preserve"
CREDIT_PRESERVE = "credit_preserve"
WATERMARK_PRESERVE = "watermark_preserve"
URL_PRESERVE = "url_preserve"
PROPER_NAME_PRESERVE = "proper_name_preserve"
# Translate (carry semantic meaning).
DECORATIVE_SEMANTIC_TRANSLATE = "decorative_semantic_translate"
TITLE_SEMANTIC_TRANSLATE = "title_semantic_translate"
NARRATION_TRANSLATE = "narration_translate"
DIALOGUE_TRANSLATE = "dialogue_translate"
# Uncertain (fail closed: human decides, provider is never called automatically).
UNKNOWN_REVIEW_REQUIRED = "unknown_review_required"

PRESERVE = frozenset({SFX_PRESERVE, CREDIT_PRESERVE, WATERMARK_PRESERVE,
                      URL_PRESERVE, PROPER_NAME_PRESERVE})
TRANSLATE = frozenset({DECORATIVE_SEMANTIC_TRANSLATE, TITLE_SEMANTIC_TRANSLATE,
                       NARRATION_TRANSLATE, DIALOGUE_TRANSLATE})
UNCERTAIN = frozenset({UNKNOWN_REVIEW_REQUIRED})
ALL_CATEGORIES = PRESERVE | TRANSLATE | UNCERTAIN


def is_preservable(category: str) -> bool:
    return category in PRESERVE


def is_translatable(category: str) -> bool:
    return category in TRANSLATE


def needs_human_review(category: str) -> bool:
    return category in UNCERTAIN


# --- structural detectors (no hardcoded phrases) ----------------------------
# A compact onomatopoeia lexicon. Not exhaustive — real detection also uses the
# shape of the token — but it anchors the common cases.
_SFX_WORDS = frozenset({
    "BAM", "BANG", "BOOM", "BUMP", "CLANG", "CRASH", "DRIP", "GONG", "GRR",
    "GULP", "HISS", "KNOCK", "PLOP", "PLUNK", "POW", "RUMBLE", "SLAM", "SNIFF",
    "SNIFFLE", "SOB", "TAP", "THUD", "UGH", "WHAM", "WHEW", "WHOOSH", "ZAP",
    "CLICK", "CLACK", "SWISH", "SPLASH", "THUMP", "CRACK", "SNAP", "BOOF",
    # consonant-cluster interjections that are onomatopoeia, not acronyms
    "TSK", "TCH", "PFF", "PFFT", "SHH", "PSST", "HMPH", "BRR", "TSS", "MMM", "HMM",
})

_VOWELS = frozenset("AEIOUY")
_URL_RE = re.compile(r"(https?://|www\.|\b[a-z0-9][a-z0-9-]*\.(com|net|org|io|br|co|us|xyz|info|gg)\b)", re.I)
_CREDIT_RE = re.compile(
    r"\b(scan(lation|s)?|translat(ed|or|ion|ions)|traduz(ido|ao|ção)|edit(ed|or|ing)?|"
    r"typeset(ter|ting)?|redraw(er)?|clean(er|ing)?|proofread(er)?|raw\s?provider|uploader|"
    r"team|subs?|staff|credits?)\b", re.I)
_WATERMARK_HINT_RE = re.compile(r"\b(read|scans?|toon|manga|manhwa|webtoon|comic|\.(com|net))\b", re.I)


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ÿ']+", str(text or ""))


def _is_real_word(token: str) -> bool:
    """A plausible dictionary word: has a vowel, some length, not a repeat run."""
    upper = token.upper().strip("'")
    if len(upper) < 3 or upper in _SFX_WORDS:
        return False
    if not any(ch in _VOWELS for ch in upper):
        return False
    if len(set(upper)) <= 1:  # "AAAA"
        return False
    return True


def looks_like_url(text: str) -> bool:
    return bool(_URL_RE.search(str(text or "")))


def looks_like_credit(text: str) -> bool:
    return bool(_CREDIT_RE.search(str(text or "")))


def looks_like_watermark(text: str) -> bool:
    body = str(text or "")
    return looks_like_url(body) or bool(_WATERMARK_HINT_RE.search(body) and _URL_RE.search(body))


def looks_like_sfx(text: str) -> bool:
    """Onomatopoeia by shape, not merely by being uppercase/stylised."""
    words = _words(text)
    if not words or len(words) > 2:
        return False
    for word in words:
        upper = word.upper().strip("'")
        if upper in _SFX_WORDS:
            return True
        # elongated vocalisation ("AAAH", "WHOOOSH", "GRRR")
        if re.search(r"(.)\1{2,}", upper):
            return True
    return False


def has_semantic_content(text: str) -> bool:
    """At least one real word and not an onomatopoeia/URL/credit token."""
    if looks_like_sfx(text) or looks_like_url(text):
        return False
    return any(_is_real_word(word) for word in _words(text))


# --- normalisation ----------------------------------------------------------
# Legacy label -> new category for the unambiguous cases. The visual buckets
# (sfx, decorative) are re-evaluated from the text because that is exactly where
# semantic text was being lost.
_DIRECT_LEGACY = {
    "speech": DIALOGUE_TRANSLATE,
    "thought": DIALOGUE_TRANSLATE,
    "dialogue": DIALOGUE_TRANSLATE,
    "narration": NARRATION_TRANSLATE,
    "credit": CREDIT_PRESERVE,
    "watermark": WATERMARK_PRESERVE,
    "url": URL_PRESERVE,
    "proper_name": PROPER_NAME_PRESERVE,
}


def normalize(legacy_label: str, *, text: str = "", preserve_as_name: bool = False) -> tuple[str, str]:
    """Map a region to (category, reason_code). Fail-closed on the unknown.

    ``legacy_label`` is the historical classification (speech, sfx, decorative…).
    ``text`` is the detected source text; ``preserve_as_name`` is the pipeline's
    proven proper-name flag.
    """
    label = str(legacy_label or "").strip().lower()
    body = str(text or "")

    # A proven proper name stays a proper name regardless of the visual bucket.
    if preserve_as_name:
        return PROPER_NAME_PRESERVE, "proven_proper_name"

    # Text-shape evidence wins over a coarse visual label for the preserve cases.
    if looks_like_url(body):
        return URL_PRESERVE, "url_detected"
    if looks_like_credit(body):
        return CREDIT_PRESERVE, "credit_terms_detected"
    if looks_like_watermark(body):
        return WATERMARK_PRESERVE, "watermark_signature_detected"

    if label in _DIRECT_LEGACY:
        return _DIRECT_LEGACY[label], f"legacy_{label}"

    if label == "title":
        if has_semantic_content(body):
            return TITLE_SEMANTIC_TRANSLATE, "legacy_title_semantic"
        return UNKNOWN_REVIEW_REQUIRED, "title_without_semantic_content"

    # The visual/uncertain buckets are re-evaluated from the text — this is where
    # semantic text was being lost. "unknown"/"" is a real legacy label ("the
    # classifier was unsure"), so it is judged by evidence, not fail-closed blind.
    if label in ("sfx", "decorative", "unknown", ""):
        if looks_like_sfx(body):
            return SFX_PRESERVE, "onomatopoeia_shape"
        if has_semantic_content(body):
            # Styled/out-of-balloon text that carries meaning is translatable,
            # not a preserved graphic effect. Human confirms the exact subtype.
            return DECORATIVE_SEMANTIC_TRANSLATE, f"legacy_{label or 'blank'}_semantic_content"
        return UNKNOWN_REVIEW_REQUIRED, f"legacy_{label or 'blank'}_without_semantic_content"

    # An unrecognised legacy label: never silently translate or preserve.
    return UNKNOWN_REVIEW_REQUIRED, "fail_closed_unknown_label"


def suggested_action(category: str) -> str:
    if is_translatable(category):
        return "translate"
    if is_preservable(category):
        return "preserve"
    return "human_review"
