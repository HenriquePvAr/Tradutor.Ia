"""P11 Phase 9A: ordinary semantic substitution characterization.

The fixtures use reliable OCR metadata and deliberately do not create any
world-memory binding.  The historical bad candidate is expected to demonstrate
the current gap (it is accepted), while the controls protect existing behavior.
"""

import _test_bootstrap  # noqa: F401

import tempfile
import unittest
from pathlib import Path

from ocr_balloon import validate_and_retry_translations
from semantic_fidelity import evaluate_local_fidelity
from session_context import SessionContextStore
from test_chapter_terminology_ledger import _StubTranslator, _group


class P11SemanticSubstitutionRedTests(unittest.TestCase):
    def _run(self, source, candidate):
        group = _group(source, candidate, group_id="P11-001")
        records = validate_and_retry_translations([group], _StubTranslator(), terminology_ledger=None)
        return group, records

    def test_historical_carrier_to_menino_currently_reaches_accept(self):
        group, _ = self._run(
            "The carrier arrived at the station.",
            "O menino chegou à estação.",
        )
        # Characterization RED: future safety must reject/retry/review this.
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation_final_reason, "ok")
        self.assertEqual(evaluate_local_fidelity(group.text, group.translation).status, "faithful")

    def test_contextually_good_translation_is_allowed(self):
        group, _ = self._run(
            "The carrier arrived at the station.",
            "O portador chegou à estação.",
        )
        self.assertTrue(group.translation_valid)

    def test_legitimate_paraphrase_is_allowed(self):
        group, _ = self._run(
            "The carrier arrived at the station.",
            "Quem transportava a carga chegou à estação.",
        )
        self.assertTrue(group.translation_valid)

    def test_generic_noun_substitution_is_currently_unseen_by_gate(self):
        finding = evaluate_local_fidelity("The healer entered the room.", "O soldado entrou na sala.")
        self.assertEqual(finding.status, "faithful")

    def test_opposite_action_is_currently_unseen_by_gate(self):
        finding = evaluate_local_fidelity("He opened the door.", "Ele fechou a porta.")
        self.assertEqual(finding.status, "faithful")

    def test_agent_patient_reversal_is_currently_unseen_by_gate(self):
        finding = evaluate_local_fidelity(
            "The hunter attacks the guard.",
            "O guarda ataca o caçador.",
        )
        self.assertEqual(finding.status, "faithful")

    def test_p11_does_not_create_world_memory_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionContextStore(Path(tmp) / "session.json", "https://example.invalid/p11")
            store.prepare([])
            self.assertEqual(store.binding_for("carrier"), "")
            self.assertEqual(store.drift_reason("The carrier arrived", "O menino chegou"), "")


if __name__ == "__main__":
    unittest.main()
