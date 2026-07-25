"""Hermetic tests for the human audit decisions store (BLOCO 3)."""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import tempfile
import unittest
from pathlib import Path

from audit_decisions import AuditDecisionStore


class AuditDecisionStoreContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = AuditDecisionStore(self.tmp / "decisions.sqlite3")
        self.base = dict(job_id="j", run_id="ru", revision_id="rev1", audit_artifact_id="aid",
                         page_id="p001", region_id="p001:R1", created_by="user-1")

    def tearDown(self):
        self.store.close()

    def test_create_get_and_list(self):
        made = self.store.upsert(decision="translate", **self.base)
        self.assertEqual(made["decision"], "translate")
        self.assertEqual(self.store.get(made["audit_decision_id"])["region_id"], "p001:R1")
        self.assertEqual(len(self.store.list_for("j", "ru", "rev1", created_by="user-1")), 1)

    def test_upsert_is_idempotent_per_user_region_revision(self):
        first = self.store.upsert(decision="translate", **self.base)
        second = self.store.upsert(decision="preserve", **self.base)
        self.assertEqual(first["audit_decision_id"], second["audit_decision_id"])
        self.assertEqual(second["decision"], "preserve")
        self.assertGreaterEqual(second["updated_at"], first["created_at"])
        self.assertEqual(len(self.store.list_for("j", "ru", "rev1")), 1)

    def test_a_second_user_gets_a_separate_decision(self):
        self.store.upsert(decision="translate", **self.base)
        other = {**self.base, "created_by": "user-2"}
        self.store.upsert(decision="preserve", **other)
        self.assertEqual(len(self.store.list_for("j", "ru", "rev1")), 2)
        self.assertEqual(len(self.store.list_for("j", "ru", "rev1", created_by="user-1")), 1)

    def test_invalid_decision_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid_decision"):
            self.store.upsert(decision="nonsense", **self.base)

    def test_missing_identity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing_created_by"):
            self.store.upsert(decision="translate", **{**self.base, "created_by": ""})

    def test_delete_requires_ownership(self):
        made = self.store.upsert(decision="translate", **self.base)
        with self.assertRaisesRegex(ValueError, "not_decision_owner"):
            self.store.delete(made["audit_decision_id"], created_by="intruder")
        self.assertTrue(self.store.delete(made["audit_decision_id"], created_by="user-1"))
        self.assertEqual(len(self.store.list_for("j", "ru", "rev1")), 0)

    def test_deleting_unknown_is_false(self):
        self.assertFalse(self.store.delete("does-not-exist", created_by="user-1"))

    def test_all_valid_decisions_accepted(self):
        from audit_decisions import DECISIONS
        for i, decision in enumerate(DECISIONS):
            self.store.upsert(decision=decision, **{**self.base, "region_id": f"p001:R{i}"})
        self.assertEqual(len(self.store.list_for("j", "ru", "rev1")), len(DECISIONS))


if __name__ == "__main__":
    unittest.main()
