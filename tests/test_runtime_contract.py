import tempfile
import unittest
from pathlib import Path

from job_store import JobStore
from runtime_contract import canonical_runtime_identity, current_runtime_contract


class RuntimeContractTests(unittest.TestCase):
    def test_python_and_pythonw_from_same_venv_share_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            scripts = root / "Scripts"
            scripts.mkdir()
            python = scripts / "python.exe"
            pythonw = scripts / "pythonw.exe"
            python.write_bytes(b"python launcher")
            pythonw.write_bytes(b"different pythonw launcher")

            self.assertEqual(
                canonical_runtime_identity(python, runtime_prefix=root),
                canonical_runtime_identity(pythonw, runtime_prefix=root),
            )
            self.assertEqual(
                current_runtime_contract(python, runtime_prefix=root),
                current_runtime_contract(pythonw, runtime_prefix=root),
            )

    def test_same_build_and_identity_share_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            exe = root / "YomuSekai.exe"
            exe.write_bytes(b"same product payload")
            self.assertEqual(
                current_runtime_contract(exe, frozen=True),
                current_runtime_contract(exe, frozen=True),
            )

    def test_different_canonical_identity_changes_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            self.assertNotEqual(
                current_runtime_contract(first / "python.exe", runtime_prefix=first),
                current_runtime_contract(second / "python.exe", runtime_prefix=second),
            )

    def test_matching_contract_allows_claim(self):
        with tempfile.TemporaryDirectory() as raw:
            store = JobStore(Path(raw) / "jobs.sqlite3")
            try:
                job = store.create_job(
                    source_url="https://example.test", output_dir=str(Path(raw) / "out"),
                    command=[], worker_contract="contract-a")
                claimed = store.claim_next_job("worker-a", 101, worker_contract="contract-a")
                self.assertEqual(claimed["id"], job)
            finally:
                store.close()

    def test_mismatching_contract_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            store = JobStore(Path(raw) / "jobs.sqlite3")
            try:
                store.create_job(
                    source_url="https://example.test", output_dir=str(Path(raw) / "out"),
                    command=[], worker_contract="contract-a")
                self.assertIsNone(
                    store.claim_next_job("worker-b", 102, worker_contract="contract-b"))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
