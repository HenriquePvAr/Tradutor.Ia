"""TDD #84F8 - RC blockers: reader auth, terminology-review render, RapidOCR UX.

Everything here is offline and replays only data this repository already holds: the
persisted run of job d6a611773212497f9040341f9af65373 / run
5758024e-7ab4-43d3-a7ec-04d773c7154a, plus synthetic groups. No provider, no network,
no Drive, no Supabase, no new translation job.
"""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()


import re
import unittest
from pathlib import Path

import source_completeness
from ocr_balloon import (
    DO_NOT_RENDER,
    RENDER_WITH_REVIEW,
    TextGroup,
    _finalize_translation_failure,
    _should_translate_group,
    render_disposition,
    terminology_review_render_candidate,
    translation_render_state,
)

ROOT = Path(__file__).resolve().parent
READER_JS = ROOT / "static" / "chapter_reader.js"
SHELL_HTML = ROOT / "ui" / "ui_shell.html"

# The four ordinary-story English residuals the real #84F8 run left visible.
STORY_RESIDUALS = ("p040:BALAO_1", "p040:BALAO_2", "p054:BALAO_1", "p057:BALAO_1")

# Every physical source residual of job d6a6117732124973 / run 5758024e, copied
# verbatim from that run's progress.json. Inlined rather than read from output/:
# the suite is hermetic, and a frozen fixture also pins the exact failure the fix
# has to answer instead of following a directory that may be cleaned.
PERSISTED = {
    "p011:BALAO_1": {
        "clean_text": "TAK", "translation_candidate": "TAK",
        "translation_final_reason": "untranslated_source_after_retries",
        "translation_validation_reason": "candidate_equals_source",
        "classification": "speech"},
    "p011:BALAO_2": {
        "clean_text": "TUR", "translation_candidate": "TUR",
        "translation_final_reason": "untranslated_source_after_retries",
        "translation_validation_reason": "candidate_equals_source",
        "classification": "speech"},
    "p013:BALAO_3": {
        "clean_text": "TRNDGE", "translation_candidate": "",
        "translation_final_reason": "missing_translation_candidate",
        "translation_validation_reason": "", "classification": "speech"},
    "p040:BALAO_1": {
        "clean_text": "SO THEIR CHILDREN COLLD RECEIVE SPECIAL TRAINING FOR "
                      "BECOMING AWAKENED.",
        "translation_candidate": "ASSIM, SEUS FILHOS PODERIAM RECEBER UM TREINAMENTO "
                                 "ESPECIAL PARA SE TORNAREM DESPERTOS.",
        "translation_final_reason": "terminology_conflict_after_retries",
        "translation_validation_reason": "terminology_conflict:AWAKENED",
        "classification": "narration"},
    "p040:BALAO_2": {
        "clean_text": "FLRTHERMORE, CHILDREN BORN INTO POWERFLL AWAKENED FAMILIES..",
        "translation_candidate": "ALÉM DISSO, CRIANÇAS NASCIDAS EM FAMÍLIAS "
                                 "PODEROSAS E DESPERTAS...",
        "translation_final_reason": "terminology_conflict_after_retries",
        "translation_validation_reason": "terminology_conflict:AWAKENED",
        "classification": "narration"},
    "p054:BALAO_1": {
        "clean_text": "... COMPLETE THE FIRST NIGHTMARE...",
        "translation_candidate": "... CONCLUA O PRIMEIRO PESADELO...",
        "translation_final_reason": "terminology_conflict_after_retries",
        "translation_validation_reason": "terminology_conflict:FIRST",
        "classification": "narration"},
    "p057:BALAO_1": {
        "clean_text": "INSIDE YOUR FIRST NIGHTMARE, YOu'LL RUN INTO MONSTERS, SURE, "
                      "BUT YOU WILL ALSO MEETPEOPLE.",
        "translation_candidate": "DENTRO DO SEU PRIMEIRO PESADELO, VOCÊ ENCONTRARÁ "
                                 "MONSTROS, É CLARO, MAS TAMBÉM CONHECERÁ PESSOAS.",
        "translation_final_reason": "terminology_conflict_after_retries",
        "translation_validation_reason": "terminology_conflict:FIRST",
        "classification": "narration"},
}


def _group(text, candidate, *, reason, classification="narration",
           validation_reason="", semantic_review=""):
    """A group already finalised by the translation stage as a failure."""

    group = TextGroup(group_id="BALAO_1", lines=[], text=text,
                      classification=classification)
    group.sent_to_translation = True
    group.semantic_review_reason = semantic_review
    group.source_completeness = {"status": source_completeness.STATUS_PASS}
    _finalize_translation_failure(
        group, reason, candidate=candidate, rejected_candidate=candidate,
        validator_reason=validation_reason or reason,
    )
    return group


def _replay(region):
    item = PERSISTED[region]
    return _group(item["clean_text"], item["translation_candidate"],
                  reason=item["translation_final_reason"],
                  classification=item["classification"],
                  validation_reason=item["translation_validation_reason"])


# ----------------------------------------------------- terminology review ----

class TerminologyReviewRenderTests(unittest.TestCase):
    """A term the ledger is unsure about must not republish the English source."""

    def test_terminology_conflict_keeps_a_usable_ptbr_candidate_renderable(self):
        group = _group(
            "SO THEIR CHILDREN COULD RECEIVE SPECIAL TRAINING FOR BECOMING AWAKENED.",
            "ASSIM, SEUS FILHOS PODERIAM RECEBER UM TREINAMENTO ESPECIAL PARA SE "
            "TORNAREM DESPERTOS.",
            reason="terminology_conflict_after_retries",
            validation_reason="terminology_conflict:AWAKENED",
        )
        self.assertTrue(terminology_review_render_candidate(group))
        self.assertTrue(group.translation, "the candidate must reach the renderer")
        self.assertTrue(_should_translate_group(group))
        self.assertTrue(group.manual_review_required)
        self.assertEqual(group.translation_quality_impact, "review_required")
        self.assertEqual(group.translation_final_reason,
                         "terminology_conflict_after_retries")

    def test_semantic_fidelity_failure_still_withholds_the_candidate(self):
        # P068's protection: a proven meaning corruption is never published just to
        # remove English from the page.
        group = _group(
            "HE WAS NEVER GOING TO AWAKEN.",
            "ELE SEMPRE IRIA DESPERTAR.",
            reason="semantic_fidelity_failed_after_retries",
            validation_reason="semantic_inversion",
        )
        self.assertEqual(terminology_review_render_candidate(group), "")
        self.assertEqual(group.translation, "")
        self.assertFalse(_should_translate_group(group))

    def test_residual_english_candidate_is_not_usable_ptbr(self):
        group = _group(
            "THIS PLACE IS HELL!!",
            "ESTE LUGAR E HELL!!",
            reason="terminology_conflict_after_retries",
            validation_reason="terminology_conflict:HELL",
        )
        self.assertEqual(terminology_review_render_candidate(group), "")
        self.assertFalse(_should_translate_group(group))

    def test_a_candidate_equal_to_the_source_is_not_usable_ptbr(self):
        group = _group("TAK", "TAK", reason="untranslated_source_after_retries",
                       classification="speech",
                       validation_reason="candidate_equals_source")
        self.assertEqual(terminology_review_render_candidate(group), "")
        self.assertFalse(_should_translate_group(group))

    def test_a_group_without_any_candidate_stays_withheld(self):
        group = _group("TRNDGE", "", reason="missing_translation_candidate",
                       classification="speech")
        self.assertEqual(terminology_review_render_candidate(group), "")
        self.assertFalse(_should_translate_group(group))


class RenderDispositionTests(unittest.TestCase):
    def test_manual_review_terminal_state_renders_with_review_not_clean(self):
        group = _group(
            "... COMPLETE THE FIRST NIGHTMARE...",
            "... CONCLUA O PRIMEIRO PESADELO...",
            reason="terminology_conflict_after_retries",
            validation_reason="terminology_conflict:FIRST",
        )
        state, reason = translation_render_state(group)
        self.assertEqual(state, "review")
        self.assertEqual(reason, "terminology_conflict_after_retries")
        disposition, disposition_reason = render_disposition(
            translation=state, translation_reason=reason,
            source_removed=True, art="clean", art_reason="")
        self.assertEqual(disposition, RENDER_WITH_REVIEW)
        self.assertIn("terminology_conflict", disposition_reason)

    def test_a_rejected_translation_still_never_renders(self):
        group = _group("HE WAS NEVER GOING TO AWAKEN.", "ELE SEMPRE IRIA DESPERTAR.",
                       reason="semantic_fidelity_failed_after_retries",
                       validation_reason="semantic_inversion")
        group.translation_final_state = "rejected"
        state, reason = translation_render_state(group)
        self.assertEqual(state, "reject")
        self.assertEqual(
            render_disposition(translation=state, translation_reason=reason,
                               source_removed=True, art="clean")[0],
            DO_NOT_RENDER)

    def test_a_clean_translation_is_still_clean(self):
        group = TextGroup(group_id="BALAO_1", lines=[], text="HELLO.",
                          classification="narration")
        group.translation_final_state = "translated"
        group.translation_final_reason = "ok"
        self.assertEqual(translation_render_state(group), ("clean", ""))


# ------------------------------------------------------- persisted replay ----

class PersistedResidualReplayTests(unittest.TestCase):
    """Replay the four real regions from the run that shipped them in English."""

    def test_every_ordinary_story_residual_now_renders_ptbr_under_review(self):
        for region in STORY_RESIDUALS:
            with self.subTest(region=region):
                group = _replay(region)
                rendered = terminology_review_render_candidate(group)
                self.assertTrue(rendered, "a usable PT-BR candidate must ship")
                self.assertEqual(group.translation, rendered)
                self.assertTrue(_should_translate_group(group))
                # The English source must not survive in what is drawn.
                source = PERSISTED[region]["clean_text"]
                self.assertNotEqual(
                    re.sub(r"[^A-Za-z]", "", rendered).casefold(),
                    re.sub(r"[^A-Za-z]", "", source).casefold())
                self.assertTrue(group.manual_review_required)
                self.assertEqual(group.translation_quality_impact, "review_required")
                state, reason = translation_render_state(group)
                self.assertEqual(state, "review")
                self.assertEqual(reason, "terminology_conflict_after_retries")

    def test_the_other_three_residuals_keep_their_own_verdicts(self):
        # Accounting: 7 physical residuals = 4 ordinary story + 2 source-language
        # residuals + 1 with no candidate at all. Only the four above may render.
        self.assertEqual(len(PERSISTED), 7)
        self.assertEqual(
            sorted(set(PERSISTED) - set(STORY_RESIDUALS)),
            ["p011:BALAO_1", "p011:BALAO_2", "p013:BALAO_3"])
        for region in ("p011:BALAO_1", "p011:BALAO_2", "p013:BALAO_3"):
            with self.subTest(region=region):
                group = _replay(region)
                self.assertEqual(terminology_review_render_candidate(group), "")
                self.assertEqual(group.translation, "")
                self.assertFalse(_should_translate_group(group))

    def test_the_missing_candidate_owner_is_exactly_one_region(self):
        owners = [region for region, item in PERSISTED.items()
                  if not item["translation_candidate"]]
        self.assertEqual(owners, ["p013:BALAO_3"])


# -------------------------------------------------------------- reader UI ----

class ReaderAuthorizationTests(unittest.TestCase):
    """The reader must authenticate the way the rest of the app does.

    Under the Beta's Supabase provider the session is a Bearer token in a header, so a
    plain `fetch` and an `<img src>` are both anonymous: every reader request came back
    401 and the UI showed the generic "Não foi possível abrir este PDF.".
    """

    @classmethod
    def setUpClass(cls):
        cls.source = READER_JS.read_text(encoding="utf-8")

    def test_the_metadata_request_carries_the_session_bearer(self):
        self.assertIn("Authorization", self.source)
        self.assertIn("__tradutorGetCanonicalAccessToken", self.source)

    def test_pages_and_thumbnails_are_fetched_not_hotlinked(self):
        # `<img src="/api/...">` cannot carry a header, so the bytes have to be
        # fetched and handed to the element as an object URL.
        self.assertIn("createObjectURL", self.source)
        self.assertIn("revokeObjectURL", self.source)
        for pattern in (r"img\.src\s*=\s*img\.dataset\.src",
                        r"setAttribute\('src',\s*source\)"):
            self.assertNotRegex(self.source, pattern)

    def test_no_token_is_ever_placed_in_a_url(self):
        self.assertNotRegex(self.source, r"[?&](token|access_token|jwt)=")

    def test_an_expired_session_is_reported_truthfully(self):
        self.assertIn("authentication_required", self.source)

    def test_the_reader_still_names_only_the_opaque_job_id(self):
        for path in re.findall(r"`/api/ui/reader/[^`]*`", self.source):
            self.assertIn("encodeURIComponent(", path)


class RapidOcrBetaSurfaceTests(unittest.TestCase):
    """The Beta runs RapidOCR; the user must be able to see that, and only that."""

    @classmethod
    def setUpClass(cls):
        cls.shell = SHELL_HTML.read_text(encoding="utf-8")

    def test_the_ocr_engine_is_shown_next_to_the_translation_engine(self):
        self.assertIn("Motor de OCR", self.shell)
        self.assertIn("RapidOCR", self.shell)

    def test_no_paddle_engine_is_offered_as_a_choice(self):
        self.assertNotIn("Paddle", self.shell)

    def test_the_ocr_engine_is_informational_and_not_selectable(self):
        field = re.search(r"Motor de OCR.*?</div>", self.shell, re.S)
        self.assertIsNotNone(field)
        self.assertIn("disabled", field.group(0))


if __name__ == "__main__":
    unittest.main()
