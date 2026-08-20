import _test_bootstrap  # noqa: F401
from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import benchmark_pipeline


class RunManifestIdentityTests(unittest.TestCase):
    def test_job_run_id_is_preserved_in_run_manifest(self):
        output = Path(tempfile.mkdtemp()) / "chapter" / "3a396e01-77d9-44b4-95a0-4759e2e7eb17"
        report = {
            "job_run_id": "3a396e01-77d9-44b4-95a0-4759e2e7eb17",
            "run_signature": "signature",
            "status": "review_required",
            "quality_validation": {"passed": False, "manual_review_required_groups": 1},
            "pdf_path": str(output / "chapter.pdf"),
        }

        manifest = benchmark_pipeline._output_run_manifest(
            output,
            report,
            SimpleNamespace(model="fake-offline"),
        )

        self.assertEqual(manifest["run_id"], "3a396e01-77d9-44b4-95a0-4759e2e7eb17")
        self.assertEqual(manifest["slug"], "3a396e01-77d9-44b4-95a0-4759e2e7eb17")


if __name__ == "__main__":
    unittest.main()
