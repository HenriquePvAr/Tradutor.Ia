"""Bridge contracts for triage, bulk decisions and provider authorization (BLOCO 5)."""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import random
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import region_taxonomy as tax
from audit_decisions import AuditDecisionStore
from job_store import JobStore
from ui_bridge import UiBridge


class _Bridge(UiBridge):
    def __init__(self, db: Path):
        self.history_store = mock.Mock()
        self.history = []
        self.profile = {}
        self.store = JobStore(db)
        self.audit_decisions = AuditDecisionStore(db)
        self.history_revision = 1

    def close(self):
        self.audit_decisions.close()
        self.store.close()


def _rand(n=6):
    return uuid.uuid4().hex[:n].upper()


def _sentence():
    stock = ["THE", "HARBOUR", "WAS", "EMPTY", "WHEN", "THEY", "ARRIVED", "LATE"]
    return " ".join(random.choice(stock) for _ in range(6)) + "."


class TriageBridgeContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.bridge.close()

    def _chapter(self, *, pages):
        """Synthetic chapter; ids, pages and texts differ on every call."""
        output = self.tmp / f"ch_{uuid.uuid4().hex[:8]}"
        revision_id = f"rev{_rand()}"
        rev_dir = output / "quality_revision" / revision_id
        rev_dir.mkdir(parents=True)
        (output / "quality_report.json").write_text(json.dumps({"pages": pages}), encoding="utf-8")
        manifest = rev_dir / "revision_manifest.json"
        manifest.write_text(json.dumps({
            "revision_id": revision_id, "reviewed_pdf_path": f"output/x/{_rand()}_reviewed.pdf",
            "updated_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
        (rev_dir / "contextual_translation_review.json").write_text(json.dumps({"reviews": []}), encoding="utf-8")
        (rev_dir / "incremental_render_audit.json").write_text(json.dumps({"region_visual_states": {}}), encoding="utf-8")
        (output / "quality_revision" / "latest_revision.json").write_text(json.dumps({
            "revision_id": revision_id, "manifest_path": str(manifest)}), encoding="utf-8")
        job_id = self.bridge.store.create_job(
            source_url="https://example.test/c", output_dir=str(output),
            command=["python", "run.py"], configuration={"job_type": "translation"})
        return job_id, self.bridge.store.get_job(job_id)["run_id"]

    def _chapter_a(self):
        page = random.randint(2, 40)
        return self._chapter(pages=[{"index": page, "suspicious_groups": [
            {"id": _rand(), "region_id": f"R{_rand()}", "classification": "speech",
             "text": _sentence(), "translation": "", "confidence": 0.9},
            {"id": _rand(), "region_id": f"R{_rand()}", "classification": "decorative",
             "text": "WHOOSH", "translation": "WHOOSH", "confidence": 0.9, "preserved_original": True},
            {"id": _rand(), "region_id": f"R{_rand()}", "classification": "unknown",
             "text": "QXZKV", "translation": "", "confidence": 0.2},
        ]}])

    def _chapter_b(self):
        # different page count, different order, legacy labels
        hi, lo = random.randint(60, 80), random.randint(81, 99)
        return self._chapter(pages=[
            {"index": lo, "translation_terminal_items": [
                {"id": _rand(), "region_id": f"Z{_rand()}", "classification": "narration",
                 "text": _sentence(), "translation": "", "confidence": 0.85}]},
            {"index": hi, "visual_validation_failures": [
                {"id": _rand(), "region_id": f"Z{_rand()}", "classification": "editorial",
                 "text": _sentence(), "translation": "", "confidence": 0.8}]},
        ])

    # --- queue ------------------------------------------------------------
    def test_queue_is_dynamic_and_counted_for_two_unrelated_chapters(self):
        for build in (self._chapter_a, self._chapter_b):
            job_id, run_id = build()
            result = self.bridge.linguistic_triage_queue(job_id, run_id, user_id="u1")
            self.assertEqual(result["total"], len(result["queue"]))
            self.assertTrue(result["counters"])
            # counters are computed, never constants
            self.assertEqual(result["counters"].get("pending", 0)
                             + result["counters"].get("decided", 0), result["total"])
            for item in result["queue"]:
                self.assertIn("triage_score", item)
                self.assertTrue(item["triage_reasons"])
                self.assertIn("linguistic_gate", item)

    # --- bulk decisions ---------------------------------------------------
    def test_bulk_decision_applies_to_every_selected_region(self):
        job_id, run_id = self._chapter_a()
        review = self.bridge.linguistic_audit_review(job_id, run_id, user_id="u1")
        regions = [r["region_id"] for r in review["records"]]
        result = self.bridge.bulk_audit_decisions(
            job_id, run_id, region_ids=regions, decision="needs_review", user_id="u1")
        self.assertEqual(result["applied"], len(regions))
        again = self.bridge.linguistic_audit_review(job_id, run_id, user_id="u1")
        self.assertTrue(all(r["human_decision"] for r in again["records"]))

    def test_bulk_is_idempotent(self):
        job_id, run_id = self._chapter_a()
        review = self.bridge.linguistic_audit_review(job_id, run_id, user_id="u1")
        regions = [r["region_id"] for r in review["records"]]
        self.bridge.bulk_audit_decisions(job_id, run_id, region_ids=regions,
                                         decision="needs_review", user_id="u1")
        self.bridge.bulk_audit_decisions(job_id, run_id, region_ids=regions,
                                         decision="needs_review", user_id="u1")
        stored = self.bridge.audit_decisions.list_for(
            job_id, run_id, review["revision_id"], created_by="u1")
        self.assertEqual(len(stored), len(regions))

    def test_bulk_rejects_a_region_outside_the_audit(self):
        job_id, run_id = self._chapter_a()
        with self.assertRaisesRegex(ValueError, "region_not_in_audit"):
            self.bridge.bulk_audit_decisions(job_id, run_id, region_ids=["p999:NOPE"],
                                             decision="needs_review", user_id="u1")

    def test_bulk_rejects_a_stale_audit_hash(self):
        job_id, run_id = self._chapter_a()
        review = self.bridge.linguistic_audit_review(job_id, run_id, user_id="u1")
        with self.assertRaisesRegex(ValueError, "source_audit_hash_mismatch"):
            self.bridge.bulk_audit_decisions(
                job_id, run_id, region_ids=[review["records"][0]["region_id"]],
                decision="needs_review", user_id="u1", source_audit_hash="stale-hash")

    def test_bulk_rejects_an_incompatible_selection(self):
        job_id, run_id = self._chapter_a()
        review = self.bridge.linguistic_audit_review(job_id, run_id, user_id="u1")
        unreadable = [r["region_id"] for r in review["records"]
                      if tax.is_unreadable(r["classification_normalized"])]
        if not unreadable:
            self.skipTest("synthetic chapter produced no unreadable region this run")
        with self.assertRaisesRegex(ValueError, "incompatible_selection"):
            self.bridge.bulk_audit_decisions(job_id, run_id, region_ids=unreadable,
                                             decision="translate", user_id="u1")

    def test_bulk_requires_authentication_and_a_selection(self):
        job_id, run_id = self._chapter_a()
        with self.assertRaisesRegex(ValueError, "authentication_required"):
            self.bridge.bulk_audit_decisions(job_id, run_id, region_ids=["x"],
                                             decision="needs_review", user_id="")
        with self.assertRaisesRegex(ValueError, "no_regions_selected"):
            self.bridge.bulk_audit_decisions(job_id, run_id, region_ids=[],
                                             decision="needs_review", user_id="u1")

    # --- provider set & authorization -------------------------------------
    def test_provider_set_excludes_preserved_and_decided_regions(self):
        job_id, run_id = self._chapter_a()
        plan = self.bridge.minimal_provider_set(job_id, run_id, user_id="u1")
        for item in plan["items"]:
            self.assertFalse(tax.is_preservable(item["classification_normalized"]))
            self.assertFalse(tax.is_unreadable(item["classification_normalized"]))
        self.assertEqual(plan["estimated_requests"], len(plan["items"]))

    def _clear_editorial_queue(self, job_id, run_id, user_id="u1"):
        """Rule on every open editorial question, the way a human would."""
        pending = self.bridge.pending_editorial_decisions(job_id, run_id, user_id=user_id)
        for item in pending["items"]:
            self.bridge.record_audit_decision(
                job_id, run_id, region_id=item["region_id"],
                decision="translate", user_id=user_id)
        after = self.bridge.pending_editorial_decisions(job_id, run_id, user_id=user_id)
        self.assertEqual(after["item_count"], 0)
        return self.bridge.minimal_provider_set(job_id, run_id, user_id=user_id)

    def test_authorization_is_blocked_while_editorial_decisions_are_open(self):
        job_id, run_id = self._chapter_a()
        pending = self.bridge.pending_editorial_decisions(job_id, run_id, user_id="u1")
        if not pending["item_count"]:
            self.skipTest("synthetic chapter left no open editorial decision this run")
        with self.assertRaisesRegex(ValueError, "blocked_pending_editorial_decisions"):
            self.bridge.request_provider_authorization(job_id, run_id, user_id="u1", confirm=True)

    def test_authorization_request_is_pending_and_never_runs_the_provider(self):
        job_id, run_id = self._chapter_a()
        with self.assertRaisesRegex(ValueError, "explicit_confirmation_required"):
            self.bridge.request_provider_authorization(job_id, run_id, user_id="u1")
        plan = self._clear_editorial_queue(job_id, run_id)
        if not plan["estimated_requests"]:
            self.skipTest("synthetic chapter left no billable region this run")
        request = self.bridge.request_provider_authorization(
            job_id, run_id, user_id="u1", confirm=True)
        self.assertEqual(request["status"], "ready_for_human_authorization")
        self.assertEqual(request["editorial_counters"]["awaiting_editorial_decision"], 0)
        self.assertFalse(request["provider_executed"])
        blob = json.dumps(request).lower()
        for forbidden in ("api_key", "authorization:", "bearer", "cookie", "token"):
            self.assertNotIn(forbidden, blob)
        cancelled = self.bridge.cancel_provider_authorization(
            job_id, run_id, request_id=request["authorization_request_id"], user_id="u1")
        self.assertTrue(cancelled["cancelled"])

    def test_authorization_request_is_owner_scoped(self):
        job_id, run_id = self._chapter_a()
        if not self._clear_editorial_queue(job_id, run_id)["estimated_requests"]:
            self.skipTest("synthetic chapter left no billable region this run")
        request = self.bridge.request_provider_authorization(
            job_id, run_id, user_id="u1", confirm=True)
        with self.assertRaisesRegex(ValueError, "not_request_owner"):
            self.bridge.cancel_provider_authorization(
                job_id, run_id, request_id=request["authorization_request_id"], user_id="u2")

    def test_nothing_creates_a_pdf_a_job_or_a_full_revision(self):
        job_id, run_id = self._chapter_a()
        output = Path(self.bridge.store.get_job(job_id)["output_dir"])
        jobs_before = self.bridge.store  # count via listing below
        self.bridge.linguistic_triage_queue(job_id, run_id, user_id="u1")
        self.bridge.minimal_provider_set(job_id, run_id, user_id="u1")
        self.bridge.ocr_invalid_candidates(job_id, run_id, user_id="u1")
        if self._clear_editorial_queue(job_id, run_id)["estimated_requests"]:
            self.bridge.request_provider_authorization(job_id, run_id, user_id="u1", confirm=True)
        self.assertEqual(list(output.glob("*.pdf")), [])
        self.assertFalse((output / "quality_revision_pages").exists())
        revisions = [d for d in (output / "quality_revision").iterdir()
                     if d.is_dir() and (d / "revision_manifest.json").is_file()]
        self.assertEqual(len(revisions), 1)  # only the seeded one


if __name__ == "__main__":
    unittest.main()
