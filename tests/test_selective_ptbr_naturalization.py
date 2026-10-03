"""Selective PT-BR naturalization after semantic fidelity.

Synthetic TDD #5 fixtures only: the production pipeline may improve already
trusted PT-BR wording, but naturalization is not a second translation authority.
It is selective, bounded, post-validated through the existing semantic fidelity
gate, and always falls back to the trusted translation when unsafe.
"""

import _test_bootstrap  # noqa: F401

import tempfile
import unittest
from pathlib import Path

import numpy as np

import semantic_fidelity
from ocr_balloon import OCRLine, TextGroup, validate_and_retry_translations
from session_context import SessionContextStore


def _line(text):
    polygon = np.array([[10, 10], [210, 10], [210, 45], [10, 45]], dtype=np.int32)
    return OCRLine(
        text=text,
        confidence=0.95,
        polygon=polygon,
        box=(10, 10, 200, 35),
        raw_text=text,
        engine="rapidocr",
        page=1,
    )


def _group(
    source,
    trusted,
    *,
    group_id="G001",
    classification="speech",
    evidence=None,
    names=(),
):
    group = TextGroup(
        group_id=group_id,
        lines=[_line(source)],
        text=source,
        translation=trusted,
        translation_candidate=trusted,
        classification=classification,
        sent_to_translation=True,
        inside_balloon_like_region=True,
        source_engine="rapidocr",
    )
    group.detected_proper_names = list(names)
    group.quality_evidence = dict(evidence or {})
    return group


class _SilentTranslator:
    is_configured = True


class _Naturalizer:
    def __init__(self, *responses, fail=False):
        self.responses = list(responses)
        self.fail = fail
        self.calls = []

    def naturalize_ptbr(self, request):
        self.calls.append(dict(request))
        if self.fail:
            raise RuntimeError("naturalizer unavailable")
        return self.responses.pop(0) if self.responses else request["trusted_translation"]


class _Verifier:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def verify_fidelity(self, **kwargs):
        self.calls.append(dict(kwargs))
        if not self.answers:
            return {"faithful": True}
        return self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)


def _run(group, *, naturalizer=None, verifier=None, ledger=None):
    stats = {}
    records = validate_and_retry_translations(
        [group],
        _SilentTranslator(),
        terminology_ledger=ledger,
        fidelity_verifier=verifier,
        fidelity_stats=stats,
        ptbr_naturalizer=naturalizer,
    )
    return records, stats


class SelectivePtBrNaturalizationTests(unittest.TestCase):
    def test_fast_path_already_natural_translation_skips_naturalizer(self):
        group = _group(
            "I will handle it from here.",
            "Eu cuido disso daqui para frente.",
        )
        naturalizer = _Naturalizer("Deixa comigo daqui pra frente.")

        _records, stats = _run(group, naturalizer=naturalizer)

        self.assertEqual(naturalizer.calls, [])
        self.assertEqual(group.translation, "Eu cuido disso daqui para frente.")
        self.assertEqual(stats.get("naturalization_skipped"), 1)
        self.assertNotEqual(group.naturalization_selected_version, "naturalized")

    def test_awkward_faithful_translation_is_selectively_naturalized(self):
        group = _group(
            "I'll handle it from here.",
            "Eu assumirei daqui.",
            evidence={"ptbr_naturalization_needed": True},
        )
        naturalizer = _Naturalizer("Deixa comigo daqui pra frente.")

        _records, stats = _run(group, naturalizer=naturalizer)

        self.assertEqual(len(naturalizer.calls), 1)
        request = naturalizer.calls[0]
        self.assertEqual(request["source_text"], "I'll handle it from here.")
        self.assertEqual(request["trusted_translation"], "Eu assumirei daqui.")
        self.assertEqual(request["target_locale"], "pt-BR")
        self.assertEqual(group.translation, "Deixa comigo daqui pra frente.")
        self.assertEqual(group.naturalization_candidate, "Deixa comigo daqui pra frente.")
        self.assertEqual(group.naturalization_selected_version, "naturalized")
        self.assertTrue(group.naturalization_accepted)
        self.assertEqual(stats.get("naturalization_accepted"), 1)

    def test_untrusted_pre_fidelity_translation_never_reaches_naturalizer(self):
        group = _group(
            "3 enemies remain.",
            "Restam dois inimigos.",
            evidence={"ptbr_naturalization_needed": True},
        )
        naturalizer = _Naturalizer("Restam dois inimigos por aqui.")

        _records, stats = _run(group, naturalizer=naturalizer)

        self.assertEqual(naturalizer.calls, [])
        self.assertFalse(group.translation_valid)
        self.assertEqual(group.translation, "")
        self.assertEqual(group.rejected_translation, "Restam dois inimigos.")
        self.assertEqual(stats.get("naturalization_attempted", 0), 0)

    def test_meaning_changing_naturalization_is_rejected_after_fidelity(self):
        group = _group(
            "I won't go.",
            "Eu não vou.",
            evidence={"ptbr_naturalization_needed": True},
        )
        naturalizer = _Naturalizer("Eu vou.")

        _records, stats = _run(group, naturalizer=naturalizer)

        self.assertEqual(len(naturalizer.calls), 1)
        self.assertEqual(group.translation, "Eu não vou.")
        self.assertEqual(group.naturalization_candidate, "Eu vou.")
        self.assertFalse(group.naturalization_accepted)
        self.assertEqual(group.naturalization_selected_version, "trusted_translation")
        self.assertEqual(group.naturalization_rejected_reason, semantic_fidelity.NEGATION_CHANGED)
        self.assertEqual(stats.get("naturalization_rejected_fidelity"), 1)
        self.assertEqual(stats.get("naturalization_fallback_to_translation"), 1)

    def test_quantity_name_terminology_and_character_facts_protect_final_text(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = SessionContextStore(Path(folder) / "session.json", "synthetic")
            ledger.prepare([])
            ledger.record_translations([
                _group("GATE", "PORTAL", names=[]),
                _group(
                    "MISS NARIN ENTERED.",
                    "Senhorita NARIN entrou.",
                    names=["NARIN"],
                ),
            ])
            group = _group(
                "NARAEK told MISS NARIN that three enemies crossed the GATE.",
                "NARAEK disse à senhorita NARIN que três inimigos cruzaram o PORTAL.",
                evidence={"ptbr_naturalization_needed": True},
                names=["NARAEK", "NARIN"],
            )
            naturalizer = _Naturalizer(
                "O guerreiro disse ao senhor NARIN que dois inimigos cruzaram a passagem."
            )

            _records, stats = _run(group, naturalizer=naturalizer, ledger=ledger)

            request = naturalizer.calls[0]
            self.assertIn("GATE", request["terminology"])
            self.assertTrue(request["character_context"])
            self.assertEqual(
                group.translation,
                "NARAEK disse à senhorita NARIN que três inimigos cruzaram o PORTAL.",
            )
            self.assertFalse(group.naturalization_accepted)
            self.assertEqual(group.naturalization_selected_version, "trusted_translation")
            self.assertGreaterEqual(stats.get("naturalization_fallback_to_translation", 0), 1)

    def test_naturalizer_error_falls_back_to_trusted_translation(self):
        group = _group(
            "I will explain later.",
            "Eu explicarei depois.",
            evidence={"ptbr_naturalization_needed": True},
        )
        naturalizer = _Naturalizer(fail=True)

        _records, stats = _run(group, naturalizer=naturalizer)

        self.assertEqual(group.translation, "Eu explicarei depois.")
        self.assertFalse(group.naturalization_accepted)
        self.assertEqual(group.naturalization_status, "failed")
        self.assertEqual(group.naturalization_selected_version, "trusted_translation")
        self.assertEqual(stats.get("naturalization_failed"), 1)

    def test_sfx_and_ocr_blocked_groups_do_not_reach_naturalizer(self):
        naturalizer = _Naturalizer("BUM!")
        sfx = _group(
            "BOOM",
            "BOOM",
            classification="sfx",
            evidence={"ptbr_naturalization_needed": True},
        )
        blocked = _group(
            "Unreadable",
            "Texto",
            evidence={"ptbr_naturalization_needed": True},
        )
        blocked.ocr_quality_blocked = True

        _run(sfx, naturalizer=naturalizer)
        _run(blocked, naturalizer=naturalizer)

        self.assertEqual(naturalizer.calls, [])


if __name__ == "__main__":
    unittest.main()
