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

    def test_dialogs_close_on_escape_and_sync_state(self):
        # Escape must close both panels and stop the URL advertising them.
        self.assertIn("event.key === 'Escape'", self.js)
        self.assertIn("closeLinguisticAudit()", self.js)
        self.assertIn("addEventListener('close'", self.js)

    def test_rejected_draft_can_still_be_discarded(self):
        # A rejected draft still has files on disk, so discard stays reachable
        # while approve is disabled.
        self.assertIn("['draft_ready', 'rejected'].includes(status)", self.js)
        self.assertIn("approve.disabled = status !== 'draft_ready'", self.js)

    def test_triage_modes_and_bulk_actions_exist(self):
        for label in ("FILA DE TRIAGEM", "CONJUNTO MÍNIMO PARA IA",
                      "MARCAR COMO TRADUZÍVEL", "MARCAR COMO PRESERVAR",
                      "MARCAR COMO OCR INVÁLIDO", "EXIGIR REVISÃO HUMANA", "REMOVER DECISÃO"):
            self.assertIn(label, self.shell, label)
        for endpoint in ("/api/ui/audit/triage", "/api/ui/audit/decision/bulk",
                         "/api/ui/audit/provider-set", "/api/ui/audit/provider-authorization"):
            self.assertIn(endpoint, self.js, endpoint)

    def test_triage_shows_priority_reason_and_gate(self):
        self.assertIn("motivo da prioridade", self.js)
        self.assertIn("triage_reasons", self.js)
        self.assertIn("linguistic_gate", self.js)

    def test_authorization_button_states_no_external_call_is_made(self):
        self.assertIn("SOLICITAR AUTORIZAÇÃO PARA REVISÃO COM IA", self.js)
        self.assertIn("Nenhuma chamada externa", self.js)
        # confirmation is explicit, never a pre-checked box
        self.assertIn("confirm: true", self.js)
        self.assertNotIn("checked disabled", self.js)

    def test_audit_mode_is_restored_from_the_url(self):
        self.assertIn("audit_mode", self.js)

    # --- editorial queues (BLOCO 6A.1) ------------------------------------
    def test_editorial_modes_and_endpoints_exist(self):
        for label in ("CANDIDATOS A OCR INVÁLIDO", "DECISÕES EDITORIAIS PENDENTES"):
            self.assertIn(label, self.shell, label)
        for endpoint in ("/api/ui/audit/ocr-invalid-candidates", "/api/ui/audit/editorial-pending"):
            self.assertIn(endpoint, self.js, endpoint)

    def test_the_three_counters_are_separate(self):
        for label in ("OCR DIRECIONADO FUTURO", "DECISÃO EDITORIAL PENDENTE",
                      "PRONTO PARA REVISÃO COM IA"):
            self.assertIn(label, self.js, label)
        # They are read from the backend, never summed into one number.
        self.assertIn("counters.targeted_ocr_pending", self.js)
        self.assertIn("counters.awaiting_editorial_decision", self.js)
        self.assertIn("counters.ready_for_ai_review", self.js)

    def test_bulk_confirmation_states_what_does_not_change(self):
        self.assertIn("auditConfirmDialog", self.shell)
        self.assertIn("retiradas da fila de tradução e encaminhadas para", self.js)
        self.assertIn("Nenhum PDF ou tradução será alterado.", self.js)
        # The bulk path confirms through the in-page dialog, never a browser prompt.
        start = self.js.index("async function applyBulkDecision")
        body = self.js[start:self.js.index("\n  $$('[data-audit-mode]')", start)]
        self.assertIn("await auditConfirm(", body)
        for banned in ("window.confirm(", "window.prompt("):
            self.assertNotIn(banned, body, banned)

    def test_authorization_is_visibly_blocked_while_decisions_are_open(self):
        self.assertIn("awaiting_editorial_count", self.js)
        self.assertIn("Bloqueado: há regiões aguardando decisão editorial.", self.js)
        self.assertIn("blocked ? ' disabled aria-disabled=\"true\"", self.js)

    def test_candidates_are_proposed_not_marked(self):
        # The panel shows the detector's evidence and both sides of it; the
        # verdict is a button a human presses.
        self.assertIn("auto_markable", self.js)
        self.assertIn("positive_evidence", self.js)
        self.assertIn("A confirmação continua sendo sua.", self.js)

    def test_editorial_queue_explains_why_each_region_is_open(self):
        self.assertIn("editorial_reasons", self.js)
        self.assertIn("pendente porque", self.js)

    def test_new_modes_survive_f5(self):
        self.assertIn("AUDIT_MODES", self.js)
        for mode in ("'ocr'", "'editorial'"):
            self.assertIn(mode, self.js, mode)

    def test_taxonomy_categories_are_not_page_conditioned(self):
        # No production module keys a decision on a literal page/region number.
        for rel in ("region_taxonomy.py", "linguistic_audit.py", "audit_registry.py",
                    "audit_decisions.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"page_number\s*==\s*\d", text), rel)
            self.assertIsNone(re.search(r"region_id\s*==\s*['\"]p\d", text), rel)


if __name__ == "__main__":
    unittest.main()
