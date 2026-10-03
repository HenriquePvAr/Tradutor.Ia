from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import resource_monitor
from resource_monitor import ResourceMonitor


class ResourceMonitorTests(unittest.TestCase):
    def test_disabled_monitor_is_safe_without_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = ResourceMonitor(tmp, enabled=False)
            monitor.start()
            summary = monitor.stop()
            self.assertFalse(summary["enabled"])
            self.assertIn("reason", summary)

    def test_missing_psutil_disables_monitor(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(resource_monitor, "psutil", None):
                monitor = ResourceMonitor(tmp, enabled=True)
                self.assertFalse(monitor.enabled)
                summary = monitor.stop()
                self.assertFalse(summary["enabled"])

    def test_report_writer_uses_existing_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = ResourceMonitor(tmp, enabled=False)
            monitor.enabled = True
            monitor.samples = [
                {
                    "timestamp": 1.0,
                    "elapsed_s": 0.0,
                    "stage": "ocr",
                    "pages_done": 1,
                    "pages_total": 2,
                    "queue_depth": 1,
                    "active_workers": 1,
                    "parent_pid": 100,
                    "parent_rss_mb": 500.0,
                    "parent_vms_mb": 900.0,
                    "parent_cpu_percent": 20.0,
                    "parent_threads": 4,
                    "child_count": 1,
                    "children_rss_mb": 1500.0,
                    "total_pipeline_rss_mb": 2000.0,
                    "system_available_mb": 12000.0,
                    "system_ram_percent": 50.0,
                    "swap_used_mb": 0.0,
                    "cpu_percent": 80.0,
                    "children": [
                        {
                            "pid": 200,
                            "role": "ocr-worker",
                            "rss_mb": 1500.0,
                            "cpu_percent": 90.0,
                        }
                    ],
                }
            ]
            summary = monitor.write_reports()
            self.assertTrue(summary["enabled"])
            self.assertEqual(summary["sample_count"], 1)
            self.assertEqual(summary["ocr_worker_rss_mb_peak"], 1500.0)
            self.assertTrue(Path(summary["resource_report_json"]).is_file())
            self.assertTrue(Path(summary["resource_report_html"]).is_file())
            self.assertTrue(Path(summary["resource_timeline_csv"]).is_file())


if __name__ == "__main__":
    unittest.main()
