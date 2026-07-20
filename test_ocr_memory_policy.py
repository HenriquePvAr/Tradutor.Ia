from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

from ocr_memory_policy import MemorySnapshot, choose_workers


class OcrMemoryPolicyTests(unittest.TestCase):
    def test_low_memory_forces_one_worker(self):
        decision = choose_workers(
            4,
            memory=MemorySnapshot(available_memory_mb=5000, total_memory_mb=32000),
            estimated_worker_peak_mb=1800,
            reserve_mb=4096,
        )
        self.assertEqual(decision.workers, 1)
        self.assertEqual(decision.memory_mode, "reduced")

    def test_heavy_engine_never_duplicates_by_default(self):
        decision = choose_workers(
            4,
            memory=MemorySnapshot(available_memory_mb=30000, total_memory_mb=32000),
            estimated_worker_peak_mb=1800,
            reserve_mb=2048,
            engine_heavy=True,
        )
        self.assertEqual(decision.workers, 1)
        self.assertIn("heavy_engine", decision.reason)

    def test_large_image_reduces_concurrency(self):
        decision = choose_workers(
            2,
            memory=MemorySnapshot(available_memory_mb=16000, total_memory_mb=32000),
            estimated_worker_peak_mb=1200,
            reserve_mb=1024,
            engine_heavy=False,
            largest_image_pixels=9_000_000,
        )
        self.assertEqual(decision.workers, 1)
        self.assertIn("large_image", decision.reason)

    def test_unavailable_metrics_fail_closed(self):
        decision = choose_workers(2, memory=None, engine_heavy=True)
        self.assertEqual(decision.workers, 1)
        self.assertIn("metrics_unavailable", decision.reason)


if __name__ == "__main__":
    unittest.main()
