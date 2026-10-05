from yomu.translation.residual_validator import (
    UNEXPECTED_SOURCE_DIALOGUE,
    classify_source_residual,
)


def test_dialogue_residual_has_explicit_reason():
    assert classify_source_residual({
        "classification": "speech",
        "clean_text": "I do not want to go outside.",
    }) == UNEXPECTED_SOURCE_DIALOGUE


def test_sfx_and_names_are_allowed_source_text():
    assert classify_source_residual({"classification": "sfx", "clean_text": "TAP"}) is None
    assert classify_source_residual({
        "classification": "speech",
        "clean_text": "Yamada",
        "preserve_as_name": True,
    }) is None


def test_legacy_decorative_text_does_not_become_silent_dialogue():
    assert classify_source_residual({
        "classification": "decorative",
        "clean_text": "TITLE",
    }) is None
