"""Start-translation submit path: source selection, queue visibility, idempotency, errors."""

import _test_bootstrap  # noqa: F401

import asyncio
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ui_bridge
from chapter_source import (
    REVIEW_REQUIRED_MEDIUM_CONFIDENCE, SOURCE_NOT_READY, SUPPORTED_SPECIFIC_ADAPTER,
    UNSUPPORTED_LOW_CONFIDENCE,
    SourceError, UniversalChapterAdapter, host_of, select_adapter, supported_hosts,
)
from job_store import JobStatus, JobStore
from ui_helpers import build_run_command
from worker_service import Worker

WEBTOON_URL = "https://www.webtoons.com/en/fantasy/serie/ep-1/viewer?title_no=1&episode_no=1"


class SourceSelectionTests(unittest.TestCase):
    def test_known_host_selects_its_adapter(self):
        self.assertEqual(select_adapter(WEBTOON_URL).name, "webtoons")
        self.assertEqual(select_adapter("https://webtoons.com/x").name, "webtoons")

    def test_unknown_public_host_uses_the_controlled_fallback(self):
        adapter = select_adapter("https://example.org/series/x/chapter-1")
        self.assertIsInstance(adapter, UniversalChapterAdapter)
        self.assertFalse(adapter.is_specific)

    def test_lookalike_host_cannot_impersonate_an_allowed_one(self):
        # Suffix matching on the raw string would let this through.
        for impostor in ("https://evil-webtoons.com/x", "https://webtoons.com.evil.net/x"):
            adapter = select_adapter(impostor)
            self.assertIsInstance(adapter, UniversalChapterAdapter, impostor)

    def test_subdomains_of_allowed_hosts_are_accepted(self):
        self.assertEqual(select_adapter("https://m.webtoons.com/x").name, "webtoons")

    def test_host_parsing_is_defensive(self):
        self.assertEqual(host_of("https://WWW.Webtoons.COM/x"), "webtoons.com")
        self.assertEqual(host_of("not a url"), "")
        self.assertEqual(host_of(""), "")

    def test_credentials_in_url_are_refused(self):
        adapter = select_adapter(WEBTOON_URL)
        with self.assertRaises(ValueError):
            adapter.validate_url("https://user:pass@webtoons.com/x")

    def test_normalize_drops_fragment_and_lowercases_host(self):
        got = select_adapter(WEBTOON_URL).normalize_url("https://WWW.Webtoons.com/en/x?a=1#frag")
        self.assertEqual(got, "https://www.webtoons.com/en/x?a=1")

    def test_supported_hosts_listed_for_the_ui(self):
        self.assertIn("webtoons.com", supported_hosts())

    def test_command_uses_the_adapter_runner_and_never_a_shell_string(self):
        command = build_run_command(
            url=WEBTOON_URL, mode="fast", output="cap", full=True, max_images=None,
            use_cache=False, force=True, use_context=True)
        self.assertIsInstance(command, list)          # never a shell string
        self.assertTrue(command[1].endswith("run_webtoon.py"))
        self.assertIn("--force", command)

    def test_unknown_public_host_builds_the_universal_runner_command(self):
        command = build_run_command(
            url="https://example.org/series/x/chapter-1", mode="fast", output="cap",
            full=True, max_images=None, use_cache=False, force=True, use_context=True)
        self.assertTrue(command[1].endswith("run_webtoon.py"))


class _Bridge(ui_bridge.UiBridge):
    def __init__(self, db_path):
        self.store = JobStore(db_path)
        self.history_revision = 1
        self.worker_calls = 0

    def _refresh_history(self):
        pass

    def ensure_worker(self):
        self.worker_calls += 1
        return {"online": False, "started": False}

    def _analyze_source(self, _url, *, cancel_check=None, on_progress=None):
        return SimpleNamespace(
            outcome=SUPPORTED_SPECIFIC_ADAPTER,
            accepted=[],
            public=lambda: {
                "adapter": "webtoons", "final_host": "webtoons.com",
                "adapter_version": "test-v1",
                "outcome": SUPPORTED_SPECIFIC_ADAPTER, "confidence": 1.0,
                "candidate_count": 0, "accepted": [], "accepted_count": 0, "discarded_count": 0,
            },
        )

    async def _run_source_analysis(self, url, *, cancel_check=None):
        # Keep the hermetic bridge synchronous to the tiny manual coroutine driver below;
        # production uses asyncio.to_thread so Selenium never blocks the UI event loop.
        return self._analyze_source(url, cancel_check=cancel_check)


def drive(coro):
    """Run a coroutine without an event loop (the offline guard blocks the self-pipe)."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("unexpectedly awaited")



class _PhaseWorker(Worker):
    """Worker with the analysis and the spawn replaced; everything else is real."""

    def __init__(self, store, analyzer):
        self.store = store
        self.worker_id = "w-test"
        self.pid = os.getpid()
        self._active = None
        self._stop_requested = False
        self._analyzer = analyzer
        self.spawns = 0

    def _analyze_source(self, url, *, cancel_check=None, on_progress=None):
        return self._analyzer(url, cancel_check=cancel_check)

    def _spawn_runner(self, job):
        self.spawns += 1
        raise AssertionError("runner spawned")


def run_source_phase(bridge, job_id, analyzer):
    """Drive the worker over a queued job, as claim_next_job + _run_one would.

    The submit no longer analyses, so a test that asserts on an analysis outcome has to
    exercise the actor that now produces it.
    """
    bridge.store.transition(job_id, JobStatus.CLAIMING, worker_id="w-test")
    worker = _PhaseWorker(bridge.store, analyzer)
    prepared = worker._prepare_source(bridge.store.get_job(job_id))
    return worker, prepared


class WorkerCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = JobStore(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.store.close()

    def bridge(self):
        bridge = object.__new__(ui_bridge.UiBridge)
        bridge.store = self.store
        bridge.history_revision = 1
        return bridge

    def test_ensure_worker_replaces_a_verified_environment_mismatch(self):
        self.store.register_worker("old", 12345)
        bridge = self.bridge()
        with mock.patch.object(ui_bridge, "_worker_environment_matches_current",
                               return_value=False), \
                mock.patch("start_tradutor.stop_worker", return_value=0) as stop, \
                mock.patch("start_tradutor.start_worker") as start:
            start.side_effect = lambda: self.store.register_worker("new", 67890)
            result = bridge.ensure_worker()
        stop.assert_called_once()
        start.assert_called_once()
        self.assertTrue(result["online"])
        self.assertTrue(result["restarted"])
        self.assertEqual(result["reason"], "worker_environment_mismatch")

    def test_ensure_worker_reuses_a_compatible_worker(self):
        self.store.register_worker("ok", 12345)
        bridge = self.bridge()
        with mock.patch.object(ui_bridge, "_worker_environment_matches_current",
                               return_value=True), \
                mock.patch("start_tradutor.stop_worker") as stop, \
                mock.patch("start_tradutor.start_worker") as start:
            result = bridge.ensure_worker()
        stop.assert_not_called()
        start.assert_not_called()
        self.assertTrue(result["online"])
        self.assertFalse(result["started"])

    def test_reconcile_keeps_worker_owned_source_analysis_in_flight(self):
        bridge = self.bridge()
        job_id = self.store.create_job(
            source_url=WEBTOON_URL, output_dir=str(self.tmp / "out"),
            configuration={"job_type": "translation"}, command=["python", "-c", "pass"])
        self.store.transition(job_id, JobStatus.CLAIMING, worker_id="w1",
                              worker_pid=12345, worker_create_time=456.0)
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False), \
                mock.patch.object(ui_bridge, "_worker_still_alive", return_value=True):
            self.assertEqual(bridge.reconcile_orphans(), [])
        self.assertEqual(self.store.get_job(job_id)["status"], JobStatus.CLAIMING)


class SubmitTests(unittest.TestCase):
    def run_phase(self, job_id):
        return run_source_phase(self.bridge, job_id, self.bridge._analyze_source)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.bridge.store.close()

    def payload(self, **over):
        base = {"url": WEBTOON_URL, "chapter_name": "Serie - Ep 1", "slug": "serie_ep_1",
                "mode": "fast", "full": True, "use_cache": False, "force": True,
                "use_context": True, "open_output": False}
        base.update(over)
        return base

    def start(self, **over):
        with mock.patch.object(ui_bridge, "env_status",
                               return_value={"env_exists": True, "nvidia_configured": True}):
            return drive(self.bridge.start(self.payload(**over)))

    def test_submit_persists_a_job_and_returns_its_id(self):
        result = self.start()
        self.assertTrue(result["ok"])
        self.assertTrue(result["job_id"])
        job = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(job["status"], JobStatus.QUEUED)

    def test_submit_uses_slug_for_output_directory(self):
        result = self.start(slug="daytime_in_the_bunker_episode_17_smoke_9")
        job = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(
            Path(job["output_dir"]).name,
            "daytime_in_the_bunker_episode_17_smoke_9",
        )
        self.assertIn("daytime_in_the_bunker_episode_17_smoke_9", job["command"])

    def test_legacy_output_field_does_not_replace_slug(self):
        result = self.start(
            slug="daytime_in_the_bunker_episode_17_smoke_9",
            output="wrong_output_field",
        )
        job = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(
            Path(job["output_dir"]).name,
            "daytime_in_the_bunker_episode_17_smoke_9",
        )
        self.assertNotIn("wrong_output_field", job["command"])

    def test_remote_submission_persists_sanitized_adapter_provenance(self):
        result = self.start()
        # Provenance is produced by the analysis, which the worker now owns.
        run_source_phase(self.bridge, result["job_id"], self.bridge._analyze_source)
        job = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(job["source_type"], "url")
        self.assertEqual(job["adapter_name"], "webtoons")
        self.assertEqual(job["adapter_version"], "test-v1")
        self.assertEqual(job["transport_name"], "pending")
        self.assertEqual(job["source_score"], 1.0)
        self.assertEqual(job["candidate_count"], 0)
        self.assertEqual(job["input_count"], 0)
        self.assertEqual(job["accepted_count"], 0)
        self.assertEqual(job["rejected_count"], 0)

    def test_remote_resume_preserves_analysis_selection_and_scalar_provenance(self):
        result = self.start()
        run_source_phase(self.bridge, result["job_id"], self.bridge._analyze_source)
        original = self.bridge.store.get_job(result["job_id"])
        self.bridge.store.transition(original["id"], JobStatus.CLAIMING,
                                     worker_id="remote-worker")
        self.bridge.store.transition(original["id"], JobStatus.STARTING, expected_worker="remote-worker")
        self.bridge.store.transition(original["id"], JobStatus.RUNNING, expected_worker="remote-worker")
        self.bridge.store.transition(original["id"], JobStatus.INTERRUPTED, expected_worker="remote-worker")

        resumed = self.bridge.resume(original["id"])
        retry = self.bridge.store.get_job(resumed["job_id"])

        self.assertEqual(retry["source_type"], "url")
        self.assertEqual(retry["adapter_name"], "webtoons")
        self.assertEqual(retry["adapter_version"], "test-v1")
        self.assertEqual(retry["transport_name"], "pending")
        self.assertEqual(retry["source_score"], 1.0)
        self.assertEqual(retry["candidate_count"], 0)
        self.assertEqual(retry["source_analysis"], original["source_analysis"])
        self.assertEqual(retry["source_selection"], original["source_selection"])

    def test_queue_item_starts_with_specific_adapter_provenance(self):
        record = self.bridge.add_queue_item(self.payload())
        self.assertEqual(record["source_provenance"]["adapter_name"], "webtoons")
        self.assertEqual(record["source_provenance"]["transport_name"], "pending")
        self.assertEqual(record["source_provenance"]["candidate_count"], 0)

    def test_submit_ensures_a_consumer_exists(self):
        # The original bug: a job was persisted with nobody to claim it.
        self.start()
        self.assertEqual(self.bridge.worker_calls, 1)

    def test_worker_offline_is_reported_not_hidden(self):
        self.assertIs(self.start()["worker"]["online"], False)

    def test_automatic_source_never_queues_the_pipeline_without_configured_environment(self):
        result = drive(self.bridge.start(self.payload()))
        with mock.patch("source_analysis_phase.apply_source_analysis") as apply:
            from source_analysis_phase import FAILED as PHASE_FAILED, SourceAnalysisPhaseResult

            def refuse(store, job, analysis, **_kw):
                store.transition(job["id"], JobStatus.FAILED,
                                 reason_code="environment_not_configured",
                                 stage="source_analysis")
                return SourceAnalysisPhaseResult(
                    outcome=PHASE_FAILED, job_id=job["id"],
                    reason_code="environment_not_configured")

            apply.side_effect = refuse
            worker, prepared = self.run_phase(result["job_id"])
        self.assertIsNone(prepared)
        self.assertEqual(worker.spawns, 0)
        job = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(job["status"], JobStatus.FAILED)
        self.assertEqual(job["reason_code"], "environment_not_configured")

    def test_double_submit_creates_only_one_job(self):
        first = self.start()
        second = self.start()
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(second["job_id"], first["job_id"])
        queued = self.bridge.store.list_jobs(statuses=[JobStatus.QUEUED])
        self.assertEqual(len(queued), 1)

    def test_unsafe_source_raises_before_persisting_anything(self):
        with self.assertRaises(ValueError):
            self.start(url="file:///C:/secret.txt")
        self.assertEqual(self.bridge.store.list_jobs(statuses=[JobStatus.QUEUED]), [])

    def test_invalid_url_raises_and_persists_nothing(self):
        with self.assertRaises(ValueError):
            self.start(url="not-a-url")
        self.assertEqual(self.bridge.store.list_jobs(statuses=[JobStatus.QUEUED]), [])

    def test_conflicting_cache_and_force_is_rejected(self):
        with self.assertRaises(ValueError):
            self.start(use_cache=True, force=True)

    def test_source_profile_creation_is_an_explicit_boolean_opt_in(self):
        selected = self.start(create_source_profile=True)
        job = self.bridge.store.get_job(selected["job_id"])
        self.assertIs(job["configuration"]["create_source_profile"], True)

        self.bridge.store.close()
        self.bridge = _Bridge(self.tmp / "second-jobs.sqlite3")
        rejected = self.start(create_source_profile="1")
        job = self.bridge.store.get_job(rejected["job_id"])
        self.assertIs(job["configuration"]["create_source_profile"], False)


class LocalFolderSubmitTests(unittest.TestCase):
    """The UI bridge must turn a raw local selection into opaque job provenance only."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")
        self.raw_folder = r"C:\\Users\\Example\\private chapter"
        self.snapshot_calls: list[str] = []

        async def fake_snapshot(raw_folder):
            self.snapshot_calls.append(raw_folder)
            return (
                "snapshot_opaque_1",
                {
                    "source_type": "local_folder",
                    "adapter_name": "local_folder",
                    "adapter_version": "1",
                    "folder_name": "private chapter",
                    "input_root_fingerprint": "a" * 16,
                    "input_count": 2,
                    "accepted_count": 2,
                    "rejected_count": 0,
                    "duplicate_count": 0,
                    "total_size_bytes": 2048,
                    "logical_pages": True,
                    "rejection_reasons": [],
                },
                ["local-0001", "local-0002"],
            )

        self.bridge._snapshot_local_folder = fake_snapshot

    def tearDown(self):
        self.bridge.store.close()

    def payload(self, **over):
        base = {
            "source_type": "local_folder",
            "local_folder": self.raw_folder,
            "chapter_name": "Capítulo local de teste",
            "slug": "capitulo_local_teste",
            "mode": "fast",
            "full": True,
            "use_cache": False,
            "force": True,
            "use_context": True,
            "open_output": False,
        }
        base.update(over)
        return base

    def start(self, **over):
        with mock.patch.object(ui_bridge, "env_status",
                               return_value={"env_exists": True, "nvidia_configured": True}):
            return drive(self.bridge.start(self.payload(**over), local_folder_allowed=True))

    def test_loopback_local_submission_persists_only_opaque_snapshot_provenance(self):
        result = self.start()
        self.assertTrue(result["ok"])
        self.assertEqual(self.snapshot_calls, [self.raw_folder])
        job = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(job["status"], JobStatus.QUEUED)
        self.assertEqual(job["source_type"], "local_folder")
        self.assertEqual(job["snapshot_ref"], "snapshot_opaque_1")
        self.assertEqual(job["transport_name"], "local_snapshot")
        self.assertEqual(job["source_score"], 1.0)
        self.assertEqual(job["candidate_count"], 2)
        self.assertTrue(job["logical_pages"])
        self.assertEqual(job["source_url"], "")
        self.assertIn("run_local_folder.py", " ".join(job["command"]))
        self.assertIn("--snapshot-ref", job["command"])
        self.assertNotIn(self.raw_folder, str(job))
        browser_record = self.bridge._job_record(job)
        self.assertEqual(browser_record["source_type"], "local_folder")
        self.assertEqual(browser_record["url"], "")
        self.assertEqual(browser_record["source_provenance"], {
            "source_type": "local_folder",
            "adapter_name": "local_folder",
            "adapter_version": "1",
            "transport_name": "local_snapshot",
            "score": 1.0,
            "candidate_count": 2,
            "accepted_page_count": 2,
            "rejected_page_count": 0,
            "reason_code": "",
        })
        self.assertNotIn(self.raw_folder, str(browser_record))

    def test_local_source_is_denied_before_snapshot_without_loopback_authorization(self):
        with self.assertRaisesRegex(ValueError, "local_folder_requires_loopback_ui"):
            drive(self.bridge.start(self.payload()))
        self.assertEqual(self.snapshot_calls, [])
        self.assertEqual(self.bridge.store.list_jobs(limit=None), [])

    def test_local_source_rejects_partial_scope_before_snapshot(self):
        with self.assertRaisesRegex(ValueError, "local_folder_requires_full_scope"):
            self.start(full=False, max_images=2)
        self.assertEqual(self.snapshot_calls, [])
        self.assertEqual(self.bridge.store.list_jobs(limit=None), [])

    def test_local_source_failure_is_a_sanitized_terminal_job(self):
        async def failure(_raw_folder):
            raise SourceError("local_path_not_allowed", "outside_allowed_root")

        self.bridge._snapshot_local_folder = failure
        with mock.patch.object(ui_bridge, "env_status",
                               return_value={"env_exists": True, "nvidia_configured": True}):
            with self.assertRaises(SourceError) as ctx:
                drive(self.bridge.start(self.payload(), local_folder_allowed=True))
        self.assertEqual(ctx.exception.code, "local_path_not_allowed")
        failed = self.bridge.store.list_jobs(statuses=[JobStatus.FAILED])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["source_type"], "local_folder")
        self.assertEqual(failed[0]["reason_code"], "local_path_not_allowed")
        self.assertNotIn(self.raw_folder, str(failed[0]))

    def test_declared_source_type_must_match_the_populated_source_field(self):
        with self.assertRaises(SourceError) as ctx:
            self.start(source_type="url")
        self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(self.snapshot_calls, [])

    def test_resume_preserves_path_free_local_provenance(self):
        result = self.start()
        original = self.bridge.store.get_job(result["job_id"])
        self.bridge.store.claim_next_job("local-worker", 999999)
        self.bridge.store.transition(original["id"], JobStatus.STARTING, expected_worker="local-worker")
        self.bridge.store.transition(original["id"], JobStatus.RUNNING, expected_worker="local-worker")
        self.bridge.store.transition(original["id"], JobStatus.INTERRUPTED, expected_worker="local-worker")
        resumed = self.bridge.resume(original["id"])
        retry = self.bridge.store.get_job(resumed["job_id"])
        self.assertEqual(retry["source_type"], "local_folder")
        self.assertEqual(retry["snapshot_ref"], "snapshot_opaque_1")
        self.assertTrue(retry["logical_pages"])
        self.assertEqual(retry["source_analysis"]["folder_name"], "private chapter")
        self.assertNotIn(self.raw_folder, str(retry))

    def test_loopback_policy_requires_both_the_bind_and_peer_to_be_local(self):
        self.assertTrue(ui_bridge.local_folder_ui_allowed(
            bind_host="127.0.0.1", peer_host="::1"))
        self.assertFalse(ui_bridge.local_folder_ui_allowed(
            bind_host="0.0.0.0", peer_host="127.0.0.1"))
        self.assertFalse(ui_bridge.local_folder_ui_allowed(
            bind_host="localhost", peer_host="198.51.100.9"))


class QueueVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.bridge.store.close()

    def queue_one(self):
        with mock.patch.object(ui_bridge, "env_status",
                               return_value={"env_exists": True, "nvidia_configured": True}):
            return drive(self.bridge.start(
                {"url": WEBTOON_URL, "slug": "serie_ep_1", "mode": "fast", "full": True,
                 "use_cache": False, "force": True}))

    def test_queued_job_is_not_reported_as_ready(self):
        self.queue_one()
        state = self.bridge.runtime_state()
        self.assertEqual(state["status"], JobStatus.QUEUED)
        self.assertTrue(state["pending"])
        self.assertTrue(state["queue_running"])       # the UI must not fall back to "pronto"

    def test_offline_worker_surfaces_a_blocked_reason(self):
        self.queue_one()
        state = self.bridge.runtime_state()
        self.assertTrue(state["blocked"])
        self.assertEqual(state["blocked_reason"], "worker_offline")

    def test_empty_queue_is_ready_and_not_blocked(self):
        state = self.bridge.runtime_state()
        self.assertEqual(state["status"], "ready")
        self.assertFalse(state["pending"])
        self.assertFalse(state["blocked"])

    def test_public_job_record_redacts_query_but_store_keeps_execution_url(self):
        job_id = self.bridge.store.create_job(
            source_url="https://reader.example.test/chapter?token=secret-value",
            output_dir=str(self.tmp / "out"), command=["python", "runner"],
        )
        job = self.bridge.store.get_job(job_id)
        record = self.bridge._job_record(job)
        self.assertIn("secret-value", job["source_url"])
        self.assertNotIn("secret-value", record["url"])


class SourceReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")
        candidates = [SimpleNamespace(id="page-a"), SimpleNamespace(id="page-b")]
        self.public = {
            "adapter": "universal", "final_host": "reader.example.test",
            "outcome": REVIEW_REQUIRED_MEDIUM_CONFIDENCE, "confidence": 0.72,
            "accepted": [
                {"id": "page-a", "order": 1, "width": 800, "height": 1200, "origin": "dom"},
                {"id": "page-b", "order": 2, "width": 800, "height": 1200, "origin": "dom"},
            ],
            "accepted_count": 2, "discarded_count": 1, "warnings": [],
        }
        self.bridge._analyze_source = lambda _url, **_kw: SimpleNamespace(
            outcome=REVIEW_REQUIRED_MEDIUM_CONFIDENCE, accepted=candidates,
            public=lambda: dict(self.public),
        )

    def tearDown(self):
        self.bridge.store.close()

    def run_phase(self, job_id):
        return run_source_phase(self.bridge, job_id, self.bridge._analyze_source)

    def payload(self, **over):
        base = {"url": "https://reader.example.test/chapter/1", "chapter_name": "Reader 1",
                "slug": "reader_1", "mode": "fast", "full": True, "use_cache": False,
                "force": True, "use_context": True, "open_output": True}
        base.update(over)
        return base

    def test_medium_confidence_waits_without_a_runner_and_exposes_sanitized_review(self):
        result = drive(self.bridge.start(self.payload()))
        worker, prepared = self.run_phase(result["job_id"])
        self.assertIsNone(prepared)                      # no runner for a review
        self.assertEqual(worker.spawns, 0)
        job = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(job["status"], JobStatus.AWAITING_SOURCE_REVIEW)
        self.assertEqual(job["stage"], "awaiting_source_review")
        self.assertEqual(job["source_analysis"]["accepted"][0]["id"], "page-a")
        self.assertNotIn("https://", str(job["source_analysis"]))
        self.assertEqual(self.bridge.runtime_state()["source_review"]["id"], result["job_id"])

    def test_known_incomplete_source_never_enters_manual_page_review(self):
        self.public["warnings"] = ["pagination_incomplete"]
        result = drive(self.bridge.start(self.payload()))
        worker, prepared = self.run_phase(result["job_id"])
        # The intent is unchanged — a known-incomplete source must not reach manual review —
        # but the verdict is now produced by the worker, not by the submit.
        self.assertIsNone(prepared)
        job = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(job["status"], JobStatus.FAILED)
        self.assertEqual(job["reason_code"], "incomplete_source_coverage")
        self.assertEqual(job["stage"], "source_analysis")
        # The invariant the bug violated: a download verdict cannot be born in analysis.
        self.assertNotEqual(job["reason_code"], "incomplete_download")
        self.assertEqual(worker.spawns, 0)

    def test_confirmation_requires_a_nonempty_known_subset_and_preserves_open_output(self):
        result = drive(self.bridge.start(self.payload()))
        self.run_phase(result["job_id"])
        with self.assertRaises(ValueError):
            self.bridge.confirm_source_pages(result["job_id"], [])
        with self.assertRaises(ValueError):
            self.bridge.confirm_source_pages(result["job_id"], ["not-observed"])
        with mock.patch.object(ui_bridge, "env_status", return_value={"env_exists": True, "nvidia_configured": True}):
            confirmed = self.bridge.confirm_source_pages(result["job_id"], ["page-b"])
        self.assertTrue(confirmed["ok"])
        job = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(job["status"], JobStatus.QUEUED)
        self.assertIn("--source-candidate-id", job["command"])
        self.assertIn("page-b", job["command"])
        self.assertIn("--open-output", job["command"])
        self.assertFalse(job["source_selection"]["automatic"])

    def test_confirmation_records_explicit_manual_page_order(self):
        result = drive(self.bridge.start(self.payload()))
        self.run_phase(result["job_id"])
        with mock.patch.object(ui_bridge, "env_status", return_value={
            "env_exists": True, "nvidia_configured": True,
        }):
            self.bridge.confirm_source_pages(result["job_id"], ["page-b", "page-a"])
        job = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(job["source_selection"]["candidate_ids"], ["page-b", "page-a"])
        self.assertTrue(job["source_selection"]["manual_reordered"])
        self.assertLess(job["command"].index("page-b"), job["command"].index("page-a"))

    def test_staging_source_job_records_the_creating_ui_process(self):
        job = self.bridge._create_job(
            self.payload(), require_environment=False, initial_status=JobStatus.STAGING)
        self.assertEqual(job["status"], JobStatus.STAGING)
        self.assertEqual(job["worker_pid"], os.getpid())
        self.assertIsNotNone(job["worker_create_time"])
        worker = Worker(self.tmp / "jobs.sqlite3", poll_seconds=0.01, stale_seconds=5)
        try:
            # This is the former race: recovery ran while the UI was still analysing and
            # treated the row as ownerless after its short grace period.
            worker._recover_staged_community_publishes(grace_seconds=0)
        finally:
            worker.close()
        self.assertEqual(self.bridge.store.get_job(job["id"])["status"], JobStatus.STAGING)

    def test_controlled_manual_selection_keeps_max_images_and_subset_provenance(self):
        result = drive(self.bridge.start(self.payload(full=False, max_images=2)))
        self.run_phase(result["job_id"])
        with mock.patch.object(ui_bridge, "env_status", return_value={
            "env_exists": True, "nvidia_configured": True,
        }):
            self.bridge.confirm_source_pages(result["job_id"], ["page-b"])
        job = self.bridge.store.get_job(result["job_id"])
        self.assertIn("--max-images", job["command"])
        self.assertIn("2", job["command"])
        self.assertEqual(job["configuration"]["max_images"], 2)
        self.assertTrue(job["source_selection"]["manual_subset"])
        self.assertEqual(job["source_selection"]["accepted_candidate_count"], 2)
        self.assertEqual(job["source_selection"]["selected_candidate_count"], 1)

    def test_low_confidence_is_a_frozen_sanitized_failure_without_a_worker(self):
        self.bridge._analyze_source = lambda _url, **_kw: SimpleNamespace(
            outcome=UNSUPPORTED_LOW_CONFIDENCE, accepted=[],
            public=lambda: {"adapter": "universal", "outcome": UNSUPPORTED_LOW_CONFIDENCE,
                            "accepted": [], "accepted_count": 0, "discarded_count": 0},
        )
        result = drive(self.bridge.start(self.payload()))
        worker, prepared = self.run_phase(result["job_id"])
        self.assertIsNone(prepared)
        job = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(job["status"], JobStatus.FAILED)
        self.assertEqual(job["reason_code"], UNSUPPORTED_LOW_CONFIDENCE)
        self.assertIsNotNone(job["finished_at"])
        self.assertEqual(worker.spawns, 0)

    def test_cancel_review_freezes_it_without_queueing(self):
        result = drive(self.bridge.start(self.payload()))
        self.run_phase(result["job_id"])
        drive(self.bridge.cancel(job_id=result["job_id"]))
        job = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(job["status"], JobStatus.CANCELLED)
        self.assertIsNotNone(job["finished_at"])

    def test_cancelling_displayed_review_never_cancels_another_waiting_review(self):
        first = self.bridge._create_job(
            self.payload(slug="first-review"), require_environment=False,
            initial_status=JobStatus.AWAITING_SOURCE_REVIEW,
            source_analysis=self.public,
        )
        # Ensure the second job is the one the runtime would actually show in the review UI.
        self.bridge.store.update_fields(first["id"], updated_at=time.time() - 1)
        second = self.bridge._create_job(
            self.payload(slug="second-review"), require_environment=False,
            initial_status=JobStatus.AWAITING_SOURCE_REVIEW,
            source_analysis=self.public,
        )

        result = drive(self.bridge.cancel(job_id=second["id"]))

        self.assertEqual(result["job_id"], second["id"])
        self.assertEqual(self.bridge.store.get_job(second["id"])["status"], JobStatus.CANCELLED)
        self.assertEqual(
            self.bridge.store.get_job(first["id"])["status"], JobStatus.AWAITING_SOURCE_REVIEW)

    def test_stale_review_id_is_rejected_without_cancelling_any_waiting_review(self):
        first = self.bridge._create_job(
            self.payload(slug="first-stale-review"), require_environment=False,
            initial_status=JobStatus.AWAITING_SOURCE_REVIEW,
            source_analysis=self.public,
        )
        self.bridge.store.update_fields(first["id"], updated_at=time.time() - 1)
        second = self.bridge._create_job(
            self.payload(slug="second-stale-review"), require_environment=False,
            initial_status=JobStatus.AWAITING_SOURCE_REVIEW,
            source_analysis=self.public,
        )

        with self.assertRaisesRegex(ValueError, "source_review_not_available"):
            drive(self.bridge.cancel(job_id=first["id"]))
        self.assertEqual(
            self.bridge.store.get_job(first["id"])["status"], JobStatus.AWAITING_SOURCE_REVIEW)
        self.assertEqual(
            self.bridge.store.get_job(second["id"])["status"], JobStatus.AWAITING_SOURCE_REVIEW)

    def test_coded_remote_source_failure_is_recorded_then_returned_to_the_api(self):
        def raises(_url, **_kw):
            raise SourceError("challenge_required", "sanitized")

        result = drive(self.bridge.start(self.payload()))
        # The submit accepts the job; the coded failure now reaches the user through the
        # job row and polling instead of an exception out of the HTTP request.
        worker, prepared = run_source_phase(self.bridge, result["job_id"], raises)
        self.assertIsNone(prepared)
        self.assertEqual(worker.spawns, 0)
        failed = self.bridge.store.list_jobs(statuses=[JobStatus.FAILED])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["reason_code"], "challenge_required")
        self.assertIsNotNone(failed[0]["finished_at"])


class SourceAnalysisTimeoutTests(unittest.TestCase):
    """A slow analysis is the worker's problem now, not the request's.

    The old test drove a timeout inside the submit and asserted the staging row flipped so
    the background thread cancelled itself. That mechanism is gone: the submit no longer
    analyses anything. The requirement it protected survives — an analysis that cannot
    finish must end the job terminally and must never leave a runner behind.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.bridge.store.close()

    def payload(self):
        return {"url": WEBTOON_URL, "slug": "timeout_probe", "mode": "fast", "full": True,
                "use_cache": False, "force": True, "use_context": True}

    def test_submit_returns_before_any_analysis_could_time_out(self):
        started = time.monotonic()
        result = drive(self.bridge.start(self.payload()))
        # The submit does no analysis at all, so its duration cannot depend on one. A slow
        # analyzer would blow this budget by two orders of magnitude if it were called.
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], JobStatus.QUEUED)

    def test_a_timed_out_analysis_is_terminal_and_spawns_no_runner(self):
        result = drive(self.bridge.start(self.payload()))

        def times_out(_url, **_kw):
            raise SourceError(SOURCE_NOT_READY, "source_analysis_timeout")

        worker, prepared = run_source_phase(self.bridge, result["job_id"], times_out)
        self.assertIsNone(prepared)
        self.assertEqual(worker.spawns, 0)
        failed = self.bridge.store.list_jobs(statuses=[JobStatus.FAILED])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["reason_code"], SOURCE_NOT_READY)
        self.assertIsNotNone(failed[0]["finished_at"])

    def test_a_cancelled_analysis_never_reaches_the_runner(self):
        result = drive(self.bridge.start(self.payload()))
        self.bridge.store.request_cancel(result["job_id"])

        def never_called(_url, **_kw):
            raise AssertionError("analysis ran for a cancelled job")

        worker, prepared = run_source_phase(self.bridge, result["job_id"], never_called)
        self.assertIsNone(prepared)
        self.assertEqual(worker.spawns, 0)


class FixtureFilteringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.bridge.store.close()

    def terminal_job(self, *, output_dir, configuration=None):
        job_id = self.bridge.store.create_job(
            source_url=WEBTOON_URL, output_dir=str(output_dir),
            configuration={"job_type": "translation", **(configuration or {})},
            command=["python", "run_webtoon.py"])
        self.bridge.store.transition(job_id, JobStatus.CLAIMING, worker_id="w1")
        self.bridge.store.transition(job_id, JobStatus.STARTING)
        self.bridge.store.transition(job_id, JobStatus.RUNNING)
        self.bridge.store.transition(job_id, JobStatus.FINISHED)
        return job_id

    def test_smoke_fixture_is_never_presented_as_the_last_result(self):
        # The AUTH SMOKE TEST symptom: a terminal translation row whose output is gone.
        self.terminal_job(output_dir=self.tmp / "auth_smoke_gone")
        self.assertIsNone(self.bridge._latest_terminal_job())
        self.assertIsNone(self.bridge.runtime_state()["latest"])

    def test_explicitly_flagged_fixture_is_filtered_even_with_output(self):
        real = self.tmp / "flagged"
        real.mkdir()
        self.terminal_job(output_dir=real, configuration={"fixture": True})
        self.assertIsNone(self.bridge._latest_terminal_job())

    def test_a_real_result_is_still_shown(self):
        real = self.tmp / "serie_ep_1"
        real.mkdir()
        job_id = self.terminal_job(output_dir=real)
        self.assertEqual(self.bridge._latest_terminal_job()["id"], job_id)


class FrontendContractTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent
        self.js = (root / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.html = (root / "ui" / "ui_shell.html").read_text(encoding="utf-8")

    def test_in_flight_label_shown_while_the_request_runs(self):
        self.assertIn("Iniciando processamento…", self.js)

    def test_controls_are_flipped_only_after_the_backend_accepts(self):
        # This assertion inverted with the architecture. Flipping first was a workaround for
        # a submit that held the request for 93-101s while it analysed; the submit now
        # enqueues and returns a job to poll, so the response is what flips the controls.
        start = self.js[self.js.index("async function startTranslation"):]
        body = start[:start.index("\n  async function cancelTranslation")]
        self.assertLess(body.index("await api('/api/ui/run'"), body.index("setRunControls(true"))

    def test_double_click_guard(self):
        self.assertIn("button.dataset.busy", self.js)

    def test_error_panel_exists_and_is_rendered(self):
        self.assertIn('id="startError"', self.html)
        self.assertIn("showStartError", self.js)
        self.assertIn("Tentar novamente", self.js)

    def test_preview_is_reset_on_a_new_run(self):
        self.assertIn("resetRunPreview", self.js)

    def test_error_panel_never_prints_secrets_or_tracebacks(self):
        panel = self.js[self.js.index("function showStartError"):]
        panel = panel[:panel.index("async function startTranslation")]
        for bad in ("traceback", "stack", "NVIDIA_API_KEY", "Authorization", "token", ".env"):
            self.assertNotIn(bad, panel, bad)


if __name__ == "__main__":
    unittest.main()
