"""The authorization request is the contract: nothing else may be sent (BLOCO 6B).

Every test here runs against a fake translator. The offline guard makes a real
network call fail loudly rather than silently costing money.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import random
import tempfile
import unittest
import uuid
from pathlib import Path

import audit_decisions
import linguistic_triage as lt
import provider_execution as pe
import region_taxonomy as tax


def _rid():
    return uuid.uuid4().hex[:8].upper()


def _sentence():
    stock = ["THE", "HARBOUR", "WAS", "EMPTY", "WHEN", "THEY", "ARRIVED", "LATE"]
    return " ".join(random.choice(stock) for _ in range(6)) + "."


class FakeTranslator:
    """Records exactly what it was asked to translate."""

    is_configured = True
    model = "fake/model"
    base_url = "https://example.invalid/v1"

    def __init__(self, *, fail=False, wrong_count=False):
        self.seen = []
        self.calls = 0
        self.fail = fail
        self.wrong_count = wrong_count
        self.stats = {"api_requests": 0, "api_texts": 0, "cache_hits": 0}

    def translate_many(self, texts):
        self.calls += 1
        self.seen.append(list(texts))
        if self.fail:
            raise RuntimeError("provider unavailable")
        self.stats["api_requests"] += 1
        self.stats["api_texts"] += len(texts)
        out = [f"[pt] {t}" for t in texts]
        return out[:-1] if self.wrong_count else out


def _record(region_id, page_id, text, **kw):
    return {"region_id": region_id, "page_id": page_id, "page_number": kw.get("page_number", 1),
            "classification_normalized": kw.get("category", tax.DIALOGUE_TRANSLATE),
            "source_text": text, "current_translation": "",
            "translatable": True, "preservable": False,
            "provider_required": kw.get("provider_required", True),
            "needs_human_review": kw.get("human", False),
            "confidence": 0.9,
            "linguistic_gate": {"status": lt.PASSED, "checks": {}}}


class ExecutionScope(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.output = self.dir.name
        self.revision = _rid()
        self.records = [_record(f"p00{i}:R{_rid()}", f"p00{i}", _sentence()) for i in (3, 4, 5)]
        self.extra = _record(f"p009:R{_rid()}", "p009", _sentence())
        self.request = {
            "authorization_request_id": uuid.uuid4().hex,
            "status": pe.READY, "provider_executed": False,
            "source_audit_hash": "hash-a",
            "region_ids": [r["region_id"] for r in self.records],
            "pages": ["p003", "p004", "p005"], "estimated_requests": 3,
        }
        Path(self.output, "quality_revision").mkdir(parents=True)
        pe.requests_path(self.output).write_text(
            json.dumps({"requests": [self.request]}), encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def _all(self):
        return self.records + [self.extra]

    def _plan(self, **kw):
        return pe.plan_execution(self.request, self._all(), source_audit_hash="hash-a", **kw)

    # --- scope ------------------------------------------------------------
    def test_only_the_authorized_regions_are_sent(self):
        plan = self._plan()
        translator = FakeTranslator()
        pe.execute(self.output, self.request, plan, translator=translator,
                   revision_id=self.revision, confirm=True)
        self.assertEqual(len(translator.seen), 1)
        sent = translator.seen[0]
        self.assertEqual(len(sent), 3)
        self.assertNotIn(self.extra["source_text"], sent)
        for record in self.records:
            self.assertIn(record["source_text"], sent)

    def test_a_region_outside_the_audit_is_refused(self):
        self.request["region_ids"] = self.request["region_ids"] + ["p099:GHOST"]
        with self.assertRaisesRegex(ValueError, "region_not_in_audit"):
            self._plan()

    def test_a_region_that_left_ready_state_stops_the_whole_run(self):
        target = self.records[0]["region_id"]
        with self.assertRaisesRegex(ValueError, "authorized_scope_drifted"):
            self._plan(decisions={target: {"decision": "ocr_invalid"}})

    def test_a_changed_audit_stops_the_run(self):
        with self.assertRaisesRegex(ValueError, "source_audit_hash_mismatch"):
            pe.plan_execution(self.request, self._all(), source_audit_hash="hash-b")

    def test_an_empty_source_is_never_sent(self):
        self.records[0]["source_text"] = "   "
        with self.assertRaisesRegex(ValueError, "authorized_scope_drifted"):
            self._plan()

    # --- one shot ---------------------------------------------------------
    def test_confirmation_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "explicit_confirmation_required"):
            pe.execute(self.output, self.request, self._plan(),
                       translator=FakeTranslator(), revision_id=self.revision)

    def test_an_executed_request_cannot_run_again(self):
        translator = FakeTranslator()
        pe.execute(self.output, self.request, self._plan(), translator=translator,
                   revision_id=self.revision, confirm=True)
        spent = pe.find_request(self.output, self.request["authorization_request_id"])
        self.assertTrue(spent["provider_executed"])
        self.assertEqual(spent["status"], pe.EXECUTED)
        with self.assertRaisesRegex(ValueError, "already_executed"):
            pe.plan_execution(spent, self._all(), source_audit_hash="hash-a")
        self.assertEqual(translator.calls, 1)

    def test_an_unconfigured_provider_is_refused_before_any_call(self):
        translator = FakeTranslator()
        translator.is_configured = False
        with self.assertRaisesRegex(ValueError, "provider_not_configured"):
            pe.execute(self.output, self.request, self._plan(), translator=translator,
                       revision_id=self.revision, confirm=True)
        self.assertEqual(translator.calls, 0)

    def test_a_mismatched_response_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "mismatched_count"):
            pe.execute(self.output, self.request, self._plan(),
                       translator=FakeTranslator(wrong_count=True),
                       revision_id=self.revision, confirm=True)

    def test_a_failed_call_leaves_the_request_reusable(self):
        with self.assertRaises(RuntimeError):
            pe.execute(self.output, self.request, self._plan(), translator=FakeTranslator(fail=True),
                       revision_id=self.revision, confirm=True)
        still = pe.find_request(self.output, self.request["authorization_request_id"])
        self.assertFalse(still["provider_executed"])
        self.assertEqual(still["status"], pe.READY)

    # --- what gets sent ---------------------------------------------------
    def test_a_human_corrected_reading_is_sent_instead_of_the_bad_read(self):
        target = self.records[0]
        corrected = "WHAT THE IMAGE ACTUALLY SAYS."
        decisions = {target["region_id"]: {
            "decision": "translate",
            "notes": audit_decisions.CORRECTED_READING_PREFIX + corrected}}
        plan = self._plan(decisions=decisions)
        translator = FakeTranslator()
        result = pe.execute(self.output, self.request, plan, translator=translator,
                            revision_id=self.revision, confirm=True)
        self.assertIn(corrected, translator.seen[0])
        self.assertNotIn(target["source_text"], translator.seen[0])
        entry = next(r for r in result["results"] if r["region_id"] == target["region_id"])
        self.assertEqual(entry["text_origin"], "human_corrected_reading")
        self.assertEqual(entry["ocr_source_text"], target["source_text"])

    # --- what it must not touch ------------------------------------------
    def test_nothing_but_the_audit_area_is_written(self):
        before = {p.relative_to(self.output) for p in Path(self.output).rglob("*") if p.is_file()}
        pe.execute(self.output, self.request, self._plan(), translator=FakeTranslator(),
                   revision_id=self.revision, confirm=True)
        after = {p.relative_to(self.output) for p in Path(self.output).rglob("*") if p.is_file()}
        created = {str(p) for p in after - before}
        self.assertTrue(created, "the execution artifact must exist")
        for path in created:
            self.assertIn("linguistic_audit", path, f"wrote outside the audit area: {path}")
        self.assertEqual(list(Path(self.output).rglob("*.pdf")), [])

    def test_the_record_states_nothing_was_applied(self):
        record = pe.execute(self.output, self.request, self._plan(), translator=FakeTranslator(),
                            revision_id=self.revision, confirm=True)
        self.assertFalse(record["applied_to_pdf"])
        self.assertFalse(record["applied_to_pages"])
        self.assertFalse(record["published"])
        self.assertEqual(record["api_requests"], 1)
        self.assertEqual(len(record["results"]), 3)

    def test_no_credential_is_recorded(self):
        record = pe.execute(self.output, self.request, self._plan(), translator=FakeTranslator(),
                            revision_id=self.revision, confirm=True)
        blob = json.dumps(record).lower()
        for forbidden in ("api_key", "authorization:", "bearer", "cookie", "senha", "token", "secret"):
            self.assertNotIn(forbidden, blob)


class CorrectedReadingContract(unittest.TestCase):
    def test_the_ui_writes_the_prefix_the_backend_reads(self):
        js = Path(__file__).resolve().parent.joinpath("static", "tradutor_ui.js").read_text(encoding="utf-8")
        self.assertIn(audit_decisions.CORRECTED_READING_PREFIX.strip().rstrip(":"), js)


if __name__ == "__main__":
    unittest.main()
