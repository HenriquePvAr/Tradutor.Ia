"""A source-analysis failure inside the worker must leave usable evidence behind.

The production worker runs detached with ``stdout``/``stderr`` set to ``DEVNULL``, so an
exception that ends a job in ``source_analysis`` is invisible unless the worker itself
persists it. These contracts pin what has to survive: the exception class, the root of its
chain, a bounded traceback, the origin frame, the process/runtime context - and never a
secret.

Hermetic: fake analyses, no browser, no network, no child process.
"""

import _test_bootstrap  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path

from job_store import JobStatus, JobStore
from worker_service import Worker

URL = "https://www.webtoons.com/en/drama/serie/episode-1/viewer?title_no=1&episode_no=1"


class _Worker(Worker):
    """Real worker failure handling with the analysis and the spawn replaced."""

    def __init__(self, store, log_dir, error):
        self.store = store
        self.worker_id = "w-test"
        self.pid = 4321
        self._active = None
        self._stop_requested = False
        self._error = error
        self.spawns = 0
        self.log_dir = Path(log_dir)

    def _analyze_source(self, url, *, cancel_check=None, on_progress=None):
        raise self._error

    def _spawn_runner(self, job):
        self.spawns += 1
        raise AssertionError("runner spawned after a failed source analysis")


class UnknownAnalysisFailure(Exception):
    """An exception class the reason taxonomy has never seen."""


class WorkerSourceFailureDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = JobStore(self.tmp / "jobs.sqlite3")
        self.log_dir = self.tmp / "logs"

    def tearDown(self):
        self.store.close()

    def queued_url_job(self):
        job_id = self.store.create_job(
            source_url=URL, output_dir=str(self.tmp / "out"),
            configuration={"job_type": "translation"}, command=["python", "-c", "pass"])
        self.store.update_fields(job_id, source_type="url")
        self.store.transition(job_id, JobStatus.CLAIMING, worker_id="w-test")
        return self.store.get_job(job_id)

    def fail_with(self, error):
        job = self.queued_url_job()
        worker = _Worker(self.store, self.log_dir, error)
        self.assertIsNone(worker._prepare_source(job))
        self.assertEqual(worker.spawns, 0)
        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], JobStatus.FAILED)
        return row, self.diagnostic(row)

    def diagnostic(self, row):
        path = row["error_trace_path"]
        self.assertTrue(path, "no failure diagnostic was persisted")
        return json.loads(Path(path).read_text(encoding="utf-8"))

    # ---- exception evidence -----------------------------------------------
    def test_a_plain_exception_persists_class_stage_origin_and_traceback(self):
        row, evidence = self.fail_with(RuntimeError("synthetic failure"))

        self.assertEqual(evidence["schema_version"], 1)
        self.assertEqual(evidence["kind"], "job_failure_diagnostic")
        self.assertEqual(evidence["stage"], "source_analysis")
        self.assertEqual(evidence["exception_class"], "RuntimeError")
        self.assertEqual(evidence["exception_module"], "builtins")
        self.assertIn("synthetic failure", evidence["safe_message"])
        self.assertEqual(evidence["job_id"], row["id"])
        self.assertEqual(evidence["run_id"], row["run_id"])
        self.assertEqual(evidence["worker_pid"], 4321)
        self.assertTrue(evidence["timestamp"] > 0)
        self.assertTrue(evidence["cwd"])
        self.assertTrue(evidence["process_executable"])
        self.assertIn(evidence["process_mode"], {"python", "pythonw"})
        self.assertTrue(evidence["traceback_summary"], "no traceback frames persisted")
        self.assertTrue(any(
            "test_worker_source_failure_diagnostic" in frame
            for frame in evidence["traceback_summary"]), evidence["traceback_summary"])
        self.assertIn("test_worker_source_failure_diagnostic", evidence["origin_file"])
        self.assertEqual(evidence["origin_function"], "_analyze_source")
        self.assertTrue(int(evidence["origin_line"]) > 0)

    def test_a_wrapped_exception_persists_both_the_outer_and_the_root_class(self):
        try:
            try:
                raise TimeoutError("upstream read timed out")
            except TimeoutError as cause:
                raise ConnectionError("provider transport failed") from cause
        except ConnectionError as wrapped:
            error = wrapped

        _, evidence = self.fail_with(error)

        self.assertEqual(evidence["exception_class"], "ConnectionError")
        self.assertEqual(evidence["root_exception_class"], "TimeoutError")
        self.assertIn("timed out", evidence["root_safe_message"])

    def test_an_implicit_context_chain_is_followed_to_the_root(self):
        try:
            try:
                raise KeyError("candidate_ids")
            except KeyError:
                raise RuntimeError("analysis aborted")
        except RuntimeError as wrapped:
            error = wrapped

        _, evidence = self.fail_with(error)

        self.assertEqual(evidence["exception_class"], "RuntimeError")
        self.assertEqual(evidence["root_exception_class"], "KeyError")

    def test_an_unknown_exception_keeps_the_generic_reason_but_stays_diagnosable(self):
        row, evidence = self.fail_with(UnknownAnalysisFailure("nothing matched"))

        self.assertEqual(row["reason_code"], "source_analysis_failed")
        self.assertEqual(evidence["reason_code"], "source_analysis_failed")
        self.assertEqual(evidence["reason_detail"], "unclassified_exception")
        self.assertEqual(evidence["exception_class"], "UnknownAnalysisFailure")
        self.assertTrue(evidence["reason_classifier_source"])

    def test_a_classified_exception_is_marked_as_classified(self):
        from chapter_source import SourceError

        row, evidence = self.fail_with(SourceError("source_not_ready", "no_driver"))

        self.assertEqual(row["reason_code"], "source_not_ready")
        self.assertEqual(evidence["reason_code"], "source_not_ready")
        self.assertEqual(evidence["reason_detail"], "classified_exception")

    # ---- sanitization ------------------------------------------------------
    def test_secrets_never_reach_the_persisted_diagnostic(self):
        row, _ = self.fail_with(RuntimeError(
            "Authorization: Bearer SECRETVALUE1 api_key=SECRETVALUE2 "
            "password=SECRETVALUE3 NVIDIA_API_KEY=SECRETVALUE4 "
            "Cookie: session=SECRETVALUE5"))

        text = Path(row["error_trace_path"]).read_text(encoding="utf-8")
        for secret in (
            "SECRETVALUE1", "SECRETVALUE2", "SECRETVALUE3", "SECRETVALUE4",
            "SECRETVALUE5",
        ):
            self.assertNotIn(secret, text)

    def test_the_source_url_never_reaches_the_persisted_diagnostic(self):
        row, _ = self.fail_with(RuntimeError(f"failed loading {URL}"))
        text = Path(row["error_trace_path"]).read_text(encoding="utf-8")
        self.assertNotIn("title_no=1", text)

    def test_an_ordinary_failure_keeps_the_information_needed_to_diagnose_it(self):
        _, evidence = self.fail_with(FileNotFoundError(
            2, "No such file or directory",
            r"C:\\Projetos\\Tradutor.Ia\\.cache\\drivers\\chromedriver.exe"))

        self.assertEqual(evidence["exception_class"], "FileNotFoundError")
        self.assertIn("chromedriver", evidence["safe_message"])

    # ---- failure boundaries ------------------------------------------------
    def test_a_lazy_import_failure_is_persisted(self):
        _, evidence = self.fail_with(ImportError("No module named 'selenium'"))

        self.assertEqual(evidence["exception_class"], "ImportError")
        self.assertIn("selenium", evidence["safe_message"])

    def test_a_subprocess_creation_failure_is_persisted(self):
        _, evidence = self.fail_with(OSError(8, "Exec format error"))

        self.assertEqual(evidence["exception_class"], "OSError")
        self.assertTrue(evidence["traceback_summary"])

    # ---- worker health and accounting --------------------------------------
    def test_the_worker_survives_a_diagnostic_write_failure(self):
        job = self.queued_url_job()
        worker = _Worker(self.store, self.tmp / "logs", RuntimeError("boom"))
        # An unwritable diagnostic location must not turn a job failure into a crash.
        worker.log_dir = Path(self.tmp / "jobs.sqlite3") / "not-a-directory"

        self.assertIsNone(worker._prepare_source(job))

        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], JobStatus.FAILED)
        self.assertEqual(row["reason_code"], "source_analysis_failed")

    def test_the_failure_is_terminal_exactly_once_and_not_resumable(self):
        row, _ = self.fail_with(RuntimeError("synthetic failure"))

        self.assertEqual(row["status"], JobStatus.FAILED)
        self.assertEqual(row["stage"], "source_analysis")
        self.assertFalse(row["recoverable"])
        self.assertIsNone(row["runner_pid"])
        self.assertTrue(row["finished_at"])

    def test_a_cancellation_is_not_recorded_as_a_failure_diagnostic(self):
        from chapter_source import SourceError

        job = self.queued_url_job()
        worker = _Worker(self.store, self.log_dir, SourceError("cancelled", "test_cancel"))

        self.assertIsNone(worker._prepare_source(job))

        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], JobStatus.CANCELLED)
        self.assertFalse(row["error_trace_path"])

    def test_the_per_job_log_names_the_exception_class(self):
        row, _ = self.fail_with(RuntimeError("synthetic failure"))
        text = Path(row["log_path"]).read_text(encoding="utf-8")
        self.assertIn("RuntimeError", text)
        self.assertIn("source_analysis_failed", text)

    # ---- legacy runs -------------------------------------------------------
    def test_a_run_without_a_diagnostic_reports_it_as_unavailable(self):
        from job_failure_diagnostic import load_failure_diagnostic

        job = self.queued_url_job()
        row = self.store.transition(
            job["id"], JobStatus.FAILED, reason_code="source_analysis_failed",
            stage="source_analysis")

        evidence = load_failure_diagnostic(row)

        self.assertFalse(evidence["available"])
        self.assertEqual(evidence["reason_code"], "source_analysis_failed")
        self.assertIsNone(evidence["exception_class"])

    def test_a_recorded_diagnostic_is_readable_back_from_the_job_row(self):
        from job_failure_diagnostic import load_failure_diagnostic

        row, _ = self.fail_with(RuntimeError("synthetic failure"))
        evidence = load_failure_diagnostic(row)

        self.assertTrue(evidence["available"])
        self.assertEqual(evidence["exception_class"], "RuntimeError")


class UiFailureDisplayContract(unittest.TestCase):
    """The user sees a short coded reason; never a traceback or a diagnostic path."""

    def setUp(self):
        self.js = (Path(__file__).resolve().parent / "static" / "tradutor_ui.js").read_text(
            encoding="utf-8")

    def test_the_generic_source_analysis_reason_has_a_safe_user_message(self):
        self.assertIn("source_analysis_failed:", self.js)
        self.assertIn("reasonText(record?.reason_code)", self.js)

    def test_the_ui_never_renders_the_failure_diagnostic(self):
        self.assertNotIn("error_trace_path", self.js)
        self.assertNotIn("traceback_summary", self.js)


if __name__ == "__main__":
    unittest.main()
