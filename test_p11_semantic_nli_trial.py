"""P11 Phase 9D deterministic GREEN-trial contracts."""

import _test_bootstrap  # noqa: F401

import unittest

from ocr_balloon import validate_and_retry_translations
from semantic_nli_adapter import SemanticNLIAdapter
from test_chapter_terminology_ledger import _StubTranslator, _group


def _scores(c_forward, e_forward, c_reverse, e_reverse):
    return {
        "forward": {"E": e_forward, "N": 0.1, "C": c_forward},
        "reverse": {"E": e_reverse, "N": 0.1, "C": c_reverse},
    }


class P11SemanticNLITrialTests(unittest.TestCase):
    def test_strong_contradiction_uses_existing_retry_path(self):
        scores = iter((_scores(0.8, 0.1, 0.7, 0.2), _scores(0.001, 0.5, 0.001, 0.5)))
        adapter = SemanticNLIAdapter(
            enabled=True,
            scorer=lambda *_: next(scores),
        )
        group = _group(
            "The carrier arrived at the station.",
            "O menino chegou à estação.",
            group_id="NLI-1",
        )
        records = validate_and_retry_translations(
            [group], _StubTranslator("O portador chegou à estação."), semantic_nli=adapter
        )
        self.assertTrue(records)
        self.assertEqual(records[0]["reason"], "ok")
        self.assertEqual(group.translation_validation_reason, "retry_ok")
        self.assertEqual(group.translation, "O portador chegou à estação.")

    def test_ambiguous_is_neutral_and_does_not_retry(self):
        calls = []
        adapter = SemanticNLIAdapter(
            enabled=True,
            scorer=lambda *_: (calls.append(1) or _scores(0.001, 0.5, 0.001, 0.5)),
        )
        group = _group("The carrier arrived at the station.", "O portador chegou à estação.", group_id="NLI-2")
        validate_and_retry_translations([group], _StubTranslator(), semantic_nli=adapter)
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation_validation_reason, "ok")
        self.assertEqual(len(calls), 1)

    def test_feature_disabled_is_backward_compatible(self):
        adapter = SemanticNLIAdapter(enabled=False, scorer=lambda *_: self.fail("NLI called"))
        group = _group("The carrier arrived at the station.", "O menino chegou à estação.", group_id="NLI-3")
        validate_and_retry_translations([group], _StubTranslator(), semantic_nli=adapter)
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation_validation_reason, "ok")

    def test_enabled_missing_model_fails_closed(self):
        adapter = SemanticNLIAdapter(enabled=True, model_path="C:/does/not/exist")
        group = _group("The carrier arrived at the station.", "O portador chegou à estação.", group_id="NLI-4")
        validate_and_retry_translations([group], _StubTranslator(), semantic_nli=adapter)
        self.assertFalse(group.translation_valid)
        self.assertIn("semantic_check_unavailable", group.translation_validation_reason)

    def test_bidirectional_combination_requires_both_frozen_features(self):
        adapter = SemanticNLIAdapter(
            enabled=True,
            scorer=lambda *_: _scores(0.8, 0.5, 0.1, 0.5),
        )
        result = adapter.evaluate("A full sentence source", "Uma frase completa")
        self.assertEqual(result.status, "AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
