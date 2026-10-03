"""P27 Phase 8A: characterize the existing world-term authority boundary.

These are hermetic design/checkpoint tests only.  They deliberately exercise
the current ledger lookup and terminology-drift path without wiring a new
production gate or calling a provider.
"""

import _test_bootstrap  # noqa: F401

import tempfile
import unittest
from pathlib import Path

from session_context import (
    KIND_TERMINOLOGY,
    TERM_AUTHORITY_DOMAIN_TERM,
    TERM_AUTHORITY_LEXICAL_HINT,
    SessionContextStore,
)
from ocr_balloon import validate_and_retry_translations
from test_chapter_terminology_ledger import _StubTranslator, _group


def _store(tmp):
    store = SessionContextStore(Path(tmp) / "session.json", "https://example.invalid/p27")
    store.prepare([])
    return store


def _binding(store, source, target, authority=TERM_AUTHORITY_DOMAIN_TERM):
    key = source.upper()
    entry = store._entry(key, source, KIND_TERMINOLOGY, authority)
    entry["authority"] = authority
    store._establish(entry, target, authority)
    store.save()


class P27AuthorityGateDesignTests(unittest.TestCase):
    def test_bad_candidate_is_already_rejected_by_existing_authority_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _binding(store, "Awakened", "Despertado")
            group = _group("The Awakened returned", "Os Alcostados voltaram", group_id="P27-1")
            records = validate_and_retry_translations(
                [group], _StubTranslator("Despertado voltou"), terminology_ledger=store
            )
            self.assertTrue(records)
            self.assertEqual(records[0]["reason"], "terminology_retry_ok")
            self.assertNotEqual(group.translation, "Os Alcostados voltaram")

    def test_exact_historical_phrase_cannot_silently_keep_the_bad_term(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _binding(store, "Awakened", "Despertado")
            self.assertTrue(
                store.drift_reason(
                    "... BECOME AN AWAKENED, RIGHT?",
                    "... SE TORNAR UM ALCOSTADOS, CERTO?",
                )
            )

    def test_canonical_target_passes_authority_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _binding(store, "Awakened", "Despertado")
            self.assertEqual(store.drift_reason("Awakened returned", "Despertado voltou"), "")

    def test_existing_target_family_accepts_gender_and_number_forms(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _binding(store, "Awakened", "Despertado")
            self.assertEqual(store.drift_reason("Awakened returned", "Despertada voltou"), "")
            self.assertEqual(store.drift_reason("Awakened returned", "Despertados voltaram"), "")

    def test_unknown_term_is_neutral(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            self.assertEqual(store.drift_reason("Voidkin returned", "Os Alcostados voltaram"), "")

    def test_source_term_absent_is_neutral(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _binding(store, "Awakened", "Despertado")
            self.assertEqual(store.drift_reason("The hunter returned", "O caçador voltou"), "")

    def test_two_authoritative_terms_do_not_let_a_correct_one_mask_other_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _binding(store, "Awakened", "Despertado")
            _binding(store, "Dungeon", "Masmorra")
            reason = store.drift_reason(
                "The Awakened entered the Dungeon", "O Despertado entrou no castelo"
            )
            self.assertTrue(reason.startswith("terminology_conflict:"))

    def test_repeated_term_is_characterized_as_token_level_lookup(self):
        """Current gate has no per-occurrence alignment; preserve this evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _binding(store, "Awakened", "Despertado")
            self.assertEqual(
                store.drift_reason("Awakened and Awakened", "Despertado e Alcostados"), ""
            )

    def test_weak_lexical_hint_is_prompt_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _binding(store, "ABOUT", "SOBRE", TERM_AUTHORITY_LEXICAL_HINT)
            self.assertEqual(store.drift_reason("This is about us", "Isso é diferente"), "")
            self.assertEqual(store.data["ledger_stats"].get("term_binding_prompt_only"), 1)

    def test_conflicted_memory_does_not_choose_last_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _binding(store, "Awakened", "Despertado")
            entry = store._bindings()["AWAKENED"]
            store._establish(entry, "Acordado", "domain_term")
            self.assertEqual(entry["target"], "Despertado")
            self.assertGreaterEqual(entry["conflicts"], 1)

    def test_p31_compound_alignment_remains_non_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            entry = store._entry("DARKLORD", "Darklord", KIND_TERMINOLOGY, "recurring_region_alignment")
            entry.update({
                "authority": "learned_term",
                "alignment_type": "recurring_region_alignment_with_single_target_support",
                "evidence_count": 1,
                "target": "",
            })
            self.assertEqual(store.drift_reason("Darklord arrived", "O vilão chegou"), "")

    def test_p11_ordinary_concept_without_authority_is_neutral(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            self.assertEqual(store.drift_reason("The carrier arrived", "O menino chegou"), "")


if __name__ == "__main__":
    unittest.main()
