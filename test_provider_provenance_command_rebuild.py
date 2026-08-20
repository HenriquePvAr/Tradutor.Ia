"""TDD #13: the requested translation provider must survive every command rebuild.

The historical E2E failure: a job requested ``riva``, the submit-time command carried
``--translation-provider riva``, and the rebuild that injects the source-analysis candidate
IDs dropped the flag, so the runner silently used the Nemotron default.
"""

import _test_bootstrap  # noqa: F401

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import benchmark_pipeline
import job_runner
import source_analysis_phase
import ui_bridge
from chapter_source import REVIEW_REQUIRED_MEDIUM_CONFIDENCE, SUPPORTED_SPECIFIC_ADAPTER
from job_store import JobStatus, JobStore
from ui_helpers import (
    assert_command_provider,
    command_translation_provider,
    requested_translation_provider,
)

READER_URL = "https://reader.example.test/chapter/1"


def _config(provider: str | None = "riva", **over) -> dict:
    config = {
        "job_type": "translation", "mode": "fast", "full": True, "use_cache": False,
        "force": True, "use_context": True, "open_output": True, "download_only": False,
    }
    if provider:
        config["translation_provider"] = provider
        config["provider_provenance"] = {
            "provider_requested": provider, "provider_source": "ui_payload",
        }
    config.update(over)
    return config


def _job(provider: str | None = "riva", **over) -> dict:
    return {
        "id": "job-1", "source_url": READER_URL, "output_dir": "output/reader_1",
        "configuration": _config(provider, **over),
    }


class CommandRebuildProviderTests(unittest.TestCase):
    """RED #1, #3, #4: the source-analysis rebuild keeps the canonical provider."""

    def test_riva_survives_the_source_analysis_rebuild(self):
        command = source_analysis_phase.command_with_source_selection(
            _job("riva"), {"candidate_ids": ["page-a", "page-b"]})

        self.assertEqual(command_translation_provider(command), "riva")
        self.assertEqual(command.count("--source-candidate-id"), 2)

    def test_nemotron_survives_the_source_analysis_rebuild(self):
        command = source_analysis_phase.command_with_source_selection(
            _job("nemotron"), {"candidate_ids": ["page-a"]})

        self.assertEqual(command_translation_provider(command), "nemotron")

    def test_deepl_survives_the_source_analysis_rebuild(self):
        # TDD #27: a newly selectable provider must inherit the same guarantee,
        # not re-open the historical "explicit choice lost at rebuild" failure.
        command = source_analysis_phase.command_with_source_selection(
            _job("deepl"), {"candidate_ids": ["page-a", "page-b"]})

        self.assertEqual(command_translation_provider(command), "deepl")
        self.assertEqual(command.count("--source-candidate-id"), 2)

    def test_explicit_deepl_beats_a_nemotron_runtime_default(self):
        with mock.patch.dict(os.environ, {"NVIDIA_TRANSLATION_PROVIDER": "nemotron"}):
            command = source_analysis_phase.command_with_source_selection(
                _job("deepl"), {"candidate_ids": ["page-a"]})

        self.assertEqual(command_translation_provider(command), "deepl")

    def test_explicit_riva_beats_a_nemotron_runtime_default(self):
        with mock.patch.dict(os.environ, {"NVIDIA_TRANSLATION_PROVIDER": "nemotron"}):
            command = source_analysis_phase.command_with_source_selection(
                _job("riva"), {"candidate_ids": ["page-a"]})

        self.assertEqual(command_translation_provider(command), "riva")

    def test_legacy_job_without_a_requested_provider_stays_implicit(self):
        job = _job(None)

        command = source_analysis_phase.command_with_source_selection(
            job, {"candidate_ids": ["page-a"]})

        self.assertEqual(requested_translation_provider(job["configuration"]), "")
        self.assertNotIn("--translation-provider", command)
        # No pretend request: an implicit legacy job must not be blocked either.
        self.assertEqual(assert_command_provider(command, job["configuration"]), command)


class ProviderInvariantTests(unittest.TestCase):
    """RED #5: a rebuilt command that lost or changed the provider fails closed."""

    def test_missing_provider_after_rebuild_is_refused(self):
        with self.assertRaisesRegex(ValueError, "provider_argument_missing"):
            assert_command_provider(["py", "run_webtoon.py", READER_URL], _config("riva"))

    def test_different_provider_after_rebuild_is_refused(self):
        command = ["py", "run_webtoon.py", READER_URL, "--translation-provider", "nemotron"]

        with self.assertRaisesRegex(ValueError, "provider_mismatch"):
            assert_command_provider(command, _config("riva"))

    def test_a_deepl_request_answered_by_riva_argv_is_refused(self):
        command = ["py", "run_webtoon.py", READER_URL, "--translation-provider", "riva"]

        with self.assertRaisesRegex(ValueError, "provider_mismatch"):
            assert_command_provider(command, _config("deepl"))

    def test_matching_provider_passes(self):
        command = ["py", "run_webtoon.py", READER_URL, "--translation-provider", "riva"]

        self.assertEqual(assert_command_provider(command, _config("riva")), command)


class _Bridge(ui_bridge.UiBridge):
    def __init__(self, db_path):
        self.store = JobStore(db_path)
        self.history_revision = 1
        # _create_job derives output_dir from output_root; without it the job (and the
        # runner's mkdir) landed in the developer's real <repo>/output.
        self.output_root = Path(db_path).parent / "output"

    def _refresh_history(self):
        pass

    def ensure_worker(self):
        return {"online": False, "started": False}


class UiBridgeRebuildTests(unittest.TestCase):
    """RED #2: the manual-review rebuild in ui_bridge keeps the provider too."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")
        self.addCleanup(self.bridge.store.close)

    def _awaiting_review_job(self, provider: str) -> dict:
        job = self.bridge._create_job(
            {"url": READER_URL, "chapter_name": "Reader 1", "slug": "reader_1",
             "mode": "fast", "full": True, "use_cache": False, "force": True,
             "use_context": True, "open_output": True,
             "translation_provider": provider},
            require_environment=False, initial_status=JobStatus.STAGING)
        analysis = {
            "adapter": "universal", "final_host": "reader.example.test",
            "outcome": REVIEW_REQUIRED_MEDIUM_CONFIDENCE, "confidence": 0.72,
            "accepted": [{"id": "page-a", "order": 1}, {"id": "page-b", "order": 2}],
            "accepted_count": 2, "discarded_count": 0, "warnings": [],
        }
        self.bridge.store.update_fields(
            job["id"], source_analysis_json=json.dumps(analysis))
        self.bridge.store.transition(job["id"], JobStatus.AWAITING_SOURCE_REVIEW)
        return self.bridge.store.get_job(job["id"])

    def _confirm(self, job_id, ids):
        with mock.patch.object(ui_bridge, "env_status", return_value={
                "env_exists": True, "nvidia_configured": True}):
            return self.bridge.confirm_source_pages(job_id, ids)

    def test_manual_confirmation_preserves_riva(self):
        job = self._awaiting_review_job("riva")
        self.assertEqual(command_translation_provider(job["command"]), "riva")

        self._confirm(job["id"], ["page-b"])

        rebuilt = self.bridge.store.get_job(job["id"])["command"]
        self.assertEqual(command_translation_provider(rebuilt), "riva")
        self.assertIn("page-b", rebuilt)

    def test_manual_confirmation_preserves_deepl(self):
        job = self._awaiting_review_job("deepl")

        self._confirm(job["id"], ["page-a"])

        rebuilt = self.bridge.store.get_job(job["id"])["command"]
        self.assertEqual(command_translation_provider(rebuilt), "deepl")

    def test_manual_confirmation_preserves_nemotron(self):
        job = self._awaiting_review_job("nemotron")

        self._confirm(job["id"], ["page-a"])

        rebuilt = self.bridge.store.get_job(job["id"])["command"]
        self.assertEqual(command_translation_provider(rebuilt), "nemotron")


class _Analysis:
    outcome = SUPPORTED_SPECIFIC_ADAPTER

    def __init__(self, ids):
        self.accepted = [SimpleNamespace(id=value) for value in ids]

    def public(self):
        return {
            "adapter": "universal", "adapter_version": "test-v1",
            "final_host": "reader.example.test", "outcome": SUPPORTED_SPECIFIC_ADAPTER,
            "confidence": 1.0, "candidate_count": len(self.accepted),
            "accepted": [{"id": item.id} for item in self.accepted],
            "accepted_count": len(self.accepted), "discarded_count": 0, "warnings": [],
        }


class ExecutionGateTests(unittest.TestCase):
    """RED #6 and #20: the FINAL argv, not the stored submit command, is validated."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "jobs.sqlite3"
        self.store = JobStore(self.db)
        self.addCleanup(self.store.close)

    def _claimed_job(self, command, config) -> str:
        job_id = self.store.create_job(
            source_url=READER_URL, output_dir=str(self.tmp / "out"),
            command=command, configuration=config)
        claimed = self.store.claim_next_job("worker-1", os.getpid())
        self.assertEqual(claimed["id"], job_id)
        return job_id

    def _run(self, job_id):
        return job_runner.run_job(job_id, str(self.db), "worker-1", str(self.tmp / "run.log"))

    def test_requested_riva_with_a_provider_less_argv_never_spawns(self):
        job_id = self._claimed_job(["py", "run_webtoon.py", READER_URL], _config("riva"))

        with mock.patch.object(subprocess, "Popen") as popen:
            code = self._run(job_id)

        popen.assert_not_called()
        self.assertEqual(code, 2)
        row = self.store.get_job(job_id)
        self.assertEqual(row["status"], JobStatus.FAILED)
        self.assertEqual(row["reason_code"], "provider_argument_missing")

    def test_requested_riva_with_a_nemotron_argv_never_spawns(self):
        job_id = self._claimed_job(
            ["py", "run_webtoon.py", READER_URL, "--translation-provider", "nemotron"],
            _config("riva"))

        with mock.patch.object(subprocess, "Popen") as popen:
            code = self._run(job_id)

        popen.assert_not_called()
        self.assertEqual(code, 2)
        row = self.store.get_job(job_id)
        self.assertEqual(row["status"], JobStatus.FAILED)
        self.assertEqual(row["reason_code"], "provider_mismatch")

    def test_matching_provider_reaches_the_spawn_with_riva_in_argv(self):
        job_id = self._claimed_job(
            ["py", "run_webtoon.py", READER_URL, "--translation-provider", "riva"],
            _config("riva"))
        spawned: list[list[str]] = []

        def capture(command, **_kwargs):
            spawned.append(list(command))
            raise RuntimeError("spawn_reached")   # no real child process in tests

        with mock.patch.object(subprocess, "Popen", side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, "spawn_reached"):
                self._run(job_id)

        self.assertEqual(command_translation_provider(spawned[0]), "riva")

    def test_full_path_from_payload_to_spawn_keeps_deepl(self):
        """TDD #27: payload -> job -> source analysis rebuild -> final spawn argv."""
        bridge = _Bridge(self.db)
        self.addCleanup(bridge.store.close)
        job = bridge._create_job(
            {"url": READER_URL, "chapter_name": "Reader 1", "slug": "reader_1",
             "mode": "fast", "full": True, "use_cache": False, "force": True,
             "use_context": True, "translation_provider": "deepl"},
            require_environment=False, initial_status=JobStatus.STAGING)
        self.assertEqual(command_translation_provider(job["command"]), "deepl")
        bridge.store.transition(job["id"], JobStatus.QUEUED)
        bridge.store.claim_next_job("worker-1", os.getpid())

        with mock.patch.dict(os.environ, {"NVIDIA_TRANSLATION_PROVIDER": "nemotron"}):
            result = source_analysis_phase.apply_source_analysis(
                bridge.store, bridge.store.get_job(job["id"]),
                _Analysis(["page-a", "page-b"]), environment_ready=lambda: True)
        self.assertEqual(result.outcome, source_analysis_phase.SOURCE_READY)

        rebuilt = bridge.store.get_job(job["id"])
        self.assertEqual(command_translation_provider(rebuilt["command"]), "deepl")
        provenance = rebuilt["configuration"]["provider_provenance"]
        self.assertEqual(provenance["provider_requested"], "deepl")
        self.assertEqual(provenance["provider_command_final"], "deepl")

        bridge.store.transition(job["id"], JobStatus.QUEUED)
        claimed = bridge.store.claim_next_job("worker-1", os.getpid())
        spawned: list[list[str]] = []

        def capture(command, **_kwargs):
            spawned.append(list(command))
            raise RuntimeError("spawn_reached")

        with mock.patch.object(subprocess, "Popen", side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, "spawn_reached"):
                self._run(claimed["id"])

        self.assertEqual(command_translation_provider(spawned[0]), "deepl")

    def test_full_path_from_payload_to_spawn_keeps_riva(self):
        """RED #19: payload -> job -> source analysis rebuild -> final spawn argv."""
        bridge = _Bridge(self.db)
        self.addCleanup(bridge.store.close)
        job = bridge._create_job(
            {"url": READER_URL, "chapter_name": "Reader 1", "slug": "reader_1",
             "mode": "fast", "full": True, "use_cache": False, "force": True,
             "use_context": True, "translation_provider": "riva"},
            require_environment=False, initial_status=JobStatus.STAGING)
        self.assertEqual(command_translation_provider(job["command"]), "riva")
        bridge.store.transition(job["id"], JobStatus.QUEUED)
        bridge.store.claim_next_job("worker-1", os.getpid())

        with mock.patch.dict(os.environ, {"NVIDIA_TRANSLATION_PROVIDER": "nemotron"}):
            result = source_analysis_phase.apply_source_analysis(
                bridge.store, bridge.store.get_job(job["id"]),
                _Analysis(["page-a", "page-b"]), environment_ready=lambda: True)
        self.assertEqual(result.outcome, source_analysis_phase.SOURCE_READY)

        rebuilt = bridge.store.get_job(job["id"])
        self.assertEqual(command_translation_provider(rebuilt["command"]), "riva")
        provenance = rebuilt["configuration"]["provider_provenance"]
        self.assertEqual(provenance["provider_requested"], "riva")
        self.assertEqual(provenance["provider_command_final"], "riva")

        bridge.store.transition(job["id"], JobStatus.QUEUED)
        claimed = bridge.store.claim_next_job("worker-1", os.getpid())
        spawned: list[list[str]] = []

        def capture(command, **_kwargs):
            spawned.append(list(command))
            raise RuntimeError("spawn_reached")

        with mock.patch.object(subprocess, "Popen", side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, "spawn_reached"):
                self._run(claimed["id"])

        self.assertEqual(command_translation_provider(spawned[0]), "riva")
        self.assertIn("--source-candidate-id", spawned[0])


class RequestedProviderImmutabilityTests(unittest.TestCase):
    """The runtime may compute ``provider_effective``; it may never invent ``requested``.

    The invalid E2E manifest self-certified as consistent: the flag was lost, so the runner
    rebuilt ``provider_requested`` from the effective runtime provider and reported
    ``requested=nemotron, effective=nemotron, mismatch=false`` for a job that asked for riva.
    """

    class _Translator:
        def __init__(self, name="nemotron"):
            self.stats = {"provider_name": name, "model": f"nvidia/{name}-test"}

    def test_runtime_default_never_becomes_a_request(self):
        with mock.patch.dict(os.environ, {"NVIDIA_TRANSLATION_PROVIDER": "nemotron"}):
            provenance = benchmark_pipeline.resolve_provider_provenance(
                self._Translator(), "")

        self.assertEqual(provenance["provider_requested"], "")
        self.assertEqual(provenance["provider_effective"], "nemotron")
        self.assertEqual(provenance["provider_source"], "runtime_default")

    def test_report_provenance_never_backfills_requested_from_the_effective_provider(self):
        stats = {"provider_name": "nemotron", "provider_effective": "nemotron"}

        provenance = benchmark_pipeline.report_provider_provenance(stats)

        self.assertEqual(provenance["provider_requested"], "")
        self.assertEqual(provenance["provider_effective"], "nemotron")
        self.assertFalse(provenance["provider_mismatch"])

    def test_effective_nemotron_against_requested_riva_fails_closed(self):
        with mock.patch.dict(os.environ, {"NVIDIA_TRANSLATION_PROVIDER": "nemotron"}):
            with self.assertRaisesRegex(RuntimeError, "provider_mismatch"):
                benchmark_pipeline.resolve_provider_provenance(self._Translator(), "riva")

    def test_persisted_request_survives_a_tampered_argv(self):
        tmp = Path(tempfile.mkdtemp())
        store = JobStore(tmp / "jobs.sqlite3")
        self.addCleanup(store.close)
        job_id = store.create_job(
            source_url=READER_URL, output_dir=str(tmp / "out"),
            command=["py", "run_webtoon.py", READER_URL],   # flag stripped after creation
            configuration=_config("riva"))
        store.claim_next_job("worker-1", os.getpid())

        with mock.patch.dict(os.environ, {"NVIDIA_TRANSLATION_PROVIDER": "nemotron"}):
            with mock.patch.object(subprocess, "Popen") as popen:
                code = job_runner.run_job(
                    job_id, str(tmp / "jobs.sqlite3"), "worker-1", str(tmp / "run.log"))

        popen.assert_not_called()
        self.assertEqual(code, 2)
        row = store.get_job(job_id)
        self.assertEqual(row["reason_code"], "provider_argument_missing")
        # The identity of the job is untouched by the runtime default it never asked for.
        self.assertEqual(
            row["configuration"]["provider_provenance"]["provider_requested"], "riva")
        self.assertFalse((tmp / "out" / "run_manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
