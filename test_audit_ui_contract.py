"""Contract + anti-hardcoding tests for the audit UI (BLOCO 3)."""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JS = ROOT / "static" / "tradutor_ui.js"
SHELL = ROOT / "ui" / "ui_shell.html"

# The production sources that must carry no chapter-specific data.
PRODUCTION_FILES = [
    "region_taxonomy.py", "audit_registry.py", "audit_decisions.py",
    "linguistic_audit.py", "static/tradutor_ui.js", "ui/ui_shell.html",
]


class AuditUiContracts(unittest.TestCase):
    def setUp(self):
        self.js = JS.read_text(encoding="utf-8")
        self.shell = SHELL.read_text(encoding="utf-8")

    def test_audit_controls_exist(self):
        for marker in ("openLinguisticAudit", "linguisticAuditPanel", "auditSummary",
                       "auditFilters", "auditList", "AUDITORIA LINGUÍSTICA"):
            self.assertIn(marker, self.js + self.shell, marker)

    def test_audit_endpoints_are_wired(self):
        for endpoint in ("/api/ui/audit/review", "/api/ui/audit/decision", "/api/ui/audit/decision/delete"):
            self.assertIn(endpoint, self.js, endpoint)

    def test_decision_actions_and_filters_present(self):
        for value in ("translate", "preserve", "ocr_invalid", "needs_review", "dismissed"):
            self.assertIn(value, self.js, value)
        for f in ("auditFilterCategory", "auditFilterHumanReview", "auditFilterReportOnly",
                  "auditFilterProvider", "auditFilterCacheHit", "auditFilterPage"):
            self.assertIn(f, self.shell, f)

    def test_summary_is_data_driven_not_constant(self):
        # The summary must read from the review payload, not embed known counts.
        self.assertIn("review.summary", self.js)
        self.assertNotIn("108 regiões", self.js)
        self.assertNotIn("23 report_only", self.js)

    def test_f5_restore_hook(self):
        self.assertIn("restoreAuditFromUrl", self.js)
        self.assertIn("audit", self.js)

    def test_no_chapter_specific_hardcoding_in_production(self):
        forbidden = [
            "REAL COFFEE", "SINCE I'VE SPENT", "shadow_slave",
            "reviewed_v8", "reviewed_v9", 'p003:', 'p004:', 'p005:',
            "page_number ==", "region_id ==", "== 'v8'", '== "v8"',
        ]
        for rel in PRODUCTION_FILES:
            text = (ROOT / rel).read_text(encoding="utf-8")
            for needle in forbidden:
                self.assertNotIn(needle, text, f"{needle!r} found in production file {rel}")

    def test_region_scoped_review_action_exists(self):
        self.assertIn("REVISAR ESTA REGIÃO", self.js)
        self.assertIn("data-audit-revise-region", self.js)
        # It is gated by the backend's reviewable flag, not by a string check.
        self.assertIn("r.reviewable ? '' : ' disabled", self.js)

    def test_frontend_renders_backend_policy_not_its_own(self):
        # The panel shows the backend's normalized class and booleans; it must not
        # re-derive policy by comparing classification strings.
        self.assertIn("classification_normalized", self.js)
        self.assertIn("r.translatable", self.js)
        for inference in ("=== 'decorative'", '=== "decorative"', "=== 'sfx'", '=== "sfx"',
                          "=== 'watermark'", "=== 'sfx_preserve'", "=== 'decorative_semantic_translate'"):
            self.assertNotIn(inference, self.js, f"frontend infers policy from {inference}")

    def test_taxonomy_categories_are_not_page_conditioned(self):
        # No production module keys a decision on a literal page/region number.
        for rel in ("region_taxonomy.py", "linguistic_audit.py", "audit_registry.py",
                    "audit_decisions.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"page_number\s*==\s*\d", text), rel)
            self.assertIsNone(re.search(r"region_id\s*==\s*['\"]p\d", text), rel)


if __name__ == "__main__":
    unittest.main()
