import _test_bootstrap  # noqa: F401

import unittest

from benchmark_environment import SystemLoadSnapshot, clean_benchmark_preflight


class CleanBenchmarkPreflightTests(unittest.TestCase):
    def test_heavy_game_process_blocks_benchmark_before_job_creation(self):
        result = clean_benchmark_preflight(
            duration_seconds=0,
            sampler=lambda: SystemLoadSnapshot(
                cpu_percent=12.0,
                ram_percent=45.0,
                available_ram_mb=8192,
                heavy_process_names=("eldenring.exe",),
            ),
        )

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["reason_code"], "BENCHMARK_ENVIRONMENT_NOT_CLEAN")
        self.assertEqual(result["heavy_process_names"], ["eldenring.exe"])

    def test_idle_window_passes_without_heavy_workload(self):
        result = clean_benchmark_preflight(
            duration_seconds=0,
            sampler=lambda: SystemLoadSnapshot(
                cpu_percent=8.0,
                ram_percent=40.0,
                available_ram_mb=12000,
                heavy_process_names=(),
            ),
        )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["warnings"], [])


if __name__ == "__main__":
    unittest.main()
