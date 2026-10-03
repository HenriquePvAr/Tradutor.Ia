from __future__ import annotations

import _test_bootstrap  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path

from source_support_report import (
    InMemorySourceSupportReporter,
    LocalOutboxSourceSupportReporter,
    build_source_support_report,
)


class SourceSupportReportTests(unittest.TestCase):
    def test_report_contains_only_sanitized_metadata(self):
        report = build_source_support_report({
            "url": "HTTPS://Example.Org/series/x/chapter-1#frag",
            "detected_adapter": "universal",
            "failure_reason_code": "unsupported_source",
            "user_note": "please add this reader",
            "cookie": "secret",
            "Authorization": "Bearer secret",
        })

        public = report.public()
        self.assertEqual(public["url"], "https://example.org/series/x/chapter-1")
        self.assertEqual(public["domain"], "example.org")
        self.assertEqual(public["failure_reason_code"], "unsupported_source")
        self.assertNotIn("cookie", json.dumps(public).lower())
        self.assertNotIn("bearer", json.dumps(public).lower())
        self.assertNotIn("chapter_images", public)

    def test_user_note_with_secret_marker_is_dropped(self):
        report = build_source_support_report({
            "url": "https://example.org/chapter",
            "failure_reason_code": "unsupported_source",
            "user_note": "Authorization: Bearer jwt.secret",
        })

        self.assertEqual(report.user_note, "")

    def test_in_memory_reporter_is_idempotent_by_url_and_reason(self):
        reporter = InMemorySourceSupportReporter()
        report = build_source_support_report({
            "url": "https://example.org/chapter",
            "failure_reason_code": "unsupported_source",
        })

        first = reporter.submit(report)
        second = reporter.submit(report)

        self.assertTrue(first.queued)
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.report_id, second.report_id)

    def test_local_outbox_persists_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            reporter = LocalOutboxSourceSupportReporter(Path(tmp) / "reports.jsonl")
            report = build_source_support_report({
                "url": "https://example.org/chapter",
                "failure_reason_code": "unsupported_source",
            })

            self.assertFalse(reporter.submit(report).duplicate)
            self.assertTrue(reporter.submit(report).duplicate)

            lines = (Path(tmp) / "reports.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["url"], "https://example.org/chapter")


if __name__ == "__main__":
    unittest.main()
