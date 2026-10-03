"""Chapter-scoped character registry: identity, gender and pronoun consistency.

The terminology ledger preserves *text* - which target form a source term was
bound to. It says nothing about the entity behind a name, so a character
established early with a feminine form of address could be addressed later with
a masculine one and nothing in the pipeline noticed. These tests pin the
entity-level memory: what we know about a character, why we believe it, and the
narrow contradictions that must not pass silently.

Every identity here is fictional. Production code keys off no specific name,
honorific, chapter, page or job.
"""

import _test_bootstrap  # noqa: F401

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from ocr_balloon import OCRLine, TextGroup, validate_and_retry_translations
from session_context import (
    CHARACTER_GENDER_CONFLICT_REASON,
    CHARACTER_MAX_IN_PROMPT,
    CHARACTER_PRONOUN_CONFLICT_REASON,
    GENDER_FEATURE,
    SessionContextStore,
)
from translator_nvidia import TranslatorNvidiaBatch


CHAPTER_A = "https://example.invalid/series/chapter-a"
CHAPTER_B = "https://example.invalid/series/chapter-b"


def _line(text, confidence=0.92):
    polygon = np.array([[10, 10], [210, 10], [210, 45], [10, 45]], dtype=np.int32)
    return OCRLine(
        text=text,
        confidence=confidence,
        polygon=polygon,
        box=(10, 10, 200, 35),
        raw_text=text,
        engine="rapidocr",
        page=1,
    )


def _group(text, translation="", classification="speech", names=(), group_id="T001"):
    group = TextGroup(
        group_id=group_id,
        lines=[_line(text)],
        text=text,
        classification=classification,
        inside_balloon_like_region=True,
        source_engine="rapidocr",
    )
    group.detected_proper_names = list(names)
    group.translation = translation
    group.translation_candidate = translation
    group.sent_to_translation = bool(translation)
    return group


def _filler_groups(count, start=0):
    return [
        _group(
            f"SOME OTHER LINE NUMBER {index} SPOKEN HERE",
            f"OUTRA FALA NUMERO {index} DITA AQUI",
            group_id=f"F{index:03}",
        )
        for index in range(start, start + count)
    ]


def _synthetic_name(index):
    """A distinct fictional name; digits are not part of a name token."""
    return f"PERSON{chr(65 + index // 26)}{chr(65 + index % 26)}"


def _store(folder, chapter_url=CHAPTER_A, name="session_context.json"):
    return SessionContextStore(Path(folder) / name, chapter_url)


def _feminine_chapter_store(folder, **kwargs):
    """A chapter where an explicit honorific establishes a feminine character."""
    store = _store(folder, **kwargs)
    store.prepare([])
    store.record_translations(
        [
            _group(
                "MISS NARIN ENTERED THE HALL",
                "SENHORITA NARIN ENTROU NO SALAO",
                names=["NARIN"],
                group_id="G000",
            )
        ]
    )
    return store


class _StubTranslator:
    """Provider stub: never calls out, records what it was asked to redo."""

    is_configured = True
    model = "stub"
    base_url = ""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def translate_strict(
        self,
        text,
        previous_translation="",
        validation_reason="",
        force=False,
        allow_proper_names=True,
        proper_names=None,
        **kwargs,
    ):
        self.calls.append(
            {
                "text": text,
                "validation_reason": validation_reason,
                "previous_translation": previous_translation,
            }
        )
        if self.responses:
            return self.responses.pop(0)
        return previous_translation


def _capture_provider_payload(translator, texts):
    """Drive the real batch payload builder and return the messages it sent."""
    captured = []

    def _fake_request(self, messages, *, deadline=None, response_format=None):
        captured.append([dict(message) for message in messages])
        return json.dumps(
            {f"BALAO_{index}": "TRADUZIDO" for index in range(1, len(texts) + 1)},
            ensure_ascii=False,
        )

    with patch.object(TranslatorNvidiaBatch, "_request_with_retry", _fake_request):
        translator.translate_many(texts, force=True)
    return captured


def _stub_provider():
    return TranslatorNvidiaBatch(api_key="test-key", enable_cache=False, parallel=False)


class CharacterMemoryDoesNotExpireTests(unittest.TestCase):
    """RED 1 / GREEN 1+20: a character fact survives far past the window."""

    def test_character_fact_survives_more_items_than_the_rolling_window(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            store.record_translations(_filler_groups(75))

            facts = store.character_facts("NARIN")
            self.assertEqual(facts["gender"], "feminine")
            self.assertEqual(facts["pronouns"], ["ela", "dela"])
            self.assertIn("NARIN", store.prompt_fragment())
            self.assertIn("feminino", store.prompt_fragment())

    def test_rolling_dialogue_window_stays_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            store.record_translations(_filler_groups(300))
            self.assertLessEqual(len(store.data["translations_used"]), 250)
            self.assertEqual(store.character_facts("NARIN")["gender"], "feminine")

    def test_character_fact_survives_reopening_and_repreparing(self):
        with tempfile.TemporaryDirectory() as folder:
            _feminine_chapter_store(folder)
            reopened = _store(folder)
            reopened.prepare([])
            self.assertEqual(reopened.character_facts("NARIN")["gender"], "feminine")


class ProviderReceivesCharacterContextTests(unittest.TestCase):
    """RED 1 / GREEN 21: proven at the real provider payload boundary."""

    def test_batch_system_prompt_carries_the_character_fact(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            store.record_translations(_filler_groups(75))
            translator = _stub_provider()

            translator.set_session_context(None)
            baseline = _capture_provider_payload(translator, ["ALGUMA FALA"])
            self.assertNotIn("NARIN", baseline[0][0]["content"])

            translator.set_session_context(store)
            captured = _capture_provider_payload(translator, ["ALGUMA FALA"])
            system_prompt = captured[0][0]["content"]
            self.assertIn("NARIN", system_prompt)
            self.assertIn("feminino", system_prompt)
            self.assertIn("ela", system_prompt)

    def test_unknown_character_contributes_no_fabricated_facts(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.observe_character("TELVAR")
            translator = _stub_provider()
            translator.set_session_context(store)
            system_prompt = _capture_provider_payload(translator, ["FALA"])[0][0][
                "content"
            ]
            self.assertNotIn("TELVAR", system_prompt)
            self.assertEqual(store.character_facts("TELVAR")["gender"], "")
            self.assertEqual(store.character_facts("TELVAR")["pronouns"], [])

    def test_character_section_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            for index in range(40):
                store.observe_character(
                    _synthetic_name(index),
                    feature=GENDER_FEATURE,
                    value="feminine",
                    provenance="structured_metadata",
                    confidence=0.9,
                )
            self.assertEqual(len(store.characters()), 40)
            self.assertEqual(
                len(store.character_prompt_lines()), CHARACTER_MAX_IN_PROMPT
            )


class GenderAgreementTests(unittest.TestCase):
    """RED 2 / GREEN 22: a contradicting form of address cannot pass silently."""

    def test_contradicting_form_of_address_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            reason = store.character_conflict_reason(
                "MISS NARIN LOOKED AWAY", "O CACADOR NARIN DESVIOU O OLHAR"
            )
            self.assertTrue(reason.startswith(CHARACTER_GENDER_CONFLICT_REASON))
            self.assertIn("NARIN", reason)

    def test_agreeing_form_of_address_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            self.assertFalse(
                store.character_conflict_reason(
                    "MISS NARIN LOOKED AWAY", "A CACADORA NARIN DESVIOU O OLHAR"
                )
            )


class PronounDriftTests(unittest.TestCase):
    """RED 3: a contradicting pronoun for a resolved character is reported."""

    def test_contradicting_pronoun_is_reported(self):
        # The name is kept in the candidate, so the terminology ledger has
        # nothing to say: only the pronoun contradicts the character.
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            reason = store.character_conflict_reason(
                "NARIN LEFT THE ROOM AND STAYED SILENT",
                "NARIN SAIU DA SALA E ELE FICOU CALADO",
            )
            self.assertTrue(reason.startswith(CHARACTER_PRONOUN_CONFLICT_REASON))
            self.assertIn("NARIN", reason)

    def test_matching_pronoun_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            self.assertFalse(
                store.character_conflict_reason(
                    "NARIN LEFT THE ROOM AND STAYED SILENT",
                    "NARIN SAIU DA SALA E ELA FICOU CALADA",
                )
            )

    def test_pronoun_conflict_reaches_the_retry_with_its_own_reason_code(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            group = _group(
                "NARIN LEFT THE ROOM AND STAYED SILENT",
                "NARIN SAIU DA SALA E ELE FICOU CALADO",
                names=["NARIN"],
                group_id="P001",
            )
            translator = _StubTranslator("NARIN SAIU DA SALA E ELA FICOU CALADA")
            validate_and_retry_translations(
                [group], translator, force=True, terminology_ledger=store
            )
            self.assertEqual(
                translator.calls[0]["validation_reason"],
                CHARACTER_PRONOUN_CONFLICT_REASON,
            )
            self.assertEqual(group.translation, "NARIN SAIU DA SALA E ELA FICOU CALADA")


class FalsePositiveControlTests(unittest.TestCase):
    """RED 24: a gendered word belonging to somebody else is not a conflict."""

    def test_another_persons_gendered_noun_is_not_a_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            self.assertFalse(
                store.character_conflict_reason(
                    "MISS NARIN SPOKE TO THE GUARD",
                    "A SENHORITA NARIN FALOU COM O GUARDA VETERANO",
                )
            )

    def test_pronoun_in_a_prepositional_phrase_is_not_a_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            self.assertFalse(
                store.character_conflict_reason(
                    "NARIN LOOKED AT HIM", "NARIN OLHOU PARA ELE"
                )
            )

    def test_pronoun_the_source_itself_spelled_out_is_not_a_conflict(self):
        # Found by an offline replay of a real chapter: a region whose source
        # said "he" about somebody else, in a sentence that merely contained a
        # known character's family name. The target pronoun renders the source
        # word, so there is nothing to correct.
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            self.assertFalse(
                store.character_conflict_reason(
                    "HE WILL CONTACT THE NARIN ESTATE",
                    "ELE VAI CONTATAR A PROPRIEDADE NARIN",
                )
            )

    def test_two_characters_in_one_region_produce_no_finding(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            store.record_translations(
                [
                    _group(
                        "MR TELVAR BOWED",
                        "SENHOR TELVAR SE CURVOU",
                        names=["TELVAR"],
                        group_id="G001",
                    )
                ]
            )
            self.assertFalse(
                store.character_conflict_reason(
                    "NARIN ANSWERED TELVAR", "ELE RESPONDEU"
                )
            )

    def test_region_without_any_known_character_produces_no_finding(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            self.assertFalse(
                store.character_conflict_reason("THE DOOR OPENED", "ELE ABRIU A PORTA")
            )


class UnknownStaysUnknownTests(unittest.TestCase):
    """RED 4 + RED 6: never invent, never block."""

    def test_character_without_evidence_stays_unknown_and_blocks_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [_group("TELVAR NODDED", "TELVAR CONCORDOU", names=["TELVAR"])]
            )
            store.observe_character("TELVAR")
            facts = store.character_facts("TELVAR")
            self.assertEqual(facts["gender"], "")
            self.assertEqual(facts["pronouns"], [])
            self.assertFalse(
                store.character_conflict_reason("TELVAR NODDED", "ELE CONCORDOU")
            )

    def test_gender_neutral_honorific_creates_no_gender_fact(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [
                    _group(
                        "DR NARIN ARRIVED",
                        "DRA NARIN CHEGOU",
                        names=["NARIN"],
                        group_id="G000",
                    )
                ]
            )
            facts = store.character_facts("NARIN")
            self.assertEqual(facts["gender"], "")
            self.assertIn("DR NARIN", facts["aliases"])
            self.assertFalse(
                store.character_conflict_reason("DR NARIN ARRIVED", "ELE CHEGOU")
            )

    def test_appearance_or_capitalisation_alone_creates_no_fact(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [
                    _group(
                        "NARIN HAD LONG HAIR AND A SOFT VOICE",
                        "NARIN TINHA CABELO LONGO E VOZ SUAVE",
                        names=["NARIN"],
                    )
                ]
            )
            self.assertEqual(store.character_facts("NARIN").get("gender", ""), "")


class AliasTests(unittest.TestCase):
    """RED 9: title + name resolves to one record, unrelated people never merge."""

    def test_title_and_bare_name_resolve_to_one_character(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [
                    _group(
                        "DR NARIN CHECKED THE WOUND",
                        "DRA NARIN EXAMINOU O FERIMENTO",
                        names=["NARIN"],
                        group_id="G000",
                    ),
                    _group(
                        "MISS NARIN SIGHED",
                        "SENHORITA NARIN SUSPIROU",
                        names=["NARIN"],
                        group_id="G001",
                    ),
                ]
            )
            self.assertEqual(len(store.characters()), 1)
            facts = store.character_facts("NARIN")
            self.assertEqual(facts["gender"], "feminine")
            self.assertEqual(
                sorted(facts["aliases"]), ["DR NARIN", "MISS NARIN"]
            )
            self.assertEqual(sorted(facts["forms_of_address"]), ["DR", "MISS"])

    def test_similar_names_are_never_merged(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [
                    _group(
                        "MISS NARIN WAITED",
                        "SENHORITA NARIN ESPEROU",
                        names=["NARIN"],
                        group_id="G000",
                    ),
                    _group(
                        "MR NARIK WAITED",
                        "SENHOR NARIK ESPEROU",
                        names=["NARIK"],
                        group_id="G001",
                    ),
                ]
            )
            self.assertEqual(sorted(store.characters()), ["NARIK", "NARIN"])
            self.assertEqual(store.character_facts("NARIN")["gender"], "feminine")
            self.assertEqual(store.character_facts("NARIK")["gender"], "masculine")


class ChapterIsolationTests(unittest.TestCase):
    """RED 5: no character memory leaks between chapters."""

    def test_second_chapter_does_not_inherit_gender(self):
        with tempfile.TemporaryDirectory() as folder:
            _feminine_chapter_store(folder)
            other = _store(folder, chapter_url=CHAPTER_B)
            other.prepare([])
            self.assertEqual(other.characters(), {})
            other.record_translations(
                [_group("NARIN NODDED", "NARIN CONCORDOU", names=["NARIN"])]
            )
            self.assertEqual(other.character_facts("NARIN").get("gender", ""), "")
            self.assertFalse(
                other.character_conflict_reason("NARIN NODDED", "ELE CONCORDOU")
            )

    def test_separate_stores_do_not_share_state(self):
        with tempfile.TemporaryDirectory() as folder:
            _feminine_chapter_store(folder)
            other = _feminine_chapter_store(
                folder, chapter_url=CHAPTER_B, name="other_context.json"
            )
            other.observe_character(
                "TELVAR",
                feature=GENDER_FEATURE,
                value="masculine",
                provenance="structured_metadata",
                confidence=0.9,
            )
            self.assertNotIn("TELVAR", _store(folder).characters())


class ConflictingEvidenceTests(unittest.TestCase):
    """RED 10 / section 26: deterministic, never last-write-wins."""

    def test_weaker_contradicting_evidence_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            store.observe_character(
                "NARIN",
                feature=GENDER_FEATURE,
                value="masculine",
                provenance="structured_metadata",
                confidence=0.2,
            )
            facts = store.character_facts("NARIN")
            self.assertEqual(facts["gender"], "feminine")
            self.assertFalse(facts["conflict"])
            self.assertEqual(len(facts["evidence"]), 2)

    def test_equally_trusted_contradiction_falls_back_to_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            store.record_translations(
                [
                    _group(
                        "MR NARIN LEFT",
                        "SENHOR NARIN SAIU",
                        names=["NARIN"],
                        group_id="G001",
                    )
                ]
            )
            facts = store.character_facts("NARIN")
            self.assertEqual(facts["gender"], "")
            self.assertTrue(facts["conflict"])
            self.assertEqual(store.summary()["character_evidence_conflicts"], 1)
            self.assertFalse(
                store.character_conflict_reason("NARIN LEFT", "ELE SAIU")
            )

    def test_observation_order_does_not_change_the_outcome(self):
        with tempfile.TemporaryDirectory() as folder:
            observations = [
                {"value": "feminine", "provenance": "explicit_honorific", "confidence": 0.9},
                {"value": "masculine", "provenance": "structured_metadata", "confidence": 0.2},
                {"value": "feminine", "provenance": "validated_translation", "confidence": 0.5},
            ]
            results = []
            for order in (observations, list(reversed(observations))):
                store = _store(folder, name=f"ctx_{len(results)}.json")
                store.prepare([])
                for item in order:
                    store.observe_character("NARIN", feature=GENDER_FEATURE, **item)
                results.append(store.character_facts("NARIN")["gender"])
            self.assertEqual(results, ["feminine", "feminine"])


class ConcurrencyTests(unittest.TestCase):
    """Section 28: concurrent observation converges on one deterministic state."""

    def test_concurrent_observations_are_not_lost_or_order_dependent(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            barrier = threading.Barrier(6)

            def observe(index):
                barrier.wait()
                store.observe_character(
                    _synthetic_name(index),
                    feature=GENDER_FEATURE,
                    value="feminine" if index % 2 else "masculine",
                    provenance="structured_metadata",
                    confidence=0.9,
                )

            threads = [threading.Thread(target=observe, args=(index,)) for index in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            characters = store.characters()
            self.assertEqual(len(characters), 6)
            self.assertEqual(
                sorted(facts["gender"] for facts in characters.values()),
                ["feminine"] * 3 + ["masculine"] * 3,
            )


class TermLedgerIntegrationTests(unittest.TestCase):
    """RED 13: one authority for text, one for attributes, no duplication."""

    def test_registry_reinforces_name_protection_without_owning_the_spelling(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            self.assertIn(
                "NARIN",
                {
                    str(item.get("text") or "").upper()
                    for item in store.data.get("proper_names", [])
                },
            )
            # Text mapping stays the ledger's decision, not the registry's.
            self.assertEqual(store.binding_for("NARIN"), "NARIN")
            self.assertNotIn("target", store.character_facts("NARIN"))

    def test_terminology_drift_still_wins_over_character_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            store.record_translations(
                [_group("ZARQUON", "ZARQUONITE", group_id="G001")]
            )
            self.assertTrue(
                store.drift_reason("ZARQUON APPEARED", "O OBJETO APARECEU").startswith(
                    "terminology_conflict"
                )
            )


class CharacterRetryTests(unittest.TestCase):
    """RED 3 / GREEN 29: the retry names the constraint it wants respected."""

    def _conflicting_group(self):
        return _group(
            "MISS NARIN LOOKED AWAY",
            "O CACADOR NARIN DESVIOU O OLHAR",
            names=["NARIN"],
            group_id="R001",
        )

    def test_conflict_triggers_a_retry_and_is_resolved(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            group = self._conflicting_group()
            translator = _StubTranslator("A CACADORA NARIN DESVIOU O OLHAR")
            records = validate_and_retry_translations(
                [group], translator, force=True, terminology_ledger=store
            )
            self.assertEqual(len(translator.calls), 1)
            self.assertEqual(
                translator.calls[0]["validation_reason"],
                CHARACTER_GENDER_CONFLICT_REASON,
            )
            self.assertEqual(group.translation, "A CACADORA NARIN DESVIOU O OLHAR")
            self.assertTrue(
                any(record["reason"] == "character_gender_retry_ok" for record in records)
            )
            self.assertEqual(store.summary()["character_consistency_retries"], 1)

    def test_unresolved_conflict_requires_review_instead_of_trusting_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            group = self._conflicting_group()
            translator = _StubTranslator("O CACADOR NARIN DESVIOU O OLHAR")
            records = validate_and_retry_translations(
                [group], translator, force=True, terminology_ledger=store
            )
            self.assertFalse(group.translation_valid)
            self.assertEqual(group.translation, "")
            self.assertEqual(group.rejected_translation, "O CACADOR NARIN DESVIOU O OLHAR")
            self.assertTrue(group.manual_review_required)
            self.assertEqual(
                group.translation_final_reason,
                "character_gender_conflict_after_retries",
            )
            self.assertTrue(
                any(
                    record["reason"] == "character_gender_conflict_unresolved"
                    for record in records
                )
            )

    def test_retry_provider_context_carries_the_character_constraint(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _feminine_chapter_store(folder)
            translator = _stub_provider()
            translator.set_session_context(store)
            captured = []

            def _fake_request(self, messages, *, deadline=None, response_format=None):
                captured.append([dict(message) for message in messages])
                return json.dumps({"BALAO_1": "A CACADORA NARIN DESVIOU O OLHAR"})

            with patch.object(TranslatorNvidiaBatch, "_request_with_retry", _fake_request):
                translator.translate_strict(
                    "MISS NARIN LOOKED AWAY",
                    previous_translation="O CACADOR NARIN DESVIOU O OLHAR",
                    validation_reason=CHARACTER_GENDER_CONFLICT_REASON,
                    force=True,
                    proper_names=["NARIN"],
                )

            system_prompt = captured[0][0]["content"]
            self.assertIn("NARIN", system_prompt)
            self.assertIn("feminino", system_prompt)
            self.assertIn("genero", system_prompt)


if __name__ == "__main__":
    unittest.main()
