"""Persistent chapter terminology and proper-name ledger.

The rolling dialogue memory (``translations_used``) is bounded on purpose, and it
must stay bounded: an unbounded chapter transcript in every prompt is not a fix.
What must *not* expire is the much smaller set of terminology decisions - which
target form a recurring source term was bound to, and which spans are proper
names. Before this module existed the two were the same thing, so a decision
taken at the start of a chapter was forgotten before the end of it.

Every term here is synthetic. Nothing in production keys off any specific word,
chapter, page or job.
"""

import _test_bootstrap  # noqa: F401

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from ocr_balloon import (
    OCRLine,
    TextGroup,
    validate_and_retry_translations,
)
from session_context import SessionContextStore
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


def _group(
    text,
    translation="",
    classification="speech",
    names=(),
    group_id="T001",
    final_reason="",
):
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
    if final_reason:
        group.translation_final_reason = final_reason
    return group


def _filler_groups(count, start=0):
    """Unrelated dialogue, none of it carrying the tracked term."""
    return [
        _group(
            f"SOME OTHER LINE NUMBER {index} SPOKEN HERE",
            f"OUTRA FALA NUMERO {index} DITA AQUI",
            group_id=f"F{index:03}",
        )
        for index in range(start, start + count)
    ]


def _store(folder, chapter_url=CHAPTER_A, name="session_context.json"):
    return SessionContextStore(Path(folder) / name, chapter_url)


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


class TerminologyMemoryDoesNotExpireTests(unittest.TestCase):
    """RED 1 / GREEN 1: a binding survives far past the rolling window."""

    def test_binding_survives_more_items_than_the_rolling_window(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            groups = [_group("ZARQUON", "ZARQUONITE", group_id="G000")]
            groups.extend(_filler_groups(75))
            store.record_translations(groups)

            self.assertEqual(store.binding_for("ZARQUON"), "ZARQUONITE")
            fragment = store.prompt_fragment()
            self.assertIn("ZARQUON => ZARQUONITE", fragment)

    def test_binding_survives_a_reopened_chapter_store(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [_group("ZARQUON", "ZARQUONITE", group_id="G000")]
            )

            reopened = _store(folder)
            reopened.prepare([])
            self.assertEqual(reopened.binding_for("ZARQUON"), "ZARQUONITE")
            self.assertIn("ZARQUON => ZARQUONITE", reopened.prompt_fragment())


class RollingDialogueStaysBoundedTests(unittest.TestCase):
    """GREEN 2: terminology memory is not an excuse to send the transcript."""

    def test_dialogue_lines_stay_capped_while_bindings_do_not(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            groups = [_group("ZARQUON", "ZARQUONITE", group_id="G000")]
            groups.extend(_filler_groups(200))
            store.record_translations(groups)

            fragment = store.prompt_fragment()
            dialogue = fragment.partition("Traducoes ja usadas:\n")[2]
            dialogue_lines = [line for line in dialogue.splitlines() if line.strip()]
            # 201 translated items went in; the dialogue window is still capped.
            self.assertLessEqual(len(dialogue_lines), 60)
            self.assertNotIn("ZARQUON => ZARQUONITE", dialogue)
            # ...and the terminology decision is outside that window entirely.
            self.assertIn("ZARQUON => ZARQUONITE", fragment)
            self.assertLessEqual(len(store.data.get("term_bindings") or {}), 120)


class DialogueMemoryQualityGateTests(unittest.TestCase):
    """TDD #18: stale source-language memory must not poison provider prompts."""

    def test_dialogue_memory_rejects_source_language_residual_translations(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [
                    _group(
                        "I'M GOING TO QUIT BEING A HUNTER.",
                        "I'M GOING TO QUIT BEING A HUNTER.",
                    ),
                    _group(
                        "EVERYTIMEIGO INTOADUNGEON TRYINGTOGETRID OF THIS THING...",
                        "EVERY TIME I GO INTO A DUNGEON TRYING TO GET RID OF THIS THING...",
                    ),
                    _group(
                        "PLEASE WAIT HERE.",
                        "POR FAVOR, ESPERE AQUI.",
                    ),
                ]
            )

            fragment = store.prompt_fragment()

            self.assertIn("POR FAVOR, ESPERE AQUI", fragment)
            self.assertNotIn("=> I'M GOING TO QUIT BEING A HUNTER.", fragment)
            self.assertNotIn("=> EVERY TIME I GO INTO A DUNGEON", fragment)

    def test_prepare_drops_legacy_bad_translation_memory_for_same_chapter(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session_context.json"
            canonical_chapter_url = _store(folder).chapter_url
            path.write_text(
                json.dumps(
                    {
                        "version": "chapter-session-v2",
                        "chapter_url": canonical_chapter_url,
                        "translations_used": [
                            {
                                "source": "I'M GOING TO QUIT BEING A HUNTER.",
                                "translation": "I'M GOING TO QUIT BEING A HUNTER.",
                                "region_type": "speech",
                            },
                            {
                                "source": "EVERYTIMEIGO...",
                                "translation": "EVERY TIME I GO INTO A DUNGEON...",
                                "region_type": "speech",
                            },
                            {
                                "source": "PLEASE WAIT HERE.",
                                "translation": "POR FAVOR, ESPERE AQUI.",
                                "region_type": "speech",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            store = _store(folder)

            store.prepare([_group("I'M GOING TO QUIT BEING A HUNTER.")])

            translations = store.data["translations_used"]
            self.assertEqual(len(translations), 1)
            self.assertEqual(translations[0]["translation"], "POR FAVOR, ESPERE AQUI.")


class ProperNameContextTests(unittest.TestCase):
    """RED 2 / GREEN 3 / GREEN 4: names become real data in the real payload."""

    def test_model_confirmed_name_only_region_becomes_a_proper_name(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            # No chapter-consensus repair fired for this chapter, so
            # detected_proper_names is empty - exactly the real run's shape.
            store.record_translations(
                [
                    _group(
                        "NARAEK",
                        "NARAEK",
                        group_id="N001",
                        final_reason="proper_name_only",
                    )
                ]
            )
            self.assertEqual(store.binding_for("NARAEK"), "NARAEK")
            names = {str(item.get("text") or "").upper() for item in store.data["proper_names"]}
            self.assertIn("NARAEK", names)

    def test_proper_name_reaches_the_real_provider_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [
                    _group(
                        "NARAEK",
                        "NARAEK",
                        group_id="N001",
                        final_reason="proper_name_only",
                    )
                ]
            )
            translator = TranslatorNvidiaBatch(
                api_key="hermetic-stub-key",
                base_url="http://provider.invalid",
                model="stub-model",
                enable_cache=False,
                parallel=False,
            )
            translator.set_session_context(store)
            payloads = _capture_provider_payload(translator, ["WHERE DID HE GO?"])

            self.assertTrue(payloads)
            system_prompt = payloads[0][0]["content"]
            self.assertIn("NARAEK", system_prompt)

    def test_known_proper_name_binding_is_enforced_on_the_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [
                    _group(
                        "NARAEK",
                        "NARAEK",
                        group_id="N001",
                        final_reason="proper_name_only",
                    )
                ]
            )
            # A later candidate silently turns the known name into an ordinary word.
            self.assertTrue(store.drift_reason("NARAEK ARRIVED", "O CAVALO CHEGOU"))
            self.assertFalse(store.drift_reason("NARAEK ARRIVED", "NARAEK CHEGOU"))


class SameChapterConsistencyTests(unittest.TestCase):
    """RED 3 / GREEN 5: a drifting candidate is retried, never silently kept."""

    def test_drift_is_detected_and_retried_with_the_established_target(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [_group("ZARQUON", "ZARQUONITE", group_id="G000")]
            )

            drifted = _group(
                "THE ZARQUON IS CLOSED",
                "O PORTALIS ESTA FECHADO",
                group_id="G100",
            )
            translator = _StubTranslator("O ZARQUONITE ESTA FECHADO")
            records = validate_and_retry_translations(
                [drifted],
                translator,
                terminology_ledger=store,
            )

            self.assertTrue(
                any(call["validation_reason"] == "terminology_conflict" for call in translator.calls),
                translator.calls,
            )
            self.assertIn("ZARQUONITE", drifted.translation)
            self.assertTrue(
                any(record.get("reason") == "terminology_retry_ok" for record in records),
                records,
            )

    def test_unfixable_drift_ships_under_review_instead_of_english(self):
        # TDD #84F8: an unresolved terminology conflict is uncertainty about a word,
        # not proof the sentence is wrong. Withholding the whole region republished
        # the English source, so the usable PT-BR now ships flagged for review while
        # the conflict itself stays recorded and never counts as clean.
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [_group("ZARQUON", "ZARQUONITE", group_id="G000")]
            )
            drifted = _group(
                "THE ZARQUON IS CLOSED",
                "O PORTALIS ESTA FECHADO",
                group_id="G100",
            )
            translator = _StubTranslator("O PORTALIS ESTA FECHADO")
            validate_and_retry_translations(
                [drifted],
                translator,
                terminology_ledger=store,
            )
            self.assertFalse(drifted.translation_valid)
            self.assertEqual(drifted.translation, "O PORTALIS ESTA FECHADO")
            self.assertEqual(drifted.rejected_translation, "O PORTALIS ESTA FECHADO")
            self.assertTrue(drifted.manual_review_required)
            self.assertEqual(drifted.translation_quality_impact, "review_required")
            self.assertEqual(
                drifted.translation_final_reason,
                "terminology_conflict_after_retries",
            )


class ChapterIsolationTests(unittest.TestCase):
    """RED 4 / GREEN 6: no global translation memory."""

    def test_another_chapter_does_not_inherit_the_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            store_a = _store(folder, CHAPTER_A)
            store_a.prepare([])
            store_a.record_translations(
                [_group("ZARQUON", "ZARQUONITE", group_id="G000")]
            )

            store_b = _store(folder, CHAPTER_B)
            store_b.prepare([])
            self.assertEqual(store_b.binding_for("ZARQUON"), "")
            self.assertNotIn("ZARQUONITE", store_b.prompt_fragment())


class RetryContextTests(unittest.TestCase):
    """GREEN 7: a retry sees the ledger as it stands now."""

    def test_retry_prompt_carries_the_current_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            translator = TranslatorNvidiaBatch(
                api_key="hermetic-stub-key",
                base_url="http://provider.invalid",
                model="stub-model",
                enable_cache=False,
                parallel=False,
            )
            translator.set_session_context(store)
            store.record_translations(
                [_group("ZARQUON", "ZARQUONITE", group_id="G000")]
            )
            store.record_translations(_filler_groups(75))
            # The pipeline refreshes the translator before the retry pass.
            translator.set_session_context(store)

            captured = []

            def _fake_request(self, messages, *, deadline=None, response_format=None):
                captured.append([dict(message) for message in messages])
                return json.dumps({"BALAO_1": "O ZARQUONITE ESTA FECHADO"})

            with patch.object(TranslatorNvidiaBatch, "_request_with_retry", _fake_request):
                translator.translate_strict(
                    "THE ZARQUON IS CLOSED",
                    previous_translation="O PORTALIS ESTA FECHADO",
                    validation_reason="terminology_conflict",
                )

            self.assertTrue(captured)
            self.assertIn("ZARQUON => ZARQUONITE", captured[0][0]["content"])


class ConflictPolicyTests(unittest.TestCase):
    """GREEN 8: an established binding is never overwritten by a later one."""

    def test_established_binding_wins_and_the_conflict_is_counted(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [_group("ZARQUON", "ZARQUONITE", group_id="G000")]
            )
            store.record_translations(
                [_group("ZARQUON", "PORTALIS", group_id="G001")]
            )
            self.assertEqual(store.binding_for("ZARQUON"), "ZARQUONITE")
            self.assertEqual(store.summary()["binding_conflicts"], 1)


class TerminologyAuthorityPolicyTests(unittest.TestCase):
    """TDD #8: ordinary lexical observations are prompt hints, not hard law."""

    def test_ambiguous_compound_alignment_does_not_create_hard_token_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [
                    _group("VOID ARCANUM", "FEITIÇO DO VAZIO", group_id="G000"),
                    _group("DREAM ARCANUM", "MAGIA DO SONHO", group_id="G001"),
                ]
            )

            self.assertEqual(store.binding_for("ARCANUM"), "")
            self.assertEqual(
                store.drift_reason("THE ARCANUM IS ACTIVE", "O FEITIÇO ESTÁ ATIVO"),
                "",
            )

    def test_legacy_ambiguous_recurring_binding_is_prompt_only_not_hard_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session_context.json"
            canonical_chapter_url = _store(folder).chapter_url
            path.write_text(
                json.dumps(
                    {
                        "version": "chapter-session-v2",
                        "chapter_url": canonical_chapter_url,
                        "term_bindings": {
                            "ARCANUM": {
                                "source": "ARCANUM",
                                "target": "DO",
                                "kind": "terminology",
                                "authority": "learned_term",
                                "provenance": "recurring_region_alignment",
                                "observations": [],
                                "conflicts": 0,
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            store = _store(folder)
            store.prepare([])

            self.assertEqual(
                store.drift_reason("THE ARCANUM IS ACTIVE", "O FEITIÇO ESTÁ ATIVO"),
                "",
            )
            self.assertEqual(store.summary()["term_binding_prompt_only"], 1)

    def test_contraction_youre_does_not_hard_fail_contextual_portuguese(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [_group("YOU'RE BOTH S-RANK", "VOCÊS SÃO S-RANK")]
            )

            self.assertEqual(
                store.drift_reason("YOU'RE STRONG TOO.", "VOCÊ TAMBÉM É FORTE."),
                "",
            )

    def test_common_verb_stop_allows_contextual_inflection(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [_group("WE NEED TO STOP IT", "PRECISAMOS PARAR ISSO")]
            )

            self.assertEqual(
                store.drift_reason("STOP TALKING!", "PARA DE FALAR!"),
                "",
            )

    def test_quit_context_does_not_require_one_surface_verb(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [_group("I DIDN'T THINK I'D QUIT THIS SOON", "NÃO PENSEI QUE FOSSE DESISTIR TÃO CEDO")]
            )

            self.assertEqual(
                store.drift_reason(
                    "I'M GOING TO QUIT BEING A HUNTER.",
                    "VOU DEIXAR DE SER CAÇADOR.",
                ),
                "",
            )

    def test_contraction_thats_does_not_become_a_rigid_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [_group("THAT'S YOUR REASON?", "ESSE É O SEU MOTIVO?")]
            )

            self.assertEqual(
                store.drift_reason("THAT'S THE REASON.", "É POR ISSO."),
                "",
            )

    def test_generic_nouns_do_not_hard_bind_after_one_alignment(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations([_group("A MONSTER", "UMA CRIATURA")])

            self.assertEqual(
                store.drift_reason("A MONSTER APPEARED", "UM MONSTRO APARECEU"),
                "",
            )

    def test_expletive_or_dialogue_word_does_not_become_required_term(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations([_group("HOLY CRAP", "CARALHO")])

            self.assertEqual(
                store.drift_reason("HOLY CRAP", "MALDITO CARA"),
                "",
            )

    def test_common_perception_verb_does_not_become_required_term(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations([_group("LOOKS LIKE TROUBLE", "PARECE PROBLEMA")])

            self.assertEqual(
                store.drift_reason("IT LOOKS LIKE TROUBLE", "PARE QUE É PROBLEMA"),
                "",
            )

    def test_proper_name_remains_strict(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [_group("PAEHYEOK", "PAEHYEOK", final_reason="proper_name_only")]
            )

            self.assertTrue(
                store.drift_reason("PAEHYEOK ARRIVED", "ELE CHEGOU").startswith(
                    "terminology_conflict"
                )
            )
            self.assertEqual(
                store.drift_reason("PAEHYEOK ARRIVED", "PAEHYEOK CHEGOU"),
                "",
            )

    def test_domain_term_remains_strict_when_authoritative(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(
                [_group("THE DUNGEON OPENED", "A MASMORRA ABRIU")]
            )
            binding = store.data["term_bindings"]["DUNGEON"]
            binding["target"] = "MASMORRA"
            binding["authority"] = "established_domain_term"
            binding["provenance"] = "domain_term"

            self.assertTrue(
                store.drift_reason("THE DUNGEON IS CLOSED", "O PORTAL ESTÁ FECHADO").startswith(
                    "terminology_conflict"
                )
            )
            self.assertEqual(
                store.drift_reason("THE DUNGEON IS CLOSED", "A MASMORRA ESTÁ FECHADA"),
                "",
            )

    def test_domain_term_allows_gender_and_number_family(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations([_group("HUNTER", "CAÇADOR")])
            binding = store.data["term_bindings"]["HUNTER"]
            binding["target"] = "CAÇADOR"
            binding["authority"] = "established_domain_term"
            binding["provenance"] = "domain_term"

            self.assertEqual(
                store.drift_reason("HUNTER", "CAÇADORA"),
                "",
            )
            self.assertEqual(
                store.drift_reason("HUNTER", "CAÇADORES"),
                "",
            )

    def test_true_term_drift_still_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations([_group("ZARQUON", "ZARQUONITE")])

            self.assertTrue(
                store.drift_reason("THE ZARQUON IS CLOSED", "O PORTAL ESTÁ FECHADO").startswith(
                    "terminology_conflict"
                )
            )


class LedgerConcurrencyTests(unittest.TestCase):
    """GREEN 9: concurrent discovery of the same term stays single-valued."""

    def test_concurrent_recording_produces_one_stable_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            barrier = threading.Barrier(8)

            def _record(index):
                barrier.wait()
                store.record_translations(
                    [_group("ZARQUON", "ZARQUONITE", group_id=f"C{index:03}")]
                )

            threads = [threading.Thread(target=_record, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(store.binding_for("ZARQUON"), "ZARQUONITE")
            self.assertEqual(store.summary()["binding_conflicts"], 0)
            self.assertEqual(len(store.data["term_bindings"]), 1)


class LedgerSizeTests(unittest.TestCase):
    """Section 24: compact identity facts, never a transcript."""

    def test_ledger_grows_with_unique_terms_not_with_dialogue(self):
        with tempfile.TemporaryDirectory() as folder:
            store = _store(folder)
            store.prepare([])
            store.record_translations(_filler_groups(300))
            self.assertLessEqual(len(store.data["term_bindings"]), 120)


if __name__ == "__main__":
    unittest.main()
