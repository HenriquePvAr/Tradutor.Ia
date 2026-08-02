import _test_bootstrap  # noqa: F401

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import ui_bridge
from job_store import JobStatus, JobStore
from ui_bridge import UiBridge


class ReviewRerunFixture:
    def __init__(self, root: Path):
        self.output = root / "output"
        revision = self.output / "quality_revision" / "rev-1"
        revision.mkdir(parents=True)
        (self.output / "pages").mkdir()
        progress = {
            "pages": [
                {
                    "index": 1,
                    "output_path": str(self.output / "pages" / "page_001.png"),
                    "debug_data": {
                        "items": [
                            {
                                "id": "REGION_001",
                                "region_id": "REGION_001",
                                "classification": "speech",
                                "clean_text": "HELLO THERE",
                                "translation": "Olá.",
                                "bounding_box": [10, 10, 80, 40],
                            },
                            {
                                "id": "REGION_002",
                                "region_id": "REGION_002",
                                "classification": "speech",
                                "clean_text": "SORRY FOR HIM.",
                                "translation": "SORRY FOR HIM.",
                                "bounding_box": [20, 60, 100, 40],
                            },
                            {
                                "id": "REGION_003",
                                "region_id": "REGION_003",
                                "classification": "speech",
                                "clean_text": "THANK YOU",
                                "translation": "Obrigado.",
                                "bounding_box": [20, 110, 100, 40],
                            },
                        ]
                    },
                }
            ]
        }
        (self.output / "progress.json").write_text(json.dumps(progress), encoding="utf-8")
        (self.output / "quality_report.json").write_text(
            json.dumps({"pages": [{"index": 1}]}), encoding="utf-8"
        )
        (self.output / "quality_revision" / "latest_revision.json").write_text(
            json.dumps({"revision_id": "rev-1", "manifest_path": str(revision / "revision_manifest.json")}),
            encoding="utf-8",
        )
        (revision / "revision_manifest.json").write_text(
            json.dumps({"revision_id": "rev-1", "status": "review_required"}), encoding="utf-8"
        )
        (revision / "contextual_translation_review.json").write_text(
            json.dumps(
                {
                    "reviews": [
                        {
                            "region_id": "p001:REGION_001",
                            "action": "rewrite",
                            "revised_translation": "Olá!",
                            "reason_code": "style",
                        },
                        {
                            "region_id": "p001:REGION_002",
                            "action": "manual_review",
                            "revised_translation": "SORRY FOR HIM.",
                            "reason_code": "unsafe_rewrite_candidate",
                        },
                        {
                            "region_id": "p001:REGION_003",
                            "action": "keep",
                            "revised_translation": "Obrigado.",
                            "reason_code": "none",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        (revision / "incremental_render_audit.json").write_text(
            json.dumps(
                {
                    "region_visual_states": {
                        "p001:REGION_001": {
                            "state": "rejected_visual_regression",
                            "reason_code": "mask_precision_low",
                        },
                        "p001:REGION_002": {
                            "state": "manual_review",
                            "reason_code": "residual_source_language",
                        },
                        "p001:REGION_003": {"state": "unchanged", "reason_code": "none"},
                    }
                }
            ),
            encoding="utf-8",
        )


class PendingRegionPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = ReviewRerunFixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_targets_only_visual_rejection_and_untranslated_source(self):
        from review_rerun import build_pending_region_plan

        plan = build_pending_region_plan(self.fixture.output)
        self.assertEqual(plan["region_count"], 2)
        self.assertEqual(plan["page_count"], 1)
        by_id = {item["region_id"]: item for item in plan["targets"]}
        self.assertEqual(by_id["p001:REGION_001"]["work_kind"], "reconstruction_only")
        self.assertFalse(by_id["p001:REGION_001"]["requires_provider"])
        self.assertEqual(by_id["p001:REGION_002"]["work_kind"], "translation_and_reconstruction")
        self.assertTrue(by_id["p001:REGION_002"]["requires_provider"])
        self.assertNotIn("p001:REGION_003", by_id)


class ReviewRerunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = ReviewRerunFixture(self.root)
        self.store = JobStore(self.root / "jobs.sqlite3")
        self.parent_id = self.store.create_job(
            source_url="",
            output_dir=str(self.fixture.output),
            command=[],
            configuration={"job_type": "translation", "community_owner_id": "owner-a"},
            initial_status=JobStatus.QUEUED,
        )
        self.store.transition(self.parent_id, JobStatus.CANCELLED)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_child_run_is_new_queued_run_and_duplicate_start_is_idempotent(self):
        first = self.store.create_review_rerun(
            self.parent_id,
            targets=[{"region_id": "p001:REGION_001", "page": 1, "requires_provider": False}],
            allow_provider=False,
        )
        second = self.store.create_review_rerun(
            self.parent_id,
            targets=[{"region_id": "p001:REGION_001", "page": 1, "requires_provider": False}],
            allow_provider=False,
        )
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["run_id"], self.store.get_job(self.parent_id)["run_id"])
        self.assertEqual(first["status"], JobStatus.QUEUED)
        self.assertEqual(first["operation_kind"], "review_rerun")
        self.assertEqual(first["parent_job_id"], self.parent_id)

    def test_provider_targets_require_explicit_authorization(self):
        with self.assertRaisesRegex(ValueError, "provider_authorization_required"):
            self.store.create_review_rerun(
                self.parent_id,
                targets=[{"region_id": "p001:REGION_002", "page": 1, "requires_provider": True}],
                allow_provider=False,
            )


class WorkerReviewRerunDispatchTests(unittest.TestCase):
    def test_review_rerun_has_a_dedicated_runner(self):
        from worker_service import Worker

        self.assertEqual(Worker._RUNNERS["review_rerun"], "review_rerun_runner.py")

    def test_review_rerun_bypasses_chapter_source_analysis(self):
        from worker_service import Worker

        worker = object.__new__(Worker)
        worker.store = unittest.mock.Mock()
        job = {
            "id": "a" * 32,
            "configuration": {"job_type": "review_rerun"},
            "source_type": "url",
            "source_url": "",
        }
        worker.store.get_job.return_value = job
        worker._analyze_source = unittest.mock.Mock(
            side_effect=AssertionError("source analysis must not run")
        )
        self.assertIs(worker._prepare_source(job), job)
        worker._analyze_source.assert_not_called()

    def test_ui_runtime_recognizes_the_dedicated_review_rerun_process(self):
        job = {
            "id": "b" * 32,
            "operation_kind": "review_rerun",
            "runner_pid": 1234,
            "runner_create_time": 5678.0,
        }
        with patch("process_tree.is_alive", return_value=True) as is_alive:
            self.assertTrue(ui_bridge._runner_still_alive(job))
        is_alive.assert_called_once_with(
            1234,
            create_time=5678.0,
            substrings=["review_rerun_runner.py", "b" * 32],
        )


class ReviewRerunBridgeAndUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = ReviewRerunFixture(self.root)
        self.env = patch.dict(
            "os.environ",
            {
                "APP_ENV": "test",
                "ALLOW_LOCAL_TEST_IDENTITIES": "1",
                "TRADUTOR_TEST_RUNTIME_ROOT": str(self.root),
            },
        )
        self.env.start()
        self.bridge = UiBridge()
        self.parent_id = self.bridge.store.create_job(
            source_url="",
            output_dir=str(self.fixture.output),
            command=[],
            configuration={
                "job_type": "translation",
                "community_owner_id": "owner-a",
                "chapter_name": "Fixture chapter",
            },
        )
        self.bridge.store.transition(self.parent_id, JobStatus.CANCELLED)

    def tearDown(self):
        self.bridge.close()
        self.env.stop()
        self.temp.cleanup()

    def test_plan_and_start_are_owner_scoped_and_queue_child_operation(self):
        plan = self.bridge.review_rerun_plan_for_owner("owner-a", self.parent_id)
        self.assertEqual(plan["region_count"], 2)
        with self.assertRaisesRegex(ValueError, "job_not_found"):
            self.bridge.review_rerun_plan_for_owner("owner-b", self.parent_id)
        started = self.bridge.start_review_rerun_for_owner(
            "owner-a", self.parent_id, allow_provider=True
        )
        self.assertEqual(started["operation_kind"], "review_rerun")
        state = self.bridge.runtime_state_for_owner("owner-a")
        self.assertEqual(len(state["queue"]), 1)
        self.assertEqual(state["queue"][0]["operation_label"], "Rerun de pendências")
        self.assertEqual(state["queue"][0]["parent_job_id"], self.parent_id)

    def test_reconstruction_only_mode_never_requires_provider_authorization(self):
        started = self.bridge.start_review_rerun_for_owner(
            "owner-a", self.parent_id, allow_provider=False, modes=["reconstruction"]
        )
        child = self.bridge.store.get_job(started["id"])
        targets = (child.get("configuration") or {}).get("targets") or []
        self.assertTrue(targets)
        self.assertTrue(all(item.get("requires_provider") is False for item in targets))
        self.assertEqual(started["provider_requests_planned"], 0)

    def test_public_plan_does_not_expose_revision_filesystem_path(self):
        plan = self.bridge.review_rerun_plan_for_owner("owner-a", self.parent_id)
        self.assertNotIn("revision_root", plan)
        self.assertNotIn(str(self.root), json.dumps(plan))

    def test_queued_child_can_be_cancelled_without_starting_runner(self):
        import asyncio

        started = self.bridge.start_review_rerun_for_owner(
            "owner-a", self.parent_id, allow_provider=False, modes=["reconstruction"]
        )
        result = asyncio.run(
            self.bridge.cancel_for_owner("owner-a", job_id=started["id"])
        )
        self.assertEqual(result["status"], JobStatus.CANCELLED)
        self.assertEqual(
            self.bridge.store.get_job(started["id"])["status"], JobStatus.CANCELLED
        )

    def test_review_rerun_ui_has_confirmation_and_real_handlers(self):
        root = Path(__file__).resolve().parent
        shell = (root / "ui" / "ui_shell.html").read_text(encoding="utf-8")
        js = (root / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.assertIn('id="rerunPendingReview"', shell)
        self.assertIn('id="reviewRerunConfirm"', shell)
        self.assertIn('id="cancelReviewRerunAction"', shell)
        self.assertIn("/api/ui/review-rerun/plan", js)
        self.assertIn("/api/ui/review-rerun/start", js)
        self.assertIn("/api/ui/review-rerun/status", js)
        self.assertIn("/api/ui/review-rerun/cancel", js)
        self.assertIn("$('#rerunPendingReview')?.addEventListener", js)
        self.assertIn("$('#reviewRerunApply')?.addEventListener", js)
        self.assertIn("item.operation_kind === 'review_rerun'", js)
        self.assertIn("review_rerun: 'quality_review'", js)
        self.assertIn("mode !== 'reconstruction' && Number(plan.provider_required || 0) > 0", js)
        self.assertIn("$('#cancelReviewRerunAction')?.addEventListener", js)
        self.assertNotIn("pollReviewRerunStatus(jobId, {once: true})", js)
        self.assertIn("String(button.dataset.reviewJob || '').toLowerCase()", js)
        self.assertIn("String(item.job_id || '').toLowerCase() === reviewJobId", js)
        self.assertIn("Aguardando decisão", js)
        self.assertIn("Decisões registradas", js)

    def test_public_rerun_status_has_authoritative_lifecycle_and_accounting(self):
        started = self.bridge.start_review_rerun_for_owner(
            "owner-a", self.parent_id, allow_provider=False, modes=["reconstruction"]
        )
        self.bridge.store.update_progress(
            started["id"],
            stage="review_rerun",
            current=1,
            total=1,
            message="Página 1 · região p001:REGION_001 · validando residual",
            counter_stage="review_rerun",
        )
        record = self.bridge.review_rerun_status_for_owner("owner-a", started["id"])
        self.assertEqual(record["lifecycle_state"], "queued")
        self.assertEqual(record["progress_current"], 1)
        self.assertEqual(record["progress_total"], 1)
        self.assertEqual(record["rerun_counts"], {
            "targets": 1,
            "processed": 1,
            "approved": 0,
            "rejected": 0,
            "remaining": 0,
            "cancelled": 0,
        })
        self.assertTrue(record["rerun_accounting_closed"])
        self.assertEqual(record["queue_position"], 1)

    def test_claimed_rerun_exposes_worker_lease_and_heartbeat(self):
        started = self.bridge.start_review_rerun_for_owner(
            "owner-a", self.parent_id, allow_provider=False, modes=["reconstruction"]
        )
        self.bridge.store.register_worker("worker-live", 4321, create_time=12.0)
        claimed = self.bridge.store.claim_next_job("worker-live", 4321, worker_create_time=12.0)
        self.assertEqual(claimed["id"], started["id"])
        self.bridge.store.worker_heartbeat("worker-live")
        record = self.bridge.review_rerun_status_for_owner("owner-a", started["id"])
        self.assertEqual(record["lifecycle_state"], "claimed")
        self.assertEqual(record["worker_lease"], "active")
        self.assertLessEqual(record["worker_heartbeat_age_seconds"], 1.0)
        self.assertIsNone(record["queue_position"])

    def test_expired_cancellation_with_missing_runner_is_reconciled(self):
        started = self.bridge.start_review_rerun_for_owner(
            "owner-a", self.parent_id, allow_provider=False, modes=["reconstruction"]
        )
        claimed = self.bridge.store.claim_next_job("worker-a", 4242)
        self.assertEqual(claimed["id"], started["id"])
        self.bridge.store.transition(started["id"], JobStatus.STARTING, expected_worker="worker-a")
        self.bridge.store.transition(started["id"], JobStatus.RUNNING, expected_worker="worker-a")
        self.bridge.store.request_cancel(started["id"])
        self.bridge.store.transition(started["id"], JobStatus.CANCELLING, expected_worker="worker-a")
        self.bridge.store.update_fields(
            started["id"],
            cancellation_requested_at=time.time() - 60,
            runner_pid=999999,
            runner_create_time=1.0,
        )
        with patch("ui_bridge._runner_still_alive", return_value=False):
            record = self.bridge.review_rerun_status_for_owner("owner-a", started["id"])
        self.assertEqual(record["status"], JobStatus.CANCELLED)
        self.assertEqual(record["lifecycle_state"], "cancelled")
        self.assertEqual(record["rerun_counts"]["cancelled"], 1)
        self.assertTrue(record["rerun_accounting_closed"])

    def test_chapter_and_decision_counts_are_disjoint_and_explicit(self):
        self.bridge.store.update_fields(
            self.parent_id,
            status=JobStatus.REVIEW_REQUIRED,
            quality_report_path=str(self.fixture.output / "quality_report.json"),
        )
        payload = self.bridge.quality_review(self.parent_id)
        self.assertIsNotNone(payload)
        chapter = payload["chapter_counts"]
        self.assertEqual(chapter["total"], 3)
        self.assertEqual(
            chapter["total"],
            chapter["approved"] + chapter["pending"] + chapter["rejected"]
            + chapter["unchanged"] + chapter["manual"],
        )
        self.assertTrue(chapter["accounting_closed"])
        decisions = payload["decision_counts"]
        self.assertEqual(decisions["total"], len(payload["items"]))
        self.assertEqual(decisions["total"], decisions["pending"] + decisions["completed"])


class ReviewRerunRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = ReviewRerunFixture(self.root)
        store = JobStore(self.root / "jobs.sqlite3")
        parent = store.create_job(
            source_url="",
            output_dir=str(self.fixture.output),
            command=[],
            configuration={"job_type": "translation", "community_owner_id": "owner-a"},
        )
        store.transition(parent, JobStatus.CANCELLED)
        child = store.create_review_rerun(
            parent,
            targets=[{"region_id": "p001:REGION_001", "page": 1, "requires_provider": False}],
            allow_provider=False,
        )
        self.child_id = child["id"]
        self.worker_id = "worker-a"
        store.claim_next_job(self.worker_id, 123)
        store.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_runner_updates_progress_and_finishes_without_provider(self):
        from review_rerun_runner import execute_review_rerun

        calls = []

        class FakeEngine:
            def __init__(self, *args, **kwargs):
                pass

            def revise_page(self, page, **kwargs):
                calls.append((page, kwargs))
                return {
                    "page_revision_id": "draft-a",
                    "page": page,
                    "region_ids": kwargs["region_ids"],
                    "status": "draft_ready",
                    "safe_changes_applied": 1,
                    "manual_review": 0,
                    "visual_state_summary": {"applied": 1},
                }

        result = execute_review_rerun(
            self.child_id,
            str(self.root / "jobs.sqlite3"),
            self.worker_id,
            engine_factory=FakeEngine,
        )
        self.assertEqual(result, 0)
        self.assertEqual(calls[0][1]["cache_only"], True)
        store = JobStore(self.root / "jobs.sqlite3")
        child = store.get_job(self.child_id)
        store.close()
        self.assertEqual(child["status"], JobStatus.FINISHED)
        self.assertEqual(child["progress_current"], 1)
        self.assertEqual(child["progress_total"], 1)

    def test_cancelled_before_work_never_calls_engine(self):
        from review_rerun_runner import execute_review_rerun

        store = JobStore(self.root / "jobs.sqlite3")
        store.request_cancel(self.child_id)
        store.close()

        class FailingEngine:
            def __init__(self, *args, **kwargs):
                raise AssertionError("engine must not be created")

        result = execute_review_rerun(
            self.child_id,
            str(self.root / "jobs.sqlite3"),
            self.worker_id,
            engine_factory=FailingEngine,
        )
        self.assertEqual(result, 0)
        store = JobStore(self.root / "jobs.sqlite3")
        child = store.get_job(self.child_id)
        store.close()
        self.assertEqual(child["status"], JobStatus.CANCELLED)

    def test_runner_counts_regions_and_persists_terminal_outcomes(self):
        from review_rerun_runner import execute_review_rerun

        store = JobStore(self.root / "jobs.sqlite3")
        parent_id = store.get_job(self.child_id)["parent_job_id"]
        store.transition(self.child_id, JobStatus.STARTING, expected_worker=self.worker_id)
        store.transition(self.child_id, JobStatus.RUNNING, expected_worker=self.worker_id)
        store.transition(self.child_id, JobStatus.CANCELLING, expected_worker=self.worker_id)
        store.transition(self.child_id, JobStatus.CANCELLED, expected_worker=self.worker_id)
        child = store.create_review_rerun(
            parent_id,
            targets=[
                {"region_id": "p001:REGION_001", "page": 1, "requires_provider": False},
                {"region_id": "p001:REGION_002", "page": 1, "requires_provider": True},
            ],
            allow_provider=True,
        )
        claimed = store.claim_next_job("worker-b", 456)
        self.assertEqual(claimed["id"], child["id"])
        store.close()

        class FakeEngine:
            def __init__(self, *args, **kwargs):
                pass

            def revise_page(self, page, **kwargs):
                return {
                    "page_revision_id": "draft-b",
                    "page": page,
                    "region_ids": kwargs["region_ids"],
                    "status": "draft_ready",
                    "safe_changes_applied": 1,
                    "manual_review": 0,
                    "visual_state_summary": {
                        "applied": 1,
                        "rejected_visual_regression": 1,
                    },
                }

        self.assertEqual(execute_review_rerun(
            child["id"], str(self.root / "jobs.sqlite3"), "worker-b",
            engine_factory=FakeEngine,
        ), 0)
        store = JobStore(self.root / "jobs.sqlite3")
        finished = store.get_job(child["id"])
        result = store.review_actions(child["id"])
        store.close()
        self.assertEqual(finished["progress_current"], 2)
        self.assertEqual(finished["progress_total"], 2)
        self.assertEqual(result["processed_regions"], 2)
        self.assertEqual(result["approved_regions"], 1)
        self.assertEqual(result["rejected_regions"], 1)
        self.assertEqual(result["remaining_regions"], 0)
        self.assertEqual(result["cancelled_regions"], 0)
        self.assertEqual(result["provider_requests_planned"], 1)


if __name__ == "__main__":
    unittest.main()
