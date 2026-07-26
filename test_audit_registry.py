"""Hermetic tests for additive audit registration/resolution (BLOCO 3)."""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import tempfile
import unittest
from pathlib import Path

import audit_registry as reg
import linguistic_triage as lt
import region_taxonomy as tax


def _report(revision_id="rev1", records=None):
    return {"taxonomy_version": tax.TAXONOMY_VERSION, "gate_version": lt.GATE_VERSION,
            "revision_id": revision_id,
            "records": records if records is not None else [{"region_id": "p001:R1"}],
            "by_normalized_category": {"dialogue_translate": 1}, "total_regions_audited": 1}


class AuditRegistryContracts(unittest.TestCase):
    def test_register_and_resolve_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            entry = reg.register_audit(folder, "rev1", _report())
            self.assertTrue(entry["audit_artifact_id"])
            resolved = reg.resolve_registered_audit(folder, "rev1")
            self.assertEqual(resolved["report"]["revision_id"], "rev1")
            self.assertEqual(resolved["entry"]["source_audit_hash"], entry["source_audit_hash"])

    def test_unregistered_revision_resolves_to_none(self):
        with tempfile.TemporaryDirectory() as folder:
            reg.register_audit(folder, "rev1", _report())
            self.assertIsNone(reg.resolve_registered_audit(folder, "revX"))

    def test_tampered_report_fails_closed_on_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            reg.register_audit(folder, "rev1", _report())
            path = Path(folder) / "quality_revision" / "linguistic_audit" / "rev1" / "linguistic_page_audit.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["records"].append({"region_id": "sneaky"})
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash_mismatch"):
                reg.resolve_registered_audit(folder, "rev1")

    def test_path_traversal_in_registry_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            reg.register_audit(folder, "rev1", _report())
            registry = Path(folder) / "quality_revision" / "linguistic_audit" / "registry.json"
            data = json.loads(registry.read_text(encoding="utf-8"))
            data["artifacts"]["rev1"]["report_relpath"] = "../../../../etc/passwd"
            registry.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                reg.resolve_registered_audit(folder, "rev1")

    def test_schema_and_taxonomy_version_validation(self):
        with self.assertRaisesRegex(ValueError, "missing_records"):
            reg.validate_report_schema({"taxonomy_version": tax.TAXONOMY_VERSION,
                                        "gate_version": lt.GATE_VERSION,
                                        "revision_id": "r", "by_normalized_category": {},
                                        "total_regions_audited": 0})
        with self.assertRaisesRegex(ValueError, "taxonomy_version_mismatch"):
            reg.validate_report_schema(_report() | {"taxonomy_version": "999"})

    def test_registration_is_additive_not_destructive(self):
        with tempfile.TemporaryDirectory() as folder:
            reg.register_audit(folder, "rev1", _report())
            reg.register_audit(folder, "rev2", _report(revision_id="rev2"))
            registry = json.loads((Path(folder) / "quality_revision" / "linguistic_audit" / "registry.json")
                                  .read_text(encoding="utf-8"))
            self.assertEqual(set(registry["artifacts"]), {"rev1", "rev2"})


if __name__ == "__main__":
    unittest.main()
