"""Contracts for canonical pipeline progress and safe review bulk actions."""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path

from job_store import JobStatus, JobStore


ROOT = Path(__file__).resolve().parent
JS = ROOT / "static" / "tradutor_ui.js"
SHELL = ROOT / "ui" / "ui_shell.html"


class PipelineCanonicalUiContracts(unittest.TestCase):
    def test_pipeline_uses_canonical_stage_aliases_for_new_backend_stages(self) -> None:
        source = JS.read_text(encoding="utf-8")
        for marker in (
            "const stageAliases = {",
            "reading_text: 'ocr'",
            "translating: 'translate'",
            "redrawing: 'render'",
            "generating_pdf: 'pdf'",
            "quality_review: 'quality_review'",
            "review_required: 'quality_review'",
        ):
            self.assertIn(marker, source)

    def test_preview_is_rendered_from_canonical_pipeline_state(self) -> None:
        source = JS.read_text(encoding="utf-8")
        render_progress = source[
            source.index("function renderProgress("):source.index("function shouldRenderSourceReview")
        ]
        self.assertIn("renderPipelinePreview(state);", render_progress)
        self.assertNotIn("aguardando início", render_progress)
        self.assertIn("buildPipelineState(runtime, visibleProgress)", source)
        self.assertIn("appState.currentPipelineState = pipelineState", source)

    def test_stage_list_contains_quality_review_step_and_explicit_states(self) -> None:
        shell = SHELL.read_text(encoding="utf-8")
        source = JS.read_text(encoding="utf-8")
        self.assertIn('data-stage="quality_review"', shell)
        self.assertIn("item.dataset.state =", source)
        self.assertIn("'completed'", source)
        self.assertIn("'active'", source)
        self.assertIn("'future'", source)


class QualityReviewBulkContracts(unittest.TestCase):
    def test_frontend_exposes_safe_bulk_controls(self) -> None:
        shell = SHELL.read_text(encoding="utf-8")
        source = JS.read_text(encoding="utf-8")
        for marker in (
            "qualityReviewSelectAll",
            "acceptLowRiskReview",
            "acceptAllReview",
            "undoBulkReview",
            "globalAiReview",
            "/api/ui/quality-review/bulk-action",
            "Aceitar baixo risco",
            "Esta seleção inclui itens de alto risco",
        ):
            self.assertIn(marker, shell + source)

    def test_job_store_bulk_review_actions_are_atomic_and_restore_previous_values(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder) / "jobs.sqlite3")
            try:
                job_id = store.create_job(
                    source_url="https://example.test/chapter",
                    output_dir=str(Path(folder) / "output"),
                    command=["python"],
                    configuration={"job_type": "translation"},
                )
                store.transition(job_id, JobStatus.CLAIMING)
                store.transition(job_id, JobStatus.STARTING)
                store.transition(job_id, JobStatus.RUNNING)
                store.transition(job_id, JobStatus.REVIEW_REQUIRED)
                store.record_review_actions_bulk(job_id, {"p1:i1": "reviewed", "p1:i2": "preserved_original"})
                self.assertEqual(
                    store.review_actions(job_id),
                    {"p1:i1": "reviewed", "p1:i2": "preserved_original"},
                )
                store.record_review_actions_bulk(job_id, {"p1:i1": "pending", "p1:i2": "reviewed"})
                self.assertEqual(store.review_actions(job_id), {"p1:i2": "reviewed"})
            finally:
                store.close()

    def test_no_chapter_specific_translation_hardcode_in_runtime(self) -> None:
        runtime = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in (ROOT / "static").glob("*.js")
        ) + "\n" + (ROOT / "ui_bridge.py").read_text(encoding="utf-8", errors="ignore")
        forbidden = (
            "REAL COFFEE",
            "PRECINCT",
            "CODE BLACK",
            "I AM",
            "THE INFECTED",
            "THE NIGHTMARE SPELL",
        )
        for phrase in forbidden:
            self.assertNotIn(phrase, runtime)
        self.assertNotIn('if text ==', runtime)
        self.assertNotIn('if source_text ==', runtime)


if __name__ == "__main__":
    unittest.main()
