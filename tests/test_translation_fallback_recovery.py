from types import SimpleNamespace

from yomu.translation.gemini_fallback import (
    TRANSLATION_FALLBACK_ACCEPTED,
    TRANSLATION_FALLBACK_REJECTED,
    apply_translation_fallbacks,
    translation_fallback_eligibility,
)


def _group(**overrides):
    values = dict(
        group_id="g1",
        classification="speech",
        text="I will wait here.",
        canonical_source_text="",
        sent_to_translation=True,
        translation="",
        translation_valid=False,
        translation_attempts_exhausted=True,
        translation_unresolved=True,
        translation_final_reason="semantic_fidelity_failed_after_retries",
        preserve_as_name=False,
        ocr_quality_blocked=False,
        detected_proper_names=[],
        page_context_texts=(),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_text_fallback_is_eligible_only_after_semantic_failure():
    group = _group()
    assert translation_fallback_eligibility(group) == (
        True,
        "GEMINI_TRANSLATION_FALLBACK_ELIGIBLE",
    )
    blocked = _group(classification="sfx")
    assert translation_fallback_eligibility(blocked)[0] is False
    ocr = _group(ocr_quality_blocked=True)
    assert translation_fallback_eligibility(ocr)[1] == "ocr_recovery_required"


def test_fallback_candidate_is_revalidated_and_persisted():
    group = _group()
    stats = apply_translation_fallbacks(
        [group],
        lambda **_: "Vou esperar aqui.",
    )
    assert stats["accepted"] == 1
    assert group.translation == "Vou esperar aqui."
    assert group.translation_fallback_status == TRANSLATION_FALLBACK_ACCEPTED
    assert group.translation_attempts_exhausted is False


def test_fallback_rejection_is_review_and_never_silent():
    group = _group(text="I won't wait here.")
    stats = apply_translation_fallbacks(
        [group],
        lambda **_: "Vou esperar aqui.",
    )
    assert stats["rejected"] == 1
    assert group.manual_review_required is True
    assert group.translation_fallback_status == TRANSLATION_FALLBACK_REJECTED


def test_fallback_does_not_touch_sfx_or_proper_names():
    sfx = _group(classification="sfx")
    name = _group(classification="speech", preserve_as_name=True)
    calls = []
    stats = apply_translation_fallbacks([sfx, name], lambda **_: calls.append(1))
    assert stats["attempted"] == 0
    assert calls == []
