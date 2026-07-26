"""Human overrides, isolated drafts and the preview gates (BLOCO 6C).

Two synthetic chapters, generated per run, prove the machinery is generic: no
test here reuses a phrase, page, region or id from the chapter under revision.
The offline guard makes any real network call fail loudly.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import random
import string
import tempfile
import unittest
import uuid
from pathlib import Path

import cv2
import numpy as np

import human_translation_decisions as htd
import linguistic_triage as lt
import preview_gates as pg
import provider_execution as pe


def _rid(n=8):
    return uuid.uuid4().hex[:n].upper()


def _line(words=5):
    stock = ["A", "PORTA", "ESTAVA", "ABERTA", "QUANDO", "ELE", "CHEGOU", "TARDE"]
    return " ".join(random.choice(stock) for _ in range(words)) + "."


def _page(width=400, height=300, *, dark=False):
    """A synthetic page: flat background plus a band of 'art' to protect."""
    level = 30 if dark else 240
    page = np.full((height, width, 3), level, dtype=np.uint8)
    cv2.rectangle(page, (0, height - 40), (width, height), (120, 90, 60), -1)
    return page


def _draw_text(page, box, text, *, dark_text=True):
    x, y, w, h = box
    colour = (20, 20, 20) if dark_text else (245, 245, 245)
    cv2.putText(page, text, (x + 6, y + h - 10), cv2.FONT_HERSHEY_SIMPLEX,
                0.6, colour, 2, cv2.LINE_AA)
    return page


class HumanDecisionStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = htd.HumanTranslationDecisionStore(Path(self.dir.name) / "d.sqlite3")
        self.ids = {"job_id": _rid(), "run_id": _rid(), "revision_id": _rid()}
        self.owner = _rid()

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _record(self, **kw):
        payload = {
            "provider_execution_id": kw.pop("execution", "exec-1"),
            "authorization_request_id": "exec-1",
            **self.ids,
            "page_id": f"p{random.randint(1, 99):03d}",
            "region_id": kw.pop("region_id", f"R{_rid()}"),
            "source_text": kw.pop("source", _line()),
            "provider_candidate": kw.pop("provider", _line()),
            "human_candidate": kw.pop("human", _line()),
            "created_by": kw.pop("owner", self.owner),
        }
        payload.update(kw)
        return self.store.upsert(**payload)

    def test_a_decision_is_recorded_with_its_lineage(self):
        record = self._record(reason="motivo")
        for key in ("provider_execution_id", "authorization_request_id", "job_id", "run_id",
                    "revision_id", "page_id", "region_id", "source_text", "source_text_hash",
                    "provider_candidate", "human_candidate", "decision", "reason",
                    "created_by", "created_at", "updated_at", "status"):
            self.assertIn(key, record, key)
        self.assertEqual(record["decision"], "replace_provider_candidate")
        self.assertEqual(record["status"], "approved_for_preview")

    def test_recording_twice_updates_instead_of_duplicating(self):
        first = self._record(region_id="R1", human="uma linha")
        second = self._record(region_id="R1", human="outra linha")
        self.assertEqual(first["human_translation_decision_id"],
                         second["human_translation_decision_id"])
        self.assertEqual(second["human_candidate"], "outra linha")
        self.assertEqual(len(self.store.list_for(**self.ids, created_by=self.owner)), 1)

    def test_a_decision_can_be_changed_and_removed(self):
        record = self._record()
        changed = self.store.set_status(record["human_translation_decision_id"],
                                        "visually_rejected", created_by=self.owner)
        self.assertEqual(changed["status"], "visually_rejected")
        self.assertTrue(self.store.delete(record["human_translation_decision_id"],
                                          created_by=self.owner))
        self.assertIsNone(self.store.get(record["human_translation_decision_id"]))

    def test_decisions_are_owner_scoped(self):
        record = self._record()
        other = _rid()
        self.assertEqual(self.store.list_for(**self.ids, created_by=other), [])
        with self.assertRaisesRegex(ValueError, "not_decision_owner"):
            self.store.delete(record["human_translation_decision_id"], created_by=other)

    def test_authentication_and_content_are_required(self):
        with self.assertRaisesRegex(ValueError, "authentication_required"):
            self._record(owner="")
        with self.assertRaisesRegex(ValueError, "empty_human_candidate"):
            self._record(human="   ")


class DecisionAgainstExecution(unittest.TestCase):
    """A decision must still answer the execution it was written for."""

    def setUp(self):
        self.region = f"R{_rid()}"
        self.source = _line()
        self.provider = _line()
        self.execution = {
            "authorization_request_id": "exec-1",
            "results": [{"region_id": self.region, "text": self.source,
                         "translation": self.provider}],
        }
        self.decision = {
            "provider_execution_id": "exec-1", "region_id": self.region,
            "source_text_hash": htd.source_text_hash(self.source),
            "provider_candidate": self.provider,
        }

    def test_a_matching_decision_validates(self):
        htd.validate_against_execution(self.decision, self.execution)

    def test_a_different_execution_is_refused(self):
        with self.assertRaisesRegex(ValueError, "provider_execution_mismatch"):
            htd.validate_against_execution({**self.decision, "provider_execution_id": "exec-2"},
                                           self.execution)

    def test_a_region_outside_the_execution_is_refused(self):
        with self.assertRaisesRegex(ValueError, "region_not_in_provider_execution"):
            htd.validate_against_execution({**self.decision, "region_id": f"R{_rid()}"},
                                           self.execution)

    def test_changed_source_text_is_refused(self):
        with self.assertRaisesRegex(ValueError, "source_text_hash_mismatch"):
            htd.validate_against_execution(
                {**self.decision, "source_text_hash": htd.source_text_hash(_line())},
                self.execution)

    def test_changed_provider_answer_is_refused(self):
        with self.assertRaisesRegex(ValueError, "provider_candidate_mismatch"):
            htd.validate_against_execution({**self.decision, "provider_candidate": _line()},
                                           self.execution)

    def test_a_missing_execution_is_refused(self):
        with self.assertRaisesRegex(ValueError, "provider_execution_not_found"):
            htd.validate_against_execution(self.decision, {})


class NoProviderGuard(unittest.TestCase):
    def test_the_guard_refuses_every_provider_entry_point(self):
        reviewer = pe.NoProviderReviewer()
        for call in (lambda: reviewer.review_batch([], {}),
                     lambda: reviewer.translate_many(["x"]),
                     lambda: reviewer.translate("x"),
                     lambda: reviewer.health_check()):
            with self.assertRaises(pe.ProviderCallNotAuthorized):
                call()

    def test_the_guard_looks_like_a_reviewer_without_being_one(self):
        reviewer = pe.NoProviderReviewer()
        self.assertEqual(reviewer.requests, 0)
        self.assertTrue(reviewer.model)


class PreviewGates(unittest.TestCase):
    """Synthetic chapter A: one region per page, light and dark backgrounds."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def _pair(self, *, box=(40, 40, 300, 60), dark=False, new_text="TEXTO NOVO",
              draw_outside=False, clip=False):
        base = _page(dark=dark)
        _draw_text(base, box, "TEXTO ANTIGO", dark_text=not dark)
        draft = base.copy()
        x, y, w, h = box
        draft[y:y + h, x:x + w] = 30 if dark else 240
        if clip:
            # Text that runs to the very edge of its own box.
            cv2.putText(draft, new_text, (x, y + h - 2), cv2.FONT_HERSHEY_SIMPLEX,
                        1.4, (245, 245, 245) if dark else (20, 20, 20), 3, cv2.LINE_AA)
        else:
            _draw_text(draft, box, new_text, dark_text=not dark)
        if draw_outside:
            cv2.circle(draft, (10, 10), 4, (0, 0, 255), -1)
        bp, dp = self.out / "base.png", self.out / "draft.png"
        cv2.imwrite(str(bp), base)
        cv2.imwrite(str(dp), draft)
        return bp, dp, box

    def test_a_clean_draft_passes(self):
        bp, dp, box = self._pair()
        gate = pg.evaluate_visual_gate(bp, dp, boxes=[box])
        self.assertEqual(gate["status"], pg.PASSED, gate["reason_codes"])
        self.assertEqual(gate["isolation"]["changed_pixels_outside_mask"], 0)
        self.assertGreater(gate["isolation"]["changed_pixels_inside_mask"], 0)

    def test_a_dark_background_draft_passes(self):
        bp, dp, box = self._pair(dark=True)
        gate = pg.evaluate_visual_gate(bp, dp, boxes=[box])
        self.assertEqual(gate["isolation"]["changed_pixels_outside_mask"], 0)
        self.assertNotEqual(gate["status"], pg.FAILED, gate["reason_codes"])

    def test_a_pixel_changed_outside_the_mask_fails_closed(self):
        bp, dp, box = self._pair(draw_outside=True)
        gate = pg.evaluate_visual_gate(bp, dp, boxes=[box])
        self.assertEqual(gate["status"], pg.FAILED)
        self.assertIn("pixels_changed_outside_mask", gate["reason_codes"])
        self.assertGreater(gate["isolation"]["changed_pixels_outside_mask"], 0)
        self.assertTrue(gate["isolation"]["outside_bounds"])

    def test_text_that_does_not_fit_fails(self):
        bp, dp, box = self._pair(clip=True, new_text="UMA LINHA MUITO MAIS LONGA DO QUE CABE")
        gate = pg.evaluate_visual_gate(bp, dp, boxes=[box])
        self.assertEqual(gate["status"], pg.FAILED)
        self.assertIn("text_fit_failed", gate["reason_codes"])

    def test_a_draft_that_drew_nothing_fails(self):
        base = _page()
        box = (40, 40, 300, 60)
        _draw_text(base, box, "TEXTO ANTIGO")
        draft = base.copy()
        x, y, w, h = box
        draft[y:y + h, x:x + w] = 240
        bp, dp = self.out / "b.png", self.out / "d.png"
        cv2.imwrite(str(bp), base); cv2.imwrite(str(dp), draft)
        gate = pg.evaluate_visual_gate(bp, dp, boxes=[box])
        self.assertEqual(gate["status"], pg.FAILED)
        self.assertIn("no_text_drawn", gate["reason_codes"])

    def test_ink_the_erase_box_could_not_reach_is_reported(self):
        """A box tighter than the glyphs leaves a fragment of the old line."""
        base = _page()
        box = (40, 40, 200, 60)
        # The old text runs past the right edge of the box it will be erased in.
        _draw_text(base, (40, 40, 320, 60), "TEXTO ANTIGO BEM MAIS LARGO")
        draft = base.copy()
        x, y, w, h = box
        draft[y:y + h, x:x + w] = 240
        _draw_text(draft, box, "NOVO")
        bp, dp = self.out / "b.png", self.out / "d.png"
        cv2.imwrite(str(bp), base); cv2.imwrite(str(dp), draft)
        gate = pg.evaluate_visual_gate(bp, dp, boxes=[box])
        self.assertIn("residual_ink_outside_mask", gate["reason_codes"])
        self.assertEqual(gate["status"], pg.NEEDS_REVIEW)
        self.assertGreater(gate["residual_ink"][0]["residual_ink_pixels"], 0)

    def test_mismatched_dimensions_fail(self):
        bp, dp, box = self._pair()
        smaller = cv2.imread(str(dp))[:100, :100]
        cv2.imwrite(str(dp), smaller)
        gate = pg.evaluate_visual_gate(bp, dp, boxes=[box])
        self.assertEqual(gate["status"], pg.FAILED)
        self.assertIn("image_dimension_mismatch", gate["reason_codes"])

    def test_a_missing_draft_is_a_verdict_not_a_pass(self):
        bp, _, box = self._pair()
        gate = pg.evaluate_visual_gate(bp, self.out / "nao_existe.png", boxes=[box])
        self.assertEqual(gate["status"], pg.FAILED)
        self.assertIn("draft_image_unavailable", gate["reason_codes"])

    def test_previews_are_written_for_every_view(self):
        bp, dp, box = self._pair()
        files = pg.write_previews(bp, dp, box=box, out_dir=self.out / "ev", stem="pg")
        for suffix in ("_preview", "_base", "_region_before", "_region_after",
                       "_diff", "_overlay", "_side_by_side"):
            self.assertTrue(any(suffix in name for name in files), suffix)
        for path in files.values():
            self.assertTrue(Path(path).is_file())
            self.assertIsNotNone(cv2.imread(path))

    def test_two_regions_on_one_page_are_both_allowed(self):
        """Synthetic chapter B: more than one region in the same page."""
        base = _page(width=500)
        boxes = [(20, 30, 200, 50), (260, 150, 200, 50)]
        for box in boxes:
            _draw_text(base, box, "ANTIGO")
        draft = base.copy()
        for box in boxes:
            x, y, w, h = box
            draft[y:y + h, x:x + w] = 240
            _draw_text(draft, box, "NOVO")
        bp, dp = self.out / "b.png", self.out / "d.png"
        cv2.imwrite(str(bp), base); cv2.imwrite(str(dp), draft)
        gate = pg.evaluate_visual_gate(bp, dp, boxes=boxes)
        self.assertEqual(gate["isolation"]["changed_pixels_outside_mask"], 0)
        self.assertEqual(len(gate["text_fit"]), 2)


class LinguisticGateOnHumanLine(unittest.TestCase):
    def test_the_human_line_is_gated_independently_of_the_provider(self):
        import region_taxonomy as tax
        policy = {"normalized_classification": tax.DIALOGUE_TRANSLATE, "translatable": True,
                  "preservable": False, "provider_required": False, "needs_human_review": False}
        source = "THE DOOR WAS OPEN WHEN HE ARRIVED."
        human = lt.evaluate_linguistic_gate(source_text=source,
                                            current_translation="A porta estava aberta.",
                                            policy=policy)
        untranslated = lt.evaluate_linguistic_gate(source_text=source,
                                                   current_translation=source, policy=policy)
        self.assertEqual(human["status"], lt.PASSED, human["reason_codes"])
        self.assertEqual(untranslated["status"], lt.FAILED)
        # The two gates answer different questions and must not be conflated.
        self.assertNotEqual(pg.GATE_VERSION, "")

    def test_a_short_line_without_accents_is_not_penalised(self):
        import region_taxonomy as tax
        policy = {"normalized_classification": tax.DIALOGUE_TRANSLATE, "translatable": True,
                  "preservable": False, "provider_required": False, "needs_human_review": False}
        gate = lt.evaluate_linguistic_gate(source_text="IT BETTER BE GOOD.",
                                           current_translation="PRECISA SER BOM.", policy=policy)
        self.assertNotIn("no_target_language_orthography", gate["reason_codes"])


class NoHardcodedTranslations(unittest.TestCase):
    """Production must carry none of the phrases this revision decided on."""

    PRODUCTION = ("human_translation_decisions.py", "preview_gates.py",
                  "provider_execution.py", "chapter_quality_revision.py",
                  "ui_bridge.py", "app_ui.py", "linguistic_triage.py",
                  "region_taxonomy.py", "static/tradutor_ui.js")

    def test_no_decided_phrase_appears_in_production(self):
        root = Path(__file__).resolve().parent
        forbidden = ("REAL COFFEE", "VERDADEIRO CAF", "CAFÉ DE VERDADE",
                     "SINCE I'VE SPENT", "Já que gastei", "IT BETTER BE WORTH",
                     "É MELHOR VALER", "shadow_slave", "reviewed_v8", "reviewed_v9")
        for rel in self.PRODUCTION:
            text = (root / rel).read_text(encoding="utf-8")
            for needle in forbidden:
                self.assertNotIn(needle, text, f"{needle!r} found in {rel}")

    def test_no_production_module_branches_on_an_identity(self):
        import re
        root = Path(__file__).resolve().parent
        for rel in self.PRODUCTION:
            if not rel.endswith(".py"):
                continue
            text = (root / rel).read_text(encoding="utf-8")
            for pattern in (r"page_number\s*==\s*\d", r"page_id\s*==\s*['\"]p\d",
                            r"region_id\s*==\s*['\"]p\d", r"job_id\s*==\s*['\"][0-9a-f]{8}"):
                self.assertIsNone(re.search(pattern, text), f"{pattern} in {rel}")


if __name__ == "__main__":
    unittest.main()
