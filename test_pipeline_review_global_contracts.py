"""Contracts for canonical pipeline progress and safe review bulk actions."""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path

from chapter_quality_revision import ChapterQualityRevision, ContextualNvidiaReviewer
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


class FakeContextualReviewer:
    model = "fake-contextual-reviewer"

    def __init__(self) -> None:
        self.requests = 0

    def review_batch(self, records, glossary):
        self.requests += 1
        result = []
        for record in reversed(records):
            result.append({
                "region_id": record["region_id"],
                "action": "manual_review" if record["region_id"].endswith("REGION_002") else "keep",
                "revised_translation": record["current_translation"],
                "reason_code": "fake_review",
                "confidence": 0.99,
                "risk": "high" if record["region_id"].endswith("REGION_002") else "low",
                "terminology": [],
            })
        return result


class FullChapterQualityRevisionContracts(unittest.TestCase):
    def _fixture_output(self, root: Path) -> Path:
        from PIL import Image

        output = root / "output"
        output.mkdir()
        pdf = output / "chapter.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% fake hermetic pdf\n" + b"0" * 2048)
        image = output / "page_001.jpg"
        Image.new("RGB", (240, 320), "white").save(image, "JPEG")
        progress = {
            "pdf_path": str(pdf),
            "pages": [
                {
                    "index": 1,
                    "sequence_index": 1,
                    "image_path": str(image),
                    "output_path": str(image),
                    "debug_data": {
                        "image_path": str(image),
                        "items": [
                            {
                                "id": "BALAO_1",
                                "region_id": "REGION_001",
                                "clean_text": "HELLO THERE",
                                "translation": "Olá.",
                                "classification": "speech",
                                "confidence": 0.99,
                                "bounding_box": [20, 20, 160, 80],
                                "sent_to_nvidia": True,
                                "redrawn": True,
                                "translation_final_state": "translated",
                            },
                            {
                                "id": "BALAO_2",
                                "region_id": "REGION_002",
                                "clean_text": "WHAT IS THIS?",
                                "translation": "WHAT IS THIS?",
                                "classification": "speech",
                                "confidence": 0.91,
                                "bounding_box": [20, 100, 180, 160],
                                "sent_to_nvidia": True,
                                "redrawn": False,
                                "manual_review_required": True,
                                "translation_final_state": "manual_review",
                            },
                            {
                                "id": "SFX_1",
                                "region_id": "REGION_003",
                                "clean_text": "BANG",
                                "translation": "BANG",
                                "classification": "sfx",
                                "confidence": 0.95,
                                "bounding_box": [20, 180, 180, 220],
                                "preserved_original": True,
                            },
                        ],
                    },
                }
            ],
        }
        quality = {
            "summary": {
                "pdf_path": str(pdf),
                "quality_validation": {
                    "smart_split_details": [
                        {"page": 1, "requires_review": True, "safe_band": False, "band_score": 1.2}
                    ]
                },
            },
            "pages": [{"index": 1}],
        }
        (output / "progress.json").write_text(json.dumps(progress), encoding="utf-8")
        (output / "quality_report.json").write_text(json.dumps(quality), encoding="utf-8")
        return output

    def test_revision_run_uses_existing_artifacts_and_preserves_original_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._fixture_output(Path(folder))
            original = output / "chapter.pdf"
            before = original.read_bytes()
            revision = ChapterQualityRevision(
                output,
                job_id="job-1",
                run_id="run-1",
                reviewer_factory=FakeContextualReviewer,
            )
            status = revision.start()
            self.assertEqual(status["parent_job_id"], "job-1")
            self.assertEqual(status["parent_run_id"], "run-1")
            self.assertEqual(status["source_pdf_path"], str(original))
            self.assertEqual(original.read_bytes(), before)
            self.assertTrue(Path(status["reviewed_pdf_path"]).is_file())
            self.assertNotEqual(Path(status["reviewed_pdf_path"]).name, original.name)
            self.assertEqual(status["total_pages"], 1)
            self.assertEqual(status["total_regions"], 3)
            self.assertEqual(status["publication_created"], False)

    def test_revision_filters_progress_pages_to_quality_report_pages(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._fixture_output(Path(folder))
            progress_path = output / "progress.json"
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            extra = json.loads(json.dumps(progress["pages"][0]))
            extra["index"] = 2
            extra["sequence_index"] = 2
            extra["debug_data"]["items"][0]["region_id"] = "REGION_EXTRA"
            progress["pages"].append(extra)
            progress_path.write_text(json.dumps(progress), encoding="utf-8")
            status = ChapterQualityRevision(
                output,
                job_id="job-1",
                run_id="run-1",
                reviewer_factory=FakeContextualReviewer,
            ).start()
            self.assertEqual(status["total_pages"], 1)
            self.assertEqual(status["total_regions"], 3)

    def test_revision_validates_region_ids_and_keeps_high_risk_manual(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._fixture_output(Path(folder))
            status = ChapterQualityRevision(
                output,
                job_id="job-1",
                run_id="run-1",
                reviewer_factory=FakeContextualReviewer,
            ).start()
            latest = json.loads((output / "quality_revision" / "latest_revision.json").read_text(encoding="utf-8"))
            contextual = json.loads(Path(latest["manifest_path"]).with_name("contextual_translation_review.json").read_text(encoding="utf-8"))
            by_id = {item["region_id"]: item for item in contextual["reviews"]}
            self.assertIn("p001:REGION_001", by_id)
            self.assertEqual(by_id["p001:REGION_002"]["action"], "manual_review")
            self.assertEqual(status["safe_changes_applied"], 0)
            self.assertEqual(status["status"], "review_required")

    def test_frontend_exposes_full_chapter_revision_action(self) -> None:
        shell = SHELL.read_text(encoding="utf-8")
        source = JS.read_text(encoding="utf-8")
        self.assertIn("REVISAR CAPÍTULO INTEIRO", shell)
        self.assertIn("/api/ui/quality-review/revision/start", source)
        self.assertIn("pollQualityRevisionStatus", source)
        self.assertIn("qualityRevisionStatus", shell)

    def test_nvidia_review_accepts_region_id_keyed_json(self) -> None:
        parsed = {
            "p001:REGION_002": {
                "action": "manual_review",
                "revised_translation": "",
                "reason_code": "needs_context",
                "confidence": 0.2,
                "risk": "high",
            }
        }
        items = ContextualNvidiaReviewer.review_items_from_parsed(parsed)
        self.assertEqual(items[0]["region_id"], "p001:REGION_002")
        self.assertEqual(items[0]["action"], "manual_review")

    def test_nvidia_review_translation_only_mapping_is_fail_closed(self) -> None:
        items = ContextualNvidiaReviewer.review_items_from_parsed({
            "p001:REGION_002": "Tradução sem contrato estruturado."
        })
        self.assertEqual(items[0]["region_id"], "p001:REGION_002")
        self.assertEqual(items[0]["action"], "manual_review")
        self.assertEqual(items[0]["risk"], "high")
        self.assertEqual(items[0]["reason_code"], "non_contract_translation_only_response")


if __name__ == "__main__":
    unittest.main()
