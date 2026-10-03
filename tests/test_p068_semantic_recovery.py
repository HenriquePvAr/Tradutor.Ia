"""TDD #84F5 - P068: a semantic rejection must have somewhere to go.

The real #84F3 run left ordinary English on page 68.  Replaying the persisted
artefacts of that run shows three separate defects on the one region, in the
order the pipeline hits them:

1. ``ocr_metadata.fallback_reason == "incomplete_group_after_selective_fallback"``
   escalated the whole page to Paddle, Paddle returned **zero** lines, and the
   escalation was accepted verbatim.  ``group_count`` went 2 -> 0, nothing was
   translated, nothing was inpainted, and the untouched English page went into
   the PDF.  The semantic gate never saw the region at all.
2. The source RapidOCR did read, ``"...FORTHENEAREST AWAKENEDTO GET HERE."``,
   glues the chapter's own class noun to the next function word.  That is the
   shape that turned "the nearest Awakened" into "depois que acordar".
3. The configured provider for that run was DeepL, and ``DeepLTranslator`` has
   no ``translate_strict``.  ``validate_and_retry_translations`` gates every
   retry on ``hasattr(translator, "translate_strict")``, so under DeepL a
   semantic rejection had *no* second attempt - safety with no recovery.

Everything here is offline: no job, no provider, no network.  The good second
candidate is a TEST fixture, never a persisted real DeepL answer.
"""

import _test_bootstrap  # noqa: F401
from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import unittest

import numpy as np

import benchmark_pipeline
import translator_deepl
from ocr_balloon import (
    OCRLine,
    TextGroup,
    detect_proper_name_spans,
    recover_protected_term_boundaries,
    validate_and_retry_translations,
)

# --- real persisted #84F3 evidence (read-only replay) ------------------------
# output/shadow_slave_chapter_1_5/00cc718e-.../progress.json  pages[67]
P068_RAW = "TAKEAFEWHOURS FORTHENEAREST AWAKENEDTO GET HERE."
P068_BAD = "LEVE ALGUMAS HORAS PARA CHEGAR AQUI, DEPOIS QUE ACORDAR."
# TEST fixture only - what a faithful candidate looks like, never a real answer.
P068_GOOD = "VAI LEVAR ALGUMAS HORAS PARA O DESPERTO MAIS PROXIMO CHEGAR AQUI."

# The chapter spells the anchor on its own, repeatedly (pages 34/37/40 of the
# same persisted run).
CHAPTER_CONTEXT = (
    "IN TIME, THE AWAKENED BECAME HUMANITY'S HOPE,",
    "SO THEIR CHILDREN COULD RECEIVE SPECIAL TRAINING FOR BECOMING AWAKENED.",
)


def _line(text, box=(10, 10, 200, 35)):
    polygon = np.array(
        [[box[0], box[1]], [box[0] + box[2], box[1]],
         [box[0] + box[2], box[1] + box[3]], [box[0], box[1] + box[3]]],
        dtype=np.int32,
    )
    return OCRLine(
        text=text, confidence=0.94, polygon=polygon, box=box,
        raw_text=text, engine="rapidocr", page=1,
    )


def _group(text, translation="", group_id="BALAO_1", classification="narration"):
    group = TextGroup(
        group_id=group_id,
        lines=[_line(text)],
        text=text,
        classification=classification,
        inside_balloon_like_region=True,
        source_engine="rapidocr",
    )
    group.detected_proper_names = list(detect_proper_name_spans(text))
    group.translation = translation
    group.translation_candidate = translation
    group.sent_to_translation = bool(translation)
    return group


def _chapter(*extra_texts):
    return [_group(text, group_id=f"BALAO_{i}") for i, text in enumerate(extra_texts)]


# =============================================================================
# 1. the page-level OCR escalation must not erase the page
# =============================================================================
class DestructivePageFallbackContract(unittest.TestCase):
    """#84F3 root: an empty Paddle answer replaced a readable RapidOCR page."""

    def test_empty_fallback_never_replaces_readable_lines(self):
        current = [_line(P068_RAW), _line("OR AT LEAST... DON'T DIE RIGHT AWAY.")]
        self.assertTrue(
            benchmark_pipeline._fallback_discards_source_text(current, [])
        )

    def test_equivalent_fallback_is_accepted(self):
        current = [_line(P068_RAW)]
        better = [_line("TAKE A FEW HOURS FOR THE NEAREST AWAKENED TO GET HERE.")]
        self.assertFalse(
            benchmark_pipeline._fallback_discards_source_text(current, better)
        )

    def test_partial_loss_is_rejected(self):
        current = [_line(P068_RAW), _line("OR AT LEAST... DON'T DIE RIGHT AWAY.")]
        self.assertTrue(
            benchmark_pipeline._fallback_discards_source_text(current, [_line("GET")])
        )

    def test_empty_page_may_still_be_replaced(self):
        # Nothing to lose: the escalation is exactly what an unread page needs.
        self.assertFalse(benchmark_pipeline._fallback_discards_source_text([], []))


# =============================================================================
# 2. protected-term boundary recovery (generic, terminology-anchored)
# =============================================================================
class ProtectedTermBoundaryContract(unittest.TestCase):
    def test_known_term_joined_to_function_word_is_split(self):
        groups = _chapter(*CHAPTER_CONTEXT, P068_RAW)
        repairs = recover_protected_term_boundaries(groups)
        self.assertEqual(groups[-1].text,
                         "TAKEAFEWHOURS FORTHENEAREST AWAKENED TO GET HERE.")
        self.assertEqual(len(repairs), 1)
        record = repairs[0]
        self.assertEqual(record["repair_reason"], "protected_term_boundary")
        self.assertEqual(record["original_text"], P068_RAW)
        self.assertEqual(record["protected_term"], "AWAKENED")
        self.assertEqual(record["joined_piece"], "TO")
        self.assertGreater(record["confidence"], 0.0)

    def test_raw_source_is_preserved(self):
        groups = _chapter(*CHAPTER_CONTEXT, P068_RAW)
        recover_protected_term_boundaries(groups)
        self.assertEqual(groups[-1].original_text, P068_RAW)
        self.assertNotEqual(groups[-1].text, groups[-1].original_text)
        self.assertIn("protected_term_boundary", groups[-1].repair_reason)

    def test_prefix_direction_is_also_recovered(self):
        groups = _chapter(
            "THE SPELL WAS BROKEN.",
            "A SPELL NOBODY COULD READ.",
            "AND THEN THE SPELLWAS GONE.",
        )
        recover_protected_term_boundaries(groups)
        self.assertEqual(groups[-1].text, "AND THEN THE SPELL WAS GONE.")

    def test_unknown_token_is_never_split(self):
        groups = _chapter(*CHAPTER_CONTEXT, "HE SAID FOOBARBAZ TO NOBODY.")
        self.assertEqual(recover_protected_term_boundaries(groups), [])
        self.assertEqual(groups[-1].text, "HE SAID FOOBARBAZ TO NOBODY.")

    def test_untrusted_remainder_stays_suspicious(self):
        groups = _chapter(*CHAPTER_CONTEXT, "THE AWAKENEDXYZ ARRIVED.")
        self.assertEqual(recover_protected_term_boundaries(groups), [])
        self.assertEqual(groups[-1].text, "THE AWAKENEDXYZ ARRIVED.")

    def test_single_observation_is_not_an_anchor(self):
        groups = _chapter("IN TIME, THE AWAKENED BECAME HOPE.", P068_RAW)
        self.assertEqual(recover_protected_term_boundaries(groups), [])

    def test_ledger_terms_may_anchor_without_chapter_repetition(self):
        groups = _chapter(P068_RAW)
        recover_protected_term_boundaries(groups, extra_anchors=["Awakened"])
        self.assertIn("AWAKENED TO", groups[-1].text)

    def test_ambiguous_decomposition_is_refused(self):
        # Two anchors, two different splits of the same token: no rewrite.
        groups = _chapter(
            "THE OUTBREAK BEGAN.", "ANOTHER OUTBREAK CAME.",
            "THE BREAKOUT FAILED.", "A SECOND BREAKOUT.",
            "THEY CALLED IT OUTBREAKOUT.",
        )
        before = groups[-1].text
        recover_protected_term_boundaries(groups)
        self.assertEqual(groups[-1].text, before)


# =============================================================================
# 3. the provider request must carry the canonical (repaired) source
# =============================================================================
class CanonicalTranslationSourceContract(unittest.TestCase):
    def test_repaired_text_is_what_the_provider_receives(self):
        groups = _chapter(*CHAPTER_CONTEXT, P068_RAW)
        recover_protected_term_boundaries(groups)
        request = [group.text for group in groups]
        self.assertIn("AWAKENED TO", request[-1])
        self.assertNotIn("AWAKENEDTO", request[-1])

    def test_repair_is_reported_as_provenance(self):
        groups = _chapter(*CHAPTER_CONTEXT, P068_RAW)
        recover_protected_term_boundaries(groups)
        records = benchmark_pipeline._group_text_repairs(groups)
        self.assertTrue(
            any(record["repair_reason"].endswith("protected_term_boundary")
                and record["original_text"] == P068_RAW
                for record in records)
        )


# =============================================================================
# 4. DeepL must have a retry at all
# =============================================================================
class _FakeDeepL:
    """One scripted DeepL response per request, with the wire body captured."""

    def __init__(self, *texts):
        self.texts = list(texts)
        self.bodies = []

    def __call__(self, url, headers, body, timeout):
        self.bodies.append(json.loads(body.decode("utf-8")))
        answer = self.texts.pop(0) if self.texts else ""
        rows = [{"text": answer, "billed_characters": len(answer)}
                for _ in self.bodies[-1]["text"]]
        return 200, json.dumps({"translations": rows})


def _deepl(*texts):
    transport = _FakeDeepL(*texts)
    translator = translator_deepl.DeepLTranslator(
        api_key="test-key",
        base_url="https://api.example.invalid",
        transport=transport,
    )
    translator.transport = transport
    return translator


class DeepLSemanticRetryContract(unittest.TestCase):
    def test_deepl_exposes_a_strict_retry(self):
        self.assertTrue(hasattr(_deepl(), "translate_strict"))

    def test_strict_retry_sends_the_source_it_was_given(self):
        translator = _deepl(P068_GOOD)
        source = "TAKEAFEWHOURS FORTHENEAREST AWAKENED TO GET HERE."
        self.assertEqual(
            translator.translate_strict(source, previous_translation=P068_BAD,
                                        validation_reason="fidelity_uncertain"),
            P068_GOOD,
        )
        self.assertEqual(translator.transport.bodies[-1]["text"], [source])

    def test_strict_retry_attaches_chapter_context(self):
        class _Store:
            def prompt_fragment(self):
                return "Awakened = classe de pessoas"

            def signature(self):
                return "sig"

        translator = _deepl(P068_GOOD)
        translator.set_session_context(_Store())
        self.assertTrue(translator.stats["context_enabled"])
        translator.translate_strict("X", validation_reason="fidelity_uncertain")
        self.assertIn("Awakened", translator.transport.bodies[-1]["context"])

    def test_first_pass_body_is_unchanged_without_context(self):
        translator = _deepl("OI")
        translator.translate_many(["HI"])
        self.assertNotIn("context", translator.transport.bodies[-1])

    def test_duplicate_candidate_is_counted_not_hidden(self):
        translator = _deepl(P068_BAD)
        translator.translate_strict("X", previous_translation=P068_BAD,
                                    validation_reason="fidelity_uncertain")
        self.assertEqual(translator.stats["strict_retry_duplicate_candidates"], 1)


# =============================================================================
# 5. bad-first / good-second, through the production retry path
# =============================================================================
class _ScriptedTranslator:
    is_configured = True

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def translate_strict(self, text, previous_translation="", validation_reason="",
                         force=False, allow_proper_names=True, proper_names=None,
                         **kwargs):
        self.calls.append({"source": text, "reason": validation_reason,
                           "previous": previous_translation})
        return self.responses.pop(0) if self.responses else ""


class _NoRetryTranslator:
    """A provider without ``translate_strict`` - what DeepL used to be."""

    is_configured = True


class SemanticRecoveryReplay(unittest.TestCase):
    def _run(self, translator, source, candidate):
        group = _group(source, candidate, group_id="BALAO_2")
        stats = {}
        validate_and_retry_translations(
            [group], translator, fidelity_stats=stats, fidelity_verifier=None,
        )
        return group, stats

    def test_bad_first_good_second_closes_the_region(self):
        translator = _ScriptedTranslator(P068_GOOD)
        source = "TAKEAFEWHOURS FORTHENEAREST AWAKENED TO GET HERE."
        group, _ = self._run(translator, source, P068_BAD)
        self.assertEqual(len(translator.calls), 1)
        # The retry is informed: it names the constraint that failed and carries
        # the canonical source, not the raw OCR join.
        self.assertTrue(translator.calls[0]["reason"])
        self.assertEqual(translator.calls[0]["source"], source)
        self.assertNotIn("AWAKENEDTO", translator.calls[0]["source"])
        self.assertEqual(group.translation, P068_GOOD)
        self.assertEqual(group.translation_final_state, "translated")
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.semantic_review_reason, "")

    def test_all_bad_candidates_stay_held(self):
        translator = _ScriptedTranslator(P068_BAD)
        group, _ = self._run(
            translator, "TAKEAFEWHOURS FORTHENEAREST AWAKENED TO GET HERE.", P068_BAD)
        self.assertNotEqual(group.translation_final_state, "translated")
        self.assertNotEqual(group.translation, P068_BAD)

    def test_provider_without_strict_retry_gets_no_recovery(self):
        """The #84F3 shape, kept as the control this mission had to remove."""
        group, _ = self._run(
            _NoRetryTranslator(),
            "TAKEAFEWHOURS FORTHENEAREST AWAKENED TO GET HERE.",
            P068_BAD,
        )
        self.assertNotEqual(group.translation_final_state, "translated")


if __name__ == "__main__":
    unittest.main()
