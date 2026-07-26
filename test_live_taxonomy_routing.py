"""Live taxonomy routing contracts (BLOCO 4).

Proves the canonical policy drives the live targeted review, the audit and the
forgotten-text search identically, on two structurally unrelated synthetic
chapters with randomised ids — nothing chapter-specific in production logic.
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

import region_taxonomy as tax
from chapter_quality_revision import ChapterQualityRevision


def _rand_word(n=7):
    return "".join(random.choice(string.ascii_uppercase) for _ in range(n))


def _consonant_garbage(n=5):
    """Unreadable token: no vowels and never 3 identical chars in a row (which
    the policy would legitimately read as an elongated onomatopoeia)."""
    pool = "QXZKVW"
    out = []
    for _ in range(n):
        choices = [c for c in pool if not (len(out) >= 2 and out[-1] == out[-2] == c)]
        out.append(random.choice(choices))
    return "".join(out)


def _rand_sentence(words=6):
    # Real English-ish words so the policy sees semantic content, but the exact
    # phrase is generated per run — never a fixture copied into production.
    stock = ["THE", "MORNING", "LIGHT", "FELL", "OVER", "QUIET", "STREETS", "AGAIN",
             "SHE", "WALKED", "SLOWLY", "TOWARD", "DISTANT", "GARDEN", "GATES"]
    return " ".join(random.choice(stock) for _ in range(words)) + "."


class PolicyRoutingContracts(unittest.TestCase):
    def setUp(self):
        self.revision = ChapterQualityRevision("unused", job_id="j", run_id="r")

    def _policy(self, classification, text, **kw):
        return self.revision.region_policy(
            {"classification": classification, "source_text": text,
             "confidence": kw.pop("confidence", 0.9), **kw})

    # --- translatable / reviewable ---------------------------------------
    def test_legacy_decorative_with_meaning_becomes_semantic_and_reviewable(self):
        policy = self._policy("decorative", _rand_sentence())
        self.assertEqual(policy["normalized_classification"], tax.DECORATIVE_SEMANTIC_TRANSLATE)
        self.assertTrue(policy["reviewable"])
        self.assertTrue(policy["translatable"])
        self.assertFalse(policy["preservable"])

    def test_styled_title_and_out_of_balloon_narration_are_reviewable(self):
        for legacy, expected in (("title", tax.TITLE_SEMANTIC_TRANSLATE),
                                 ("narration", tax.NARRATION_TRANSLATE)):
            policy = self._policy(legacy, _rand_sentence())
            self.assertEqual(policy["normalized_classification"], expected)
            self.assertTrue(policy["reviewable"], legacy)

    def test_every_translatable_category_is_reviewable(self):
        for legacy in ("speech", "dialogue", "thought", "narration", "system_message",
                       "location", "editorial"):
            policy = self._policy(legacy, _rand_sentence())
            self.assertTrue(policy["translatable"], legacy)
            self.assertTrue(policy["reviewable"], legacy)

    # --- preservable ------------------------------------------------------
    def test_real_sfx_in_a_decorative_bucket_stays_preserved(self):
        for word in ("BAM", "WHOOSH", "THUD", "CLANG"):
            policy = self._policy("decorative", word)
            self.assertEqual(policy["normalized_classification"], tax.SFX_PRESERVE, word)
            self.assertFalse(policy["reviewable"], word)
            self.assertTrue(policy["preservable"], word)

    def test_watermark_url_and_credit_stay_preserved_whatever_the_bucket(self):
        host = f"{_rand_word(6).lower()}.com"
        cases = [("decorative", f"www.{host}", tax.URL_PRESERVE),
                 ("speech", f"https://{host}/x", tax.URL_PRESERVE),
                 ("narration", f"Translated by {_rand_word()} team", tax.CREDIT_PRESERVE)]
        for legacy, text, expected in cases:
            policy = self._policy(legacy, text)
            self.assertEqual(policy["normalized_classification"], expected, text)
            self.assertFalse(policy["translatable"], text)
            self.assertFalse(policy["provider_required"], text)

    def test_proven_proper_name_is_preserved(self):
        policy = self._policy("decorative", _rand_word(), raw_item={"preserve_as_name": True})
        self.assertEqual(policy["normalized_classification"], tax.PROPER_NAME_PRESERVE)
        self.assertTrue(policy["preservable"])

    # --- uncertain / unreadable ------------------------------------------
    def test_ambiguous_short_token_fails_closed(self):
        # Consonant-only clusters carry no readable word, whatever the bucket.
        garbage = _consonant_garbage(4)
        policy = self._policy("decorative", garbage)
        self.assertIn(policy["normalized_classification"],
                      (tax.UNKNOWN_REVIEW_REQUIRED, tax.OCR_INVALID))
        self.assertFalse(policy["translatable"])
        self.assertFalse(policy["provider_required"])
        self.assertTrue(policy["needs_human_review"])

    def test_unreadable_low_confidence_text_is_ocr_invalid_and_never_translated(self):
        policy = self._policy("unknown", _consonant_garbage(5), confidence=0.2)
        self.assertEqual(policy["normalized_classification"], tax.OCR_INVALID)
        self.assertFalse(policy["translatable"])
        self.assertTrue(policy["ocr_retry_allowed"])
        self.assertEqual(policy["suggested_action"], "targeted_ocr")

    def test_unrecognised_legacy_bucket_fails_closed(self):
        policy = self._policy(_rand_word(9).lower(), _rand_sentence())
        self.assertEqual(policy["normalized_classification"], tax.UNKNOWN_REVIEW_REQUIRED)
        self.assertFalse(policy["translatable"])

    # --- human decisions --------------------------------------------------
    def test_human_decision_changes_policy_and_removal_restores_it(self):
        sfx = ("decorative", "BAM")
        before = self._policy(*sfx)
        self.assertFalse(before["reviewable"])
        decided = self.revision.region_policy(
            {"classification": sfx[0], "source_text": sfx[1], "confidence": 0.9},
            user_decision="translate")
        self.assertTrue(decided["reviewable"])
        self.assertTrue(decided["translatable"])
        # Removing the decision (empty) restores the inferred policy.
        after = self._policy(*sfx)
        self.assertEqual(after["normalized_classification"], before["normalized_classification"])
        self.assertFalse(after["reviewable"])

    def test_dismissed_decision_removes_it_from_review(self):
        policy = self.revision.region_policy(
            {"classification": "speech", "source_text": _rand_sentence(), "confidence": 0.9},
            user_decision="dismissed")
        self.assertFalse(policy["reviewable"])

    def test_ocr_invalid_decision_blocks_translation(self):
        policy = self.revision.region_policy(
            {"classification": "speech", "source_text": _rand_sentence(), "confidence": 0.9},
            user_decision="ocr_invalid")
        self.assertEqual(policy["normalized_classification"], tax.OCR_INVALID)
        self.assertFalse(policy["translatable"])
        self.assertFalse(policy["provider_required"])

    # --- provider / cache -------------------------------------------------
    def test_provider_required_only_when_translatable_and_uncached(self):
        text = _rand_sentence()
        uncached = self.revision.region_policy(
            {"classification": "speech", "source_text": text, "confidence": 0.9})
        cached = self.revision.region_policy(
            {"classification": "speech", "source_text": text, "confidence": 0.9},
            cache_status="hit")
        self.assertTrue(uncached["provider_required"])
        self.assertFalse(cached["provider_required"])
        self.assertTrue(cached["reviewable"])


def _synthetic_chapter(root: Path, *, pages):
    """A chapter whose ids, texts and page numbers are generated per run."""
    from PIL import Image
    output = root / f"ch_{uuid.uuid4().hex[:8]}"
    output.mkdir(parents=True)
    progress_pages = []
    for number, items in pages:
        image = output / f"page_{number:03d}.jpg"
        Image.new("RGB", (200, 260), "white").save(image, "JPEG")
        progress_pages.append({
            "index": number, "sequence_index": number,
            "image_path": str(image), "output_path": str(image),
            "debug_data": {"image_path": str(image), "items": items}})
    (output / "progress.json").write_text(json.dumps(
        {"pdf_path": str(output / "c.pdf"), "pages": progress_pages}), encoding="utf-8")
    (output / "quality_report.json").write_text(json.dumps(
        {"pages": [{"index": n} for n, _ in pages]}), encoding="utf-8")
    return output


class TwoSyntheticChaptersContracts(unittest.TestCase):
    """The same policy must drive two chapters that share nothing."""

    def _revision(self, output):
        return ChapterQualityRevision(output, job_id=uuid.uuid4().hex, run_id=uuid.uuid4().hex)

    def _chapter_a(self, root):
        # dialogue, semantic decorative, sfx, watermark, ocr-invalid
        page = random.randint(2, 40)
        items = [
            {"id": _rand_word(), "region_id": f"R_{uuid.uuid4().hex[:6]}", "classification": "speech",
             "clean_text": _rand_sentence(), "translation": "", "confidence": 0.95, "redrawn": True},
            {"id": _rand_word(), "region_id": f"R_{uuid.uuid4().hex[:6]}", "classification": "decorative",
             "clean_text": _rand_sentence(4), "translation": "", "confidence": 0.9, "preserved_original": True},
            {"id": _rand_word(), "region_id": f"R_{uuid.uuid4().hex[:6]}", "classification": "decorative",
             "clean_text": "WHOOSH", "translation": "WHOOSH", "confidence": 0.9, "preserved_original": True},
            {"id": _rand_word(), "region_id": f"R_{uuid.uuid4().hex[:6]}", "classification": "speech",
             "clean_text": f"www.{_rand_word(5).lower()}.com", "translation": "", "confidence": 0.9},
            {"id": _rand_word(), "region_id": f"R_{uuid.uuid4().hex[:6]}", "classification": "unknown",
             "clean_text": _consonant_garbage(4), "translation": "", "confidence": 0.15},
        ]
        return _synthetic_chapter(root, pages=[(page, items)]), page

    def _chapter_b(self, root):
        # different count, reversed page order, legacy labels, no shared ids
        first, second = random.randint(50, 70), random.randint(71, 99)
        page_hi = [{"id": _rand_word(), "region_id": f"Z{uuid.uuid4().hex[:5]}", "classification": "narration",
                    "clean_text": _rand_sentence(8), "translation": "", "confidence": 0.88, "redrawn": True}]
        page_lo = [{"id": _rand_word(), "region_id": f"Z{uuid.uuid4().hex[:5]}", "classification": "editorial",
                    "clean_text": _rand_sentence(5), "translation": "", "confidence": 0.8},
                   {"id": _rand_word(), "region_id": f"Z{uuid.uuid4().hex[:5]}", "classification": "sfx",
                    "clean_text": "CLANG", "translation": "CLANG", "confidence": 0.9, "preserved_original": True}]
        # deliberately reversed order in the progress file
        return _synthetic_chapter(root, pages=[(second, page_hi), (first, page_lo)]), (first, second)

    def test_both_chapters_route_through_the_same_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            out_a, page_a = self._chapter_a(root)
            listing = self._revision(out_a).list_page_regions(page_a)
            by_class = {r["classification_normalized"] for r in listing["regions"]}
            self.assertIn(tax.DIALOGUE_TRANSLATE, by_class)
            self.assertIn(tax.DECORATIVE_SEMANTIC_TRANSLATE, by_class)  # the core fix
            self.assertIn(tax.SFX_PRESERVE, by_class)
            self.assertIn(tax.URL_PRESERVE, by_class)
            # preserve classes are not reviewable; semantic ones are
            for region in listing["regions"]:
                if region["classification_normalized"] in tax.PRESERVE:
                    self.assertFalse(region["reviewable"], region["region_id"])
                if region["classification_normalized"] in tax.TRANSLATE:
                    self.assertTrue(region["reviewable"], region["region_id"])

            out_b, (first, second) = self._chapter_b(root)
            revision_b = self._revision(out_b)
            for page in (first, second):
                listing_b = revision_b.list_page_regions(page)
                self.assertTrue(listing_b["regions"])
                for region in listing_b["regions"]:
                    self.assertIn(region["classification_normalized"], tax.ALL_CATEGORIES)

    def test_reordered_pages_do_not_change_the_outcome(self):
        with tempfile.TemporaryDirectory() as folder:
            out_b, (first, second) = self._chapter_b(Path(folder))
            revision = self._revision(out_b)
            # The higher page number appears first in the file; both still resolve.
            self.assertEqual(revision.list_page_regions(first)["page"], first)
            self.assertEqual(revision.list_page_regions(second)["page"], second)

    def test_forgotten_text_uses_the_same_policy_as_the_review(self):
        with tempfile.TemporaryDirectory() as folder:
            out_a, page_a = self._chapter_a(Path(folder))
            revision = self._revision(out_a)
            listing = {r["region_id"]: r for r in revision.list_page_regions(page_a)["regions"]}
            for candidate in revision.search_forgotten_text(page_a)["candidates"]:
                mirror = listing.get(candidate["region_id"])
                if not mirror:
                    continue
                self.assertEqual(candidate["classification_normalized"],
                                 mirror["classification_normalized"], candidate["region_id"])
                # a preserve class is never proposed for translation
                if candidate["do_not_translate"]:
                    self.assertEqual(candidate["suggested_action"], "keep_preserved")

    def test_no_pdf_or_revision_is_created_by_inspection(self):
        with tempfile.TemporaryDirectory() as folder:
            out_a, page_a = self._chapter_a(Path(folder))
            revision = self._revision(out_a)
            revision.list_page_regions(page_a)
            revision.search_forgotten_text(page_a)
            self.assertEqual(list(Path(out_a).glob("*.pdf")), [])
            self.assertFalse((Path(out_a) / "quality_revision_pages").exists())


if __name__ == "__main__":
    unittest.main()
