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

    def test_unfixable_drift_requires_review_instead_of_trusting_conflict(self):
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
            self.assertEqual(drifted.translation, "")
            self.assertEqual(drifted.rejected_translation, "O PORTALIS ESTA FECHADO")
            self.assertTrue(drifted.manual_review_required)
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
