"""Hermetic contracts for the operational UI refinements."""

from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import _test_bootstrap  # noqa: F401

import ui_bridge
from job_store import JobStatus, JobStore
from process_options import build_background_process_options
from download_transport import preflight_browser_navigation
from chapter_source import SourceError
from ui_bridge import UiBridge


class OperationalBridge(UiBridge):
    def __init__(self, db: Path):
        self.history_store = mock.Mock()
        self.history = []
        self.profile = {}
        self.store = JobStore(db)
        self.history_revision = 1

    def close(self):
        self.store.close()


class QualityReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.output = self.tmp / "quality-output"
        self.output.mkdir()
        self.bridge = OperationalBridge(self.tmp / "jobs.sqlite3")
        report_path = self.output / "quality_report.json"
        image_path = self.output / "page_001.png"
        image_path.write_bytes(b"fake-image")
        report_path.write_text(json.dumps({
            "pages": [{
                "index": 1,
                "output_path": str(image_path),
                "translation_terminal_items": [{
                    "id": "BALAO_1", "classification": "speech", "text": "Hello",
                    "translation": "Ola", "manual_review_required": True,
                    "preserved_original": False,
                }],
            }],
        }), encoding="utf-8")
        self.job_id = self.bridge.store.create_job(
            source_url="https://example.test/chapter", output_dir=str(self.output),
            command=["python", "run_webtoon.py"],
            configuration={"job_type": "translation", "chapter_name": "Example"},
        )
        self.bridge.store.transition(self.job_id, JobStatus.CLAIMING, worker_id="w")
        self.bridge.store.transition(self.job_id, JobStatus.STARTING)
        self.bridge.store.transition(self.job_id, JobStatus.RUNNING)
        self.bridge.store.transition(
            self.job_id, JobStatus.REVIEW_REQUIRED,
            quality_report_path=str(report_path), reason_code="quality_review_required",
        )

    def tearDown(self):
        self.bridge.close()

    def test_quality_review_exposes_human_item_and_page_reference(self):
        review = self.bridge.quality_review(self.job_id)
        self.assertEqual(review["pending_count"], 1)
        item = review["items"][0]
        self.assertEqual(item["page"], 1)
        self.assertEqual(item["original"], "Hello")
        self.assertIn("confirma", item["reason"].lower())
        self.assertIn(f"/api/ui/quality-review/{self.job_id}/page/1", item["page_url"])

    def test_item_action_persists_and_confirmation_requires_no_pending_items(self):
        with self.assertRaisesRegex(ValueError, "quality_review_items_pending"):
            self.bridge.confirm_quality_review(self.job_id)
        review = self.bridge.quality_review_action(self.job_id, "p1:iBALAO_1", "preserved_original")
        self.assertEqual(review["pending_count"], 0)
        confirmed = self.bridge.confirm_quality_review(self.job_id)
        self.assertTrue(confirmed["confirmed"])
        self.assertEqual(self.bridge.store.review_actions(self.job_id)["p1:iBALAO_1"], "preserved_original")

    def test_rejection_requires_a_human_reason_before_recording_revision(self):
        with self.assertRaisesRegex(ValueError, "quality_review_reason_required"):
            self.bridge.quality_review_edit(
                self.job_id,
                "p1:iBALAO_1",
                expected_version=0,
                action="rejected",
                translation="Ola",
                reason="",
                actor_id="reviewer-a",
            )
        item = self.bridge.quality_review(self.job_id)["items"][0]
        self.assertEqual(item["state"], "pending")
        self.assertEqual(item["version"], 0)

    def test_visual_state_joins_on_region_id_not_balloon_id(self):
        # The report item's id is the balloon label (BALAO_1); the visual gate
        # keys regions by region_id (REGION_001). The panel must join on the
        # region_id, otherwise every per-item state is silently dropped.
        report_path = self.output / "quality_report.json"
        report_path.write_text(json.dumps({"pages": [{
            "index": 8,
            "output_path": str(self.output / "page_001.png"),
            "translation_terminal_items": [{
                "id": "BALAO_1", "region_id": "REGION_001", "classification": "speech",
                "text": "Hello", "translation": "Ola", "manual_review_required": True,
            }],
        }]}), encoding="utf-8")
        rev_root = self.output / "quality_revision"
        rev_dir = rev_root / "rev1"
        rev_dir.mkdir(parents=True)
        manifest_path = rev_dir / "revision_manifest.json"
        manifest_path.write_text(json.dumps({"revision_id": "rev1", "status": "review_required"}), encoding="utf-8")
        (rev_root / "latest_revision.json").write_text(
            json.dumps({"revision_id": "rev1", "manifest_path": str(manifest_path)}), encoding="utf-8")
        (rev_dir / "incremental_render_audit.json").write_text(json.dumps({"region_visual_states": {
            "p008:REGION_001": {"state": "applied", "reason_code": "",
                                "proposed_translation": "Ola revisado"}}}), encoding="utf-8")

        item = self.bridge.quality_review(self.job_id)["items"][0]
        self.assertEqual(item["region_id"], "p008:REGION_001")
        self.assertEqual(item["visual_state"], "applied")
        self.assertEqual(item["proposed_translation"], "Ola revisado")
        self.assertTrue(item["revision_linked"])

    def _write_revision(self, region_states, manifest_extra=None):
        rev_root = self.output / "quality_revision"
        rev_dir = rev_root / "rev1"
        rev_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = rev_dir / "revision_manifest.json"
        manifest = {"revision_id": "rev1", "status": "review_required"}
        manifest.update(manifest_extra or {})
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (rev_root / "latest_revision.json").write_text(
            json.dumps({"revision_id": "rev1", "manifest_path": str(manifest_path)}), encoding="utf-8")
        (rev_dir / "incremental_render_audit.json").write_text(
            json.dumps({"region_visual_states": region_states}), encoding="utf-8")

    def test_item_without_a_reviewed_region_is_report_only_not_stateless(self):
        # A preserved decorative/sfx region is flagged in the quality report but
        # never enters the revision. It must be labelled report_only, not left
        # with an empty visual state, and must not be revision-linked.
        report_path = self.output / "quality_report.json"
        report_path.write_text(json.dumps({"pages": [{
            "index": 3, "output_path": str(self.output / "page_001.png"),
            "suspicious_groups": [{
                "id": "BALAO_2", "region_id": "REGION_002", "classification": "decorative",
                "text": "KRAKOOM", "preserved_original": True,
                "quality_reasons": ["long_token_without_spaces"]}],
        }]}), encoding="utf-8")
        # Revision exists, but only reviewed a different region.
        self._write_revision({"p003:REGION_009": {"state": "applied"}})
        payload = self.bridge.quality_review(self.job_id)
        item = payload["items"][0]
        self.assertEqual(item["visual_state"], "report_only")
        self.assertFalse(item["revision_linked"])
        self.assertEqual(payload["report_only_count"], 1)

    def test_reviewed_pdf_is_exposed_from_the_manifest_not_a_glob(self):
        pdf = self.output / "chapter_reviewed_v8.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        # The manifest stores a project-relative path; the basename is canonical.
        self._write_revision({"p001:REGION_001": {"state": "applied"}}, manifest_extra={
            "reviewed_pdf_path": "output/whatever/chapter_reviewed_v8.pdf",
            "reviewed_pdf_sha256": "ABC123"})
        rp = self.bridge.quality_review(self.job_id)["reviewed_pdf"]
        self.assertIsNotNone(rp)
        self.assertEqual(rp["name"], "chapter_reviewed_v8.pdf")
        self.assertEqual(rp["sha256"], "ABC123")
        self.assertTrue(rp["path"].endswith("chapter_reviewed_v8.pdf"))
        self.assertTrue(Path(rp["path"]).is_file())

    def test_quality_review_falls_back_to_structured_json_next_to_html_report(self):
        html_path = self.output / "quality_report.html"
        html_path.write_text("<html>visual report</html>", encoding="utf-8")
        structured_path = html_path.with_suffix(".json")
        structured_path.write_text(json.dumps({
            "pages": [{
                "index": 2,
                "output_path": str(self.output / "page_001.png"),
                "translation_terminal_items": [{
                    "id": "FIXTURE_2", "classification": "speech",
                    "text": "Original fixture", "translation": "Tradução fixture",
                    "manual_review_required": True,
                }],
            }],
        }), encoding="utf-8")
        self.bridge.store.update_fields(self.job_id, quality_report_path=str(html_path))
        review = self.bridge.quality_review(self.job_id)
        self.assertEqual(review["pending_count"], 1)
        self.assertEqual(review["items"][0]["page"], 2)


class LifecycleAndProcessTests(unittest.TestCase):
    def test_source_preflight_stops_when_cancellation_is_requested(self):
        started = threading.Event()
        released = threading.Event()

        class Adapter:
            def validate_navigation_url(self, url):
                return None

            def validate_path(self, url):
                return None

        class Session:
            def get(self, *args, **kwargs):
                started.set()
                released.wait(10)
                raise RuntimeError("request released")

            def close(self):
                released.set()

        session = Session()
        with self.assertRaisesRegex(SourceError, "cancelled"):
            preflight_browser_navigation(
                Adapter(), "https://example.test/chapter", session=session,
                cancel_check=started.is_set,
            )

    def test_cancel_accepts_active_translation_job_id(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = OperationalBridge(Path(folder) / "jobs.sqlite3")
            try:
                job_id = bridge.store.create_job(
                    source_url="https://example.test/chapter", output_dir="", command=["python"],
                    configuration={"job_type": "translation"},
                )
                bridge.store.transition(job_id, JobStatus.CLAIMING, worker_id="w")
                bridge.store.transition(job_id, JobStatus.STARTING)
                bridge.store.transition(job_id, JobStatus.RUNNING)
                result = asyncio.run(bridge.cancel(job_id=job_id))
                row = bridge.store.get_job(job_id)
                self.assertTrue(result["ok"])
                self.assertEqual(row["status"], JobStatus.CANCELLED)
                self.assertEqual(row["reason_code"], "user_cancelled")
                self.assertIsNotNone(row["cancellation_requested_at"])
                self.assertIsNotNone(row["cancellation_completed_at"])
            finally:
                bridge.close()

    def test_cancel_timestamps_are_persisted(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder) / "jobs.sqlite3")
            job_id = store.create_job(
                source_url="https://example.test/chapter", output_dir="", command=["python"],
                configuration={"job_type": "translation"},
            )
            store.transition(job_id, JobStatus.CLAIMING, worker_id="w")
            store.transition(job_id, JobStatus.STARTING)
            store.transition(job_id, JobStatus.RUNNING)
            self.assertTrue(store.request_cancel(job_id))
            requested = store.get_job(job_id)
            self.assertIsNotNone(requested["cancellation_requested_at"])
            store.transition(job_id, JobStatus.CANCELLING)
            store.transition(job_id, JobStatus.CANCELLED, reason_code="user_cancelled")
            self.assertIsNotNone(store.get_job(job_id)["cancellation_completed_at"])
            store.close()

    def test_stage_clock_and_eta_inputs_are_persisted(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder) / "jobs.sqlite3")
            job_id = store.create_job(
                source_url="https://example.test/chapter", output_dir="", command=["python"],
                configuration={"job_type": "translation"},
            )
            store.transition(job_id, JobStatus.CLAIMING, worker_id="w")
            store.transition(job_id, JobStatus.STARTING)
            store.transition(job_id, JobStatus.RUNNING)
            store.update_progress(job_id, stage="downloading_pages", current=2, total=4)
            row = store.get_job(job_id)
            self.assertEqual(row["stage"], "downloading_pages")
            self.assertIsNotNone(row["stage_started_at"])
            store.close()

    def test_background_options_are_shell_free_and_session_is_explicit(self):
        options = build_background_process_options(new_session=True)
        self.assertFalse(options["shell"])
        self.assertTrue(options["stdin"] is not None)
        if ui_bridge.os.name != "nt":
            self.assertTrue(options["start_new_session"])


class FrontendOperationalContractsTests(unittest.TestCase):
    def test_frontend_exposes_review_cancel_eta_and_retry(self):
        root = Path(__file__).resolve().parent
        js = (root / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        shell = (root / "ui" / "ui_shell.html").read_text(encoding="utf-8")
        for marker in (
            "qualityReviewPanel", "/api/ui/quality-review/action", "confirmQualityReview",
            "runCancelAction", "runEtaHuman", "runRetryAction", "qualityReviewFilter",
        ):
            self.assertIn(marker, js + shell, marker)

    def test_frontend_uses_structured_human_messages_not_tracebacks(self):
        js = (Path(__file__).resolve().parent / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.assertIn("worker_unavailable", js)
        self.assertIn("user_cancelled", js)
        self.assertNotIn("traceback", js[js.index("function renderRunStatus"):])


class TranslatedHistoryReviewEntry(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent
        self.js = (root / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.shell = (root / "ui" / "ui_shell.html").read_text(encoding="utf-8")

    def test_history_card_exposes_explicit_review_action(self):
        # A visible text action (not just another icon) with accessible labelling.
        self.assertIn('data-action="review"', self.js)
        self.assertIn("reviewAction(record)", self.js)
        self.assertIn("Revisar OCR, tradução e qualidade deste capítulo", self.js)
        for label in ("Ver revisão", "Continuar revisão", "Revisar"):
            self.assertIn(label, self.js)

    def test_review_entry_adopts_job_and_run_not_runtime_latest(self):
        # The entry must bind exactly the clicked card's job_id + run_id.
        self.assertIn("function reviewIdentity", self.js)
        # The canonical job id only; a presentation/history row id is never a source job.
        self.assertIn("String(record.job_id || '').toLowerCase()", self.js)
        self.assertNotIn("record.job_id || record.id", self.js)
        self.assertIn("record.run_id", self.js)
        self.assertIn("function openChapterReview", self.js)
        self.assertIn("url.searchParams.set('view', 'review')", self.js)
        self.assertIn("url.searchParams.set('job_id', jobId)", self.js)
        self.assertIn("url.searchParams.set('run_id', runId)", self.js)
        # It must not adopt a chapter by falling back to the global latest runtime.
        self.assertNotIn("runtime.latest", self.js[self.js.index("function openChapterReview"):])

    def test_review_mode_opens_panel_and_hides_start(self):
        self.assertIn("function applyReviewMode", self.js)
        self.assertIn("if (start) start.hidden = true", self.js)
        self.assertIn("panel.scrollIntoView", self.js)
        self.assertIn('reviewModeBanner', self.shell)
        self.assertIn('reviewModeExit', self.shell)

    def test_reload_restores_review_selection(self):
        self.assertIn("function restoreReviewModeFromUrl", self.js)
        self.assertIn("params.get('view') !== 'review'", self.js)
        self.assertIn("restoreReviewModeFromUrl()", self.js)
        # A background poll must not hide the panel while review_mode owns it.
        self.assertIn("!appState.reviewMode && $('#qualityReviewPanel')", self.js)

    def test_review_action_stays_separate_from_compare_and_report(self):
        # Compare/report/pdf keep opening only their own artifact.
        self.assertIn("['pdf','folder','report','compare','context'].includes", self.js)
        self.assertIn("if (button.dataset.action === 'review')", self.js)

    def test_history_actions_use_stable_document_delegation(self):
        # Authentication can replace authenticated surfaces after this script
        # initially binds. History actions must therefore be delegated from a
        # stable ancestor instead of the current #histList node.
        self.assertIn("async function handleHistoryAction", self.js)
        self.assertIn(
            "document.addEventListener('click', handleHistoryAction, {capture: true})",
            self.js,
        )
        self.assertNotIn(
            "$('#histList')?.addEventListener('click', async event =>",
            self.js,
        )

    def test_history_folder_restores_focus_after_dynamic_render(self):
        self.assertIn(
            "const replacement = list?.querySelector("
            "`.cf-header[data-folder=\"${CSS.escape(key)}\"]`)",
            self.js,
        )
        self.assertIn(
            "window.setTimeout(() => replacement?.focus(), 0)",
            self.js,
        )

    def test_closing_review_restores_focus_to_its_history_action(self):
        self.assertIn('data-review-job="${escapeAttr(identity.jobId)}"', self.js)
        self.assertIn("const returnJobId = appState.reviewMode?.jobId || ''", self.js)
        self.assertIn(
            "`.hm-review-action[data-review-job=\"${CSS.escape(returnJobId)}\"]`",
            self.js,
        )
        self.assertIn("window.setTimeout(() => returnAction?.focus(), 0)", self.js)

    def test_pending_review_filter_uses_terminal_state_not_visual_state(self):
        review_start = self.js.index("function renderQualityReview(review)")
        review_end = self.js.index("function visibleQualityReviewKeys", review_start)
        review = self.js[review_start:review_end]
        terminal_pending = "if (filter === 'pending') return item.state === 'pending'"
        visual_filter = "if (VISUAL_STATE_LABELS[filter])"
        self.assertIn(terminal_pending, review)
        self.assertIn(visual_filter, review)
        self.assertLess(review.index(terminal_pending), review.index(visual_filter))

    def test_rejection_without_reason_is_blocked_before_request(self):
        action_start = self.js.index("async function qualityReviewAction(event)")
        action_end = self.js.index("const button = event.target.closest('[data-review-action]')", action_start)
        action = self.js[action_start:action_end]
        validation = "if (action === 'rejected' && !reason)"
        request = "await api('/api/ui/quality-review/edit'"
        self.assertIn(validation, action)
        self.assertIn("Informe um motivo para rejeitar esta tradução.", action)
        self.assertLess(action.index(validation), action.index(request))

    def test_developer_mode_toggle_is_a_real_ui_option(self):
        self.assertIn('id="developerModeToggle"', self.shell)
        self.assertIn("developerModeToggle", self.js)
        self.assertIn("tradutorDeveloperMode", self.js)
        self.assertIn("updateQualityReviewDeveloperActions()", self.js)


class PageRevisionBridgeTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.tmp = Path(tempfile.mkdtemp())
        self.output = self.tmp / "output"
        self.output.mkdir()
        self.bridge = OperationalBridge(self.tmp / "jobs.sqlite3")
        image = self.output / "page_001.jpg"
        Image.new("RGB", (240, 320), "white").save(image, "JPEG")
        progress = {"pdf_path": str(self.output / "c.pdf"), "pages": [{
            "index": 1, "sequence_index": 1, "image_path": str(image), "output_path": str(image),
            "debug_data": {"image_path": str(image), "items": [
                {"id": "BALAO_1", "region_id": "REGION_001", "clean_text": "HELLO",
                 "translation": "Olá.", "classification": "speech", "confidence": 0.9,
                 "bounding_box": [20, 20, 160, 80], "redrawn": True},
            ]}}]}
        (self.output / "progress.json").write_text(json.dumps(progress), encoding="utf-8")
        (self.output / "quality_report.json").write_text(json.dumps({"pages": [{"index": 1}]}), encoding="utf-8")
        self.job_id = self.bridge.store.create_job(
            source_url="https://example.test/c", output_dir=str(self.output),
            command=["python", "run.py"], configuration={"job_type": "translation"})
        self.run_id = self.bridge.store.get_job(self.job_id)["run_id"]

    def tearDown(self):
        self.bridge.close()

    def test_wrong_run_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "run_id_mismatch"):
            self.bridge.start_page_revision(self.job_id, "not-the-run", 1)

    def test_unknown_job_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "job_not_found"):
            self.bridge.list_page_revision_regions("nope", "", 1)

    def test_start_is_cache_only_and_flags_authorization(self):
        manifest = self.bridge.start_page_revision(self.job_id, self.run_id, 1)
        self.assertEqual(manifest["status"], "awaiting_provider_authorization")
        self.assertEqual(manifest["requests_needed"], 1)
        self.assertEqual(list(self.output.glob("*_reviewed_*.pdf")), [])
        # Status round-trips with the same page_revision_id (F5 restore).
        again = self.bridge.page_revision_status(self.job_id, self.run_id, manifest["page_revision_id"])
        self.assertEqual(again["page_revision_id"], manifest["page_revision_id"])

    def test_page_revision_from_another_job_is_rejected(self):
        manifest = self.bridge.start_page_revision(self.job_id, self.run_id, 1)
        other = self.bridge.store.create_job(
            source_url="https://example.test/other", output_dir=str(self.output),
            command=["python", "run.py"], configuration={"job_type": "translation"})
        other_run = self.bridge.store.get_job(other)["run_id"]
        with self.assertRaisesRegex(ValueError, "page_revision_job_mismatch"):
            self.bridge.page_revision_status(other, other_run, manifest["page_revision_id"])

    def test_regions_manual_and_decision_round_trip(self):
        listing = self.bridge.list_page_revision_regions(self.job_id, self.run_id, 1)
        self.assertEqual(listing["requests_needed"], 1)
        manifest = self.bridge.start_page_revision(self.job_id, self.run_id, 1)
        pr = manifest["page_revision_id"]
        entry = self.bridge.add_page_revision_manual_region(
            self.job_id, self.run_id, pr, [10, 10, 60, 40], source_text="HI", region_type="speech")
        self.assertTrue(entry["region_id"].endswith("MANUAL_001"))
        rejected = self.bridge.decide_page_revision(self.job_id, self.run_id, pr, "rejected")
        self.assertEqual(rejected["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
