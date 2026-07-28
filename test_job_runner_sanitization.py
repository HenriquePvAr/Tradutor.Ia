"""Hermetic checks for runner diagnostics and terminal source outcomes."""

import _test_bootstrap  # noqa: F401

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from job_runner import _OutputPump, _finalize, _write_manifest
from job_store import JobStatus, JobStore


class JobRunnerSanitizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = JobStore(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.store.close()

    def _running_job(
        self,
        *,
        url="https://reader.test/chapter?token=secret-value",
        configuration_updates=None,
        output_name="chapter",
    ):
        configuration = {
            "mode": "fast", "unknown_url": url,
            "chapter_name": "Veja https://reader.test/private-token?token=title-secret",
        }
        configuration.update(configuration_updates or {})
        output = self.tmp / output_name
        job_id = self.store.create_job(
            source_url=url, output_dir=str(output), command=["fake"],
            configuration=configuration,
        )
        self.store.claim_next_job("worker", 1)
        self.store.transition(job_id, JobStatus.STARTING, expected_worker="worker")
        self.store.transition(job_id, JobStatus.RUNNING, expected_worker="worker")
        return self.store.get_job(job_id), output

    def _write_successful_generic_download(self, output, selection):
        output.mkdir(parents=True)
        pdf = output / "chapter.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        analysis = {
            "adapter": "universal", "final_host": "fresh.reader.test",
            "adapter_version": "1", "confidence": 0.9, "candidate_count": 1,
            "accepted_count": 1, "discarded_count": 0,
            "accepted": [{"id": "fresh-page"}],
            "clusters": [{"key": "cluster:0123456789abcdef0123", "score": 0.9,
                          "candidate_ids": ["fresh-page"]}],
        }
        (output / "timing_report.json").write_text(
            json.dumps({"pdf_path": str(pdf), "quality_validation": {"passed": True}}),
            encoding="utf-8",
        )
        (output / "downloaded_images.json").write_text(
            json.dumps({
                "source_type": "url", "adapter_name": "universal",
                "adapter_version": "1", "transport_name": "browser_session",
                "source_analysis": analysis, "source_selection": selection,
            }),
            encoding="utf-8",
        )
        return analysis

    def test_manifest_and_pumped_log_redact_url_credentials_and_query(self):
        job, output = self._running_job(
            url="https://user:pass@reader.test/chapter?token=secret-value",
            configuration_updates={"create_source_profile": "1"},
        )
        _write_manifest(output, job)
        manifest = (output / "job_manifest.json").read_text(encoding="utf-8")
        self.assertNotIn("user:pass", manifest)
        self.assertNotIn("secret-value", manifest)
        self.assertNotIn("title-secret", manifest)
        self.assertNotIn("unknown_url", manifest)
        self.assertFalse(json.loads(manifest)["configuration"]["create_source_profile"])

        log_path = self.tmp / "runner.log"
        with log_path.open("w", encoding="utf-8") as handle:
            pump = _OutputPump(
                io.BytesIO(b"source https://reader.test/chapter?token=secret-value\n"),
                handle, mock.Mock(), job["id"],
            )
            pump.run()
        self.assertNotIn("secret-value", log_path.read_text(encoding="utf-8"))

    def test_fresh_download_failure_reason_is_preserved_in_terminal_job(self):
        job, output = self._running_job()
        output.mkdir(parents=True)
        (output / "timing_report.json").write_text("{}", encoding="utf-8")
        (output / "downloaded_images.json").write_text(
            json.dumps({"failure": {"code": "challenge_required"}}), encoding="utf-8"
        )
        rc = _finalize(self.store, job["id"], job, output, 1, False, str(self.tmp / "log"))
        terminal = self.store.get_job(job["id"])
        self.assertEqual(rc, 0)
        self.assertEqual(terminal["status"], JobStatus.FAILED)
        self.assertEqual(terminal["reason_code"], "challenge_required")
        self.assertEqual(terminal["error_message"], "challenge_required")

    def test_success_without_fresh_generic_analysis_does_not_record_profile(self):
        job, output = self._running_job()
        output.mkdir(parents=True)
        pdf = output / "chapter.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        (output / "timing_report.json").write_text(
            json.dumps({"pdf_path": str(pdf), "quality_validation": {"passed": True}}),
            encoding="utf-8",
        )
        (output / "downloaded_images.json").write_text("{}", encoding="utf-8")
        with mock.patch("source_profile.SourceProfileStore.record_success") as record:
            _finalize(self.store, job["id"], job, output, 0, False, str(self.tmp / "log"))
        terminal = self.store.get_job(job["id"])
        self.assertEqual(terminal["status"], JobStatus.FINISHED)
        self.assertEqual(terminal["reason_code"], "completed")
        record.assert_not_called()

    def test_profile_uses_fresh_manual_analysis_after_explicit_opt_in(self):
        job, output = self._running_job(
            configuration_updates={"create_source_profile": True},
        )
        fresh_selection = {"candidate_ids": ["fresh-page"], "automatic": False}
        fresh_analysis = self._write_successful_generic_download(output, fresh_selection)
        with mock.patch("source_profile.SourceProfileStore.record_success") as record:
            _finalize(self.store, job["id"], job, output, 0, False, str(self.tmp / "log"))
        terminal = self.store.get_job(job["id"])
        record.assert_called_once_with(fresh_analysis, fresh_selection)
        self.assertEqual(terminal["source_analysis"], fresh_analysis)
        self.assertEqual(terminal["source_selection"], fresh_selection)
        self.assertEqual(terminal["source_type"], "url")
        self.assertEqual(terminal["adapter_name"], "universal")
        self.assertEqual(terminal["adapter_version"], "1")
        self.assertEqual(terminal["transport_name"], "browser_session")
        self.assertEqual(terminal["source_score"], 0.9)
        self.assertEqual(terminal["candidate_count"], 1)
        self.assertEqual(terminal["accepted_count"], 1)
        self.assertEqual(terminal["rejected_count"], 0)
        manifest = json.loads((output / "job_manifest.json").read_text(encoding="utf-8"))
        self.assertIs(manifest["configuration"]["create_source_profile"], True)
        self.assertEqual(manifest["source_provenance"]["transport_name"], "browser_session")

    def test_terminal_download_analysis_preserves_canonical_identity_and_preflight(self):
        job, output = self._running_job()
        prior = {
            "adapter": "webtoons",
            "canonical_identity": {
                "validation_status": "canonical_source_confirmed",
                "identity_hash": "a" * 64,
            },
            "preflight": {
                "http_method": "GET",
                "http_status": 200,
                "content_type": "text/html",
            },
        }
        self.store.update_fields(
            job["id"], source_analysis_json=json.dumps(prior))
        job = self.store.get_job(job["id"])
        self._write_successful_generic_download(
            output, {"candidate_ids": ["fresh-page"], "automatic": True})

        _finalize(self.store, job["id"], job, output, 0, False, str(self.tmp / "log"))

        terminal = self.store.get_job(job["id"])
        self.assertEqual(
            terminal["source_analysis"]["canonical_identity"],
            prior["canonical_identity"])
        self.assertEqual(
            terminal["source_analysis"]["preflight"], prior["preflight"])

    def test_automatic_selection_never_records_profile_even_when_opted_in(self):
        job, output = self._running_job(
            configuration_updates={"create_source_profile": True},
        )
        fresh_selection = {"candidate_ids": ["fresh-page"], "automatic": True}
        self._write_successful_generic_download(output, fresh_selection)
        with mock.patch("source_profile.SourceProfileStore.record_success") as record:
            _finalize(self.store, job["id"], job, output, 0, False, str(self.tmp / "log"))
        record.assert_not_called()

    def test_profile_opt_in_must_be_a_boolean_true_value(self):
        for value, output_name in (("1", "profile-string"), (1, "profile-int"), (False, "profile-false")):
            with self.subTest(value=value):
                job, output = self._running_job(
                    configuration_updates={"create_source_profile": value},
                    output_name=output_name,
                )
                fresh_selection = {"candidate_ids": ["fresh-page"], "automatic": False}
                self._write_successful_generic_download(output, fresh_selection)
                with mock.patch("source_profile.SourceProfileStore.record_success") as record:
                    _finalize(
                        self.store, job["id"], job, output, 0, False, str(self.tmp / "log")
                    )
                record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
