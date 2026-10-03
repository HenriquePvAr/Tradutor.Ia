from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from job_store import JobStatus, JobStore


class WorkerOwnershipContractTests(unittest.TestCase):
    def test_old_worker_cannot_claim_new_runtime_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            job_id = store.create_job(
                source_url="local-folder:test",
                output_dir="out",
                command=["candidate-b"],
                worker_contract="contract-b",
            )

            self.assertIsNone(
                store.claim_next_job("worker-a", 101, worker_contract="contract-a")
            )
            claimed = store.claim_next_job("worker-b", 102, worker_contract="contract-b")
            self.assertEqual(claimed["id"], job_id)
            self.assertEqual(claimed["worker_contract"], "contract-b")
            store.close()

    def test_legacy_unbound_job_remains_claimable_by_compatibility_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            job_id = store.create_job(
                source_url="local-folder:legacy",
                output_dir="out",
                command=["legacy"],
                worker_contract="",
            )
            store.update_fields(job_id, worker_contract="")
            claimed = store.claim_next_job("worker-new", 103, worker_contract="contract-new")
            self.assertEqual(claimed["id"], job_id)
            self.assertEqual(claimed["status"], JobStatus.CLAIMING)
            store.close()


if __name__ == "__main__":
    unittest.main()
