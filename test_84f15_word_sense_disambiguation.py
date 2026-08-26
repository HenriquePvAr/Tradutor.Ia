"""#84F15 - a clean source, fluent Portuguese, and the wrong sense of the word.

p046:BALAO_1 reads ``PRECINCT 7`` and shipped ``7 DISTRITO ELEITORAL``.  Nothing
upstream is broken: the OCR read it perfectly, the Portuguese is well formed,
every fidelity invariant holds - numbers, names, negation, tense.  The provider
simply picked the electoral sense of an ambiguous noun in a story whose own
page says ``EMERGENCY CONTAINMENT VAULT``.

The defect class is lexical sense selection, so the contract here is about
sense evidence, never about this page or this word: an ambiguous source term,
a candidate that commits to one sense of it, and bounded context that supports
a different sense - or none at all.
"""

import _test_bootstrap  # noqa: F401

import unittest

import benchmark_pipeline
import semantic_fidelity
from ocr_balloon import TextGroup, validate_and_retry_translations


# The persisted p046 evidence, verbatim from .cache/processed.
PRECINCT_SOURCE = "PRECINCT 7"
PRECINCT_BAD = "7 DISTRITO ELEITORAL"
# Not a real provider answer: a hand-written stand-in for a faithful candidate.
PRECINCT_GOOD_FAKE = "DISTRITO POLICIAL 7"
PAGE_046_CONTEXT = ("GRRR...", "AHHH!!", "EMERGENCY CONTAINMENT VAULT")


def _evaluate(source, candidate, context=(), classification="speech"):
    return semantic_fidelity.evaluate_local_fidelity(
        source,
        candidate,
        classification=classification,
        context_texts=context,
    )


def _usability(finding):
    return semantic_fidelity.review_usability(finding.reason())


class WordSenseEvidence(unittest.TestCase):
    """#84F15-1 - three conditions, all required, none of them page-specific."""

    def test_police_context_with_an_electoral_candidate_is_not_clean(self):
        finding = _evaluate(
            "REPORT TO PRECINCT 7",
            PRECINCT_BAD,
            ("THE OFFICERS ARE WAITING AT THE STATION.",),
        )
        self.assertEqual(finding.status, semantic_fidelity.REVIEW)
        self.assertEqual(
            finding.primary_reason, semantic_fidelity.WORD_SENSE_CONTEXT_MISMATCH
        )
        self.assertEqual(_usability(finding), semantic_fidelity.REVIEW_UNUSABLE)

    def test_electoral_context_keeps_the_electoral_candidate_clean(self):
        finding = _evaluate(
            "PRECINCT 7",
            PRECINCT_BAD,
            ("THE VOTERS QUEUED AT THE POLLING STATION.", "COUNT THE BALLOTS."),
        )
        self.assertTrue(finding.faithful, finding.reason())

    def test_no_context_at_all_is_review_not_a_forced_sense(self):
        finding = _evaluate(PRECINCT_SOURCE, PRECINCT_BAD)
        self.assertEqual(finding.status, semantic_fidelity.REVIEW)
        self.assertEqual(
            finding.primary_reason, semantic_fidelity.WORD_SENSE_CONTEXT_MISMATCH
        )

    def test_a_candidate_that_commits_to_no_sense_is_never_flagged(self):
        """The sense marker is the third condition, and it really is required."""
        self.assertTrue(
            _evaluate(
                "PRECINCT 7", "DISTRITO 7", ("THE OFFICERS ARE WAITING.",)
            ).faithful
        )

    def test_a_source_without_an_ambiguous_term_is_never_flagged(self):
        self.assertTrue(
            _evaluate(
                "THE ELECTION IS TOMORROW.",
                "A ELEICAO E AMANHA.",
                ("THE OFFICERS ARE WAITING.",),
            ).faithful
        )


class GenericWordSenseControl(unittest.TestCase):
    """#84F15-2 - the mechanism is a table, not a Precinct rule."""

    def test_prison_context_rejects_the_biological_sense(self):
        finding = _evaluate(
            "GET BACK IN YOUR CELL.",
            "VOLTE PARA SUA CELULA.",
            ("THE GUARD LOCKED THE PRISONER IN.",),
        )
        self.assertEqual(finding.status, semantic_fidelity.REVIEW)
        self.assertEqual(
            finding.primary_reason, semantic_fidelity.WORD_SENSE_CONTEXT_MISMATCH
        )

    def test_biological_context_keeps_the_biological_sense_clean(self):
        self.assertTrue(
            _evaluate(
                "EVERY CELL IN HIS BODY BURNED.",
                "CADA CELULA DO SEU CORPO QUEIMAVA.",
                ("THE BLOOD SAMPLE WAS UNDER THE MICROSCOPE.",),
            ).faithful
        )

    def test_the_rule_is_the_table_and_nothing_else(self):
        """Drop the entry and the finding disappears: no term-specific branch."""
        table = dict(semantic_fidelity.AMBIGUOUS_WORD_SENSES)
        table.pop("precinct")
        original = semantic_fidelity.AMBIGUOUS_WORD_SENSES
        semantic_fidelity.AMBIGUOUS_WORD_SENSES = table
        try:
            self.assertTrue(_evaluate(PRECINCT_SOURCE, PRECINCT_BAD).faithful)
            self.assertFalse(
                _evaluate(
                    "GET BACK IN YOUR CELL.",
                    "VOLTE PARA SUA CELULA.",
                    ("THE GUARD LOCKED THE PRISONER IN.",),
                ).faithful
            )
        finally:
            semantic_fidelity.AMBIGUOUS_WORD_SENSES = original

    def test_prison_context_keeps_the_prison_sense_clean(self):
        self.assertTrue(
            _evaluate(
                "GET BACK IN YOUR CELL.",
                "VOLTE PARA SUA CELA.",
                ("THE GUARD LOCKED THE PRISONER IN.",),
            ).faithful
        )


class PrecinctOfflineReplay(unittest.TestCase):
    """#84F15-3 - the persisted p046 candidate, replayed with no provider."""

    def test_the_persisted_candidate_is_no_longer_clean(self):
        finding = _evaluate(PRECINCT_SOURCE, PRECINCT_BAD, PAGE_046_CONTEXT)
        self.assertEqual(finding.status, semantic_fidelity.REVIEW)
        self.assertEqual(_usability(finding), semantic_fidelity.REVIEW_UNUSABLE)

    def test_a_faithful_candidate_for_the_same_region_stays_clean(self):
        finding = _evaluate(PRECINCT_SOURCE, PRECINCT_GOOD_FAKE, PAGE_046_CONTEXT)
        self.assertTrue(finding.faithful, finding.reason())

    def test_every_candidate_keeping_the_wrong_sense_stays_unsafe(self):
        for candidate in (PRECINCT_BAD, "DISTRITO ELEITORAL 7", "ZONA ELEITORAL 7"):
            with self.subTest(candidate=candidate):
                finding = _evaluate(PRECINCT_SOURCE, candidate, PAGE_046_CONTEXT)
                self.assertFalse(finding.faithful)
                self.assertEqual(_usability(finding), semantic_fidelity.REVIEW_UNUSABLE)


class WordSenseRetryGuidance(unittest.TestCase):
    """#84F15-4 - the finding carries a constraint, never the source text."""

    def test_the_mismatch_maps_to_a_word_sense_constraint(self):
        self.assertEqual(
            semantic_fidelity.retry_constraint(
                semantic_fidelity.WORD_SENSE_CONTEXT_MISMATCH + ":precinct>electoral"
            ),
            "preserve_word_sense",
        )

    def test_it_is_a_recognised_fidelity_reason(self):
        self.assertTrue(
            semantic_fidelity.is_fidelity_reason(
                semantic_fidelity.WORD_SENSE_CONTEXT_MISMATCH + ":precinct>electoral"
            )
        )

    def test_the_provider_has_an_instruction_for_it(self):
        import translator_nvidia

        class _Named:
            source_language = "en"
            _target_language_name = lambda self: "portugues"  # noqa: E731
            _quality_retry_instruction = (
                translator_nvidia.TranslatorNvidiaBatch._quality_retry_instruction
            )

        instruction = _Named()._quality_retry_instruction(
            semantic_fidelity.WORD_SENSE_CONTEXT_MISMATCH + ":precinct>electoral"
        )
        self.assertIn("sentido", instruction.lower())
        self.assertNotIn("precinct", instruction.lower())


class PageContextIsWired(unittest.TestCase):
    """#84F15-5 - the validator really is given the neighbouring regions."""

    def _group(self, text, translation, group_id):
        group = TextGroup(
            group_id=group_id,
            lines=[],
            text=text,
            classification="speech",
            inside_balloon_like_region=True,
            source_engine="rapidocr",
        )
        group.translation = translation
        group.translation_candidate = translation
        group.sent_to_translation = True
        return group

    def test_a_sibling_region_supplies_the_domain_evidence(self):
        target = self._group(PRECINCT_SOURCE, PRECINCT_BAD, "BALAO_1")
        sibling = self._group(
            "EMERGENCY CONTAINMENT VAULT",
            "COFRE DE CONTENCAO DE EMERGENCIA",
            "BALAO_2",
        )
        validate_and_retry_translations([target, sibling], None, fidelity_stats={})
        self.assertTrue(
            target.semantic_review_reason.startswith(
                semantic_fidelity.WORD_SENSE_CONTEXT_MISMATCH
            ),
            target.semantic_review_reason,
        )
        self.assertEqual(sibling.semantic_review_reason, "")


class WordSenseAccounting(unittest.TestCase):
    """#84F15-6 - a wrong sense can never be counted as clean output."""

    def test_the_flagged_region_blocks_setup_readiness(self):
        report = benchmark_pipeline._user_visible_output_accounting([
            {
                "index": 46,
                "debug_data": {
                    "items": [
                        {
                            "id": "BALAO_1",
                            "classification": "speech",
                            "clean_text": PRECINCT_SOURCE,
                            "translation": PRECINCT_BAD,
                            "translation_valid": True,
                            "translation_final_state": "translated",
                            "translation_final_reason": "ok",
                            "semantic_review_reason": (
                                semantic_fidelity.WORD_SENSE_CONTEXT_MISMATCH
                                + ":precinct>electoral"
                            ),
                            "render_disposition": "render_with_review",
                            "redrawn": True,
                        }
                    ]
                },
            }
        ])
        self.assertEqual(report["semantic_review_unusable"], 1)
        self.assertEqual(report["semantic_clean"], 0)
        self.assertEqual(report["semantic_bad_clean"], 0)
        self.assertFalse(report["setup_ready"])


class NoFalsePositiveExplosion(unittest.TestCase):
    """#84F15-7 - measured against every persisted region, not asserted."""

    def test_the_persisted_corpus_flags_only_the_precinct_region(self):
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parent / ".cache" / "processed"
        if not root.is_dir():
            self.skipTest("no persisted run in this checkout")
        pages, flagged = {}, []
        for path in sorted(root.glob("*.json")):
            try:
                debug = json.loads(path.read_text(encoding="utf-8"))["debug_data"]
            except Exception:  # noqa: BLE001 - a stale cache entry is not a failure.
                continue
            for item in debug.get("items") or []:
                key = (item.get("page"), item.get("id"), item.get("clean_text"))
                pages.setdefault(item.get("page"), {})[key] = item
        if not pages:
            self.skipTest("no persisted regions in this checkout")
        for page, items in pages.items():
            texts = [str(item.get("clean_text") or "") for item in items.values()]
            for key, item in items.items():
                translation = str(item.get("translation") or "")
                if not translation:
                    continue
                source = str(item.get("clean_text") or "")
                context = tuple(text for text in texts if text != source)
                conflicts = semantic_fidelity.word_sense_conflicts(
                    source, translation, context
                )
                if conflicts:
                    flagged.append((page, key[1], source, translation, conflicts))
        self.assertEqual(
            [(row[0], row[1]) for row in flagged], [(46, "BALAO_1")], flagged
        )


if __name__ == "__main__":
    unittest.main()
