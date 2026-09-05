"""Permanent tripwires: tests can never reach the project's real runtime state.

Root cause this locks down: ``UiBridge.__init__`` resolved ``runtime_root`` to
``<repo>/.cache/runtime`` (and ``JOBS_DB_PATH`` to the real ``jobs.sqlite3``) whenever no
runtime root was supplied.  Because ``app_ui.py`` builds a module-level ``BRIDGE =
UiBridge()``, merely importing ``app_ui`` from a test opened the user's real queue and ran
orphan reconciliation against it, and ``ensure_worker`` could launch the real
``worker_service.py`` through ``start_tradutor.start_worker``.

Every assertion below is safe: the real runtime is only ever *named*, never opened, listed
or mutated.  ``hermetic_runtime`` refuses each attempt before the side effect happens.
"""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import hermetic_runtime
import ui_bridge
from hermetic_runtime import (
    REAL_JOBS_DB,
    REAL_OUTPUT_ROOT,
    REAL_RUNTIME_ROOT,
    REAL_UI_HISTORY_PATHS,
    RealRuntimeAccess,
    is_real_runtime_path,
    is_real_user_state_path,
    sqlite_uri_path,
)
from job_store import JobStatus, JobStore
from ui_history import UIHistoryStore
from runtime_paths import default_user_data_root

REPO_ROOT = Path(__file__).resolve().parent


class RealRuntimePathTripwireTests(unittest.TestCase):
    """The real runtime root is FORBIDDEN for every filesystem verb the suite can use."""

    def test_canonicalization_covers_relative_case_and_separators(self):
        self.assertTrue(is_real_runtime_path(REAL_JOBS_DB))
        self.assertTrue(is_real_runtime_path(".cache/runtime/jobs.sqlite3"))
        self.assertTrue(is_real_runtime_path(r".cache\runtime\jobs.sqlite3"))
        self.assertTrue(is_real_runtime_path(str(REAL_RUNTIME_ROOT).upper() + r"\logs"))
        self.assertTrue(is_real_runtime_path(REAL_RUNTIME_ROOT / "logs" / "worker.log"))
        self.assertFalse(is_real_runtime_path(REPO_ROOT / ".cache" / "ui_profile.json"))
        self.assertFalse(is_real_runtime_path("output/chapter"))

    def test_reading_the_real_jobs_db_is_refused(self):
        with self.assertRaises(RealRuntimeAccess):
            open(REAL_JOBS_DB, "rb")

    def test_writing_under_the_real_runtime_root_is_refused(self):
        with self.assertRaises(RealRuntimeAccess):
            open(REAL_RUNTIME_ROOT / "tripwire.txt", "w")
        with self.assertRaises(RealRuntimeAccess):
            (REAL_RUNTIME_ROOT / "tripwire.txt").write_text("x", encoding="utf-8")

    def test_creating_directories_under_the_real_runtime_root_is_refused(self):
        with self.assertRaises(RealRuntimeAccess):
            os.makedirs(REAL_RUNTIME_ROOT / "tripwire", exist_ok=True)
        with self.assertRaises(RealRuntimeAccess):
            (REAL_RUNTIME_ROOT / "tripwire").mkdir(parents=True, exist_ok=True)

    def test_deleting_and_renaming_under_the_real_runtime_root_is_refused(self):
        with self.assertRaises(RealRuntimeAccess):
            os.unlink(REAL_RUNTIME_ROOT / "jobs.sqlite3")
        with self.assertRaises(RealRuntimeAccess):
            os.replace(REAL_JOBS_DB, REAL_RUNTIME_ROOT / "moved.sqlite3")

    def test_the_real_runtime_is_never_read_or_modified_by_the_tripwire(self):
        """Refusals are name comparisons only; nothing was opened, listed or changed."""
        self.assertFalse((REAL_RUNTIME_ROOT / "tripwire.txt").exists())
        self.assertFalse((REAL_RUNTIME_ROOT / "tripwire").exists())
        self.assertFalse((REAL_RUNTIME_ROOT / "moved.sqlite3").exists())


class RealJobsDatabaseTripwireTests(unittest.TestCase):
    def test_sqlite_connect_to_the_real_jobs_db_fails_before_connecting(self):
        with self.assertRaises(RealRuntimeAccess):
            sqlite3.connect(REAL_JOBS_DB)
        with self.assertRaises(RealRuntimeAccess):
            sqlite3.connect(str(REAL_JOBS_DB))
        with self.assertRaises(RealRuntimeAccess):
            sqlite3.connect(".cache/runtime/jobs.sqlite3")

    def test_job_store_cannot_be_pointed_at_the_real_jobs_db(self):
        with self.assertRaises(RealRuntimeAccess):
            JobStore(REAL_JOBS_DB)

    def test_the_module_default_uses_user_data_not_the_legacy_repo_db(self):
        """Beta defaults must be writable outside the installation directory."""
        self.assertEqual(ui_bridge.JOBS_DB_PATH, Path(os.environ["TRADUTOR_TEST_RUNTIME_ROOT"]) / "jobs.sqlite3")
        self.assertFalse(is_real_runtime_path(ui_bridge.JOBS_DB_PATH))


class SqliteUriTripwireTests(unittest.TestCase):
    """A ``file:`` URI names the same database as a plain path and must be refused too.

    Root cause: ``is_real_runtime_path`` normalised through ``os.path.abspath``, which sees
    ``file:C:/...`` as a *relative* name and resolves it under the cwd, so no URI form of
    the real queue ever matched. Nothing below opens a database: the guard refuses on the
    name, ahead of ``sqlite3.connect``.
    """

    @staticmethod
    def _uri(path: Path, query: str = "") -> str:
        return "file:" + str(path).replace("\\", "/") + query

    def _real_uris(self) -> tuple[str, ...]:
        posix = str(REAL_JOBS_DB).replace("\\", "/")
        return (
            self._uri(REAL_JOBS_DB),
            self._uri(REAL_JOBS_DB, "?mode=ro"),
            self._uri(REAL_JOBS_DB, "?mode=rw&cache=shared"),
            self._uri(REAL_JOBS_DB, "?mode=ro&vfs=win32&immutable=1"),
            f"file:///{posix}",
            f"file:///{posix}?mode=ro",
            f"file://localhost/{posix}?mode=ro",
            "file:" + posix.replace(".cache", ".%63ache") + "?mode=ro",   # percent-encoded
        )

    def test_every_uri_spelling_of_the_real_queue_is_recognised(self):
        for uri in self._real_uris():
            self.assertTrue(is_real_runtime_path(uri), uri)

    def test_sqlite_connect_refuses_every_uri_spelling_before_connecting(self):
        for uri in self._real_uris():
            with self.assertRaises(RealRuntimeAccess, msg=uri):
                sqlite3.connect(uri, uri=True)

    def test_isolated_and_non_file_databases_stay_connectable(self):
        """False positives would make the suite untestable, so prove the allowed cases."""
        self.assertFalse(is_real_runtime_path(":memory:"))
        self.assertFalse(is_real_runtime_path("file::memory:?cache=shared"))
        self.assertFalse(is_real_runtime_path("file:isolated.db?mode=ro"))
        sqlite3.connect(":memory:").close()
        sqlite3.connect("file::memory:?cache=shared", uri=True).close()
        with tempfile.TemporaryDirectory(prefix="tradutor-uri-") as folder:
            db = Path(folder) / "jobs.sqlite3"
            db.write_bytes(b"")
            uri = self._uri(db, "?mode=ro")
            self.assertFalse(is_real_runtime_path(uri), uri)
            sqlite3.connect(uri, uri=True).close()

    def test_uri_parsing_keeps_query_parameters_out_of_the_path(self):
        self.assertEqual(sqlite_uri_path("file:jobs.sqlite3?mode=ro"), "jobs.sqlite3")
        self.assertEqual(sqlite_uri_path("file:///C:/x/jobs.db"), "C:/x/jobs.db")
        self.assertIsNone(sqlite_uri_path("file::memory:"))
        self.assertIsNone(sqlite_uri_path("file://remote.invalid/share/jobs.db"))
        self.assertIsNone(sqlite_uri_path("C:/x/jobs.db"))

    def test_the_real_queue_was_never_opened(self):
        self.assertEqual(REAL_JOBS_DB.stat().st_size, self.size_before)

    @classmethod
    def setUpClass(cls):
        cls.size_before = REAL_JOBS_DB.stat().st_size if REAL_JOBS_DB.exists() else -1


class RealOutputTripwireTests(unittest.TestCase):
    """A test must never discover the developer's real translated chapters.

    Root cause: ``UIHistoryStore.discover_outputs`` enumerated the module-level
    ``ui_helpers.OUTPUT_ROOT`` (``<repo>/output``), which the runtime guard did not cover,
    so a store built without an explicit root produced machine-dependent history.
    """

    def test_the_real_output_root_and_history_files_are_recognised(self):
        self.assertTrue(is_real_user_state_path(REAL_OUTPUT_ROOT))
        self.assertTrue(is_real_user_state_path(REAL_OUTPUT_ROOT / "any_chapter"))
        for path in REAL_UI_HISTORY_PATHS:
            self.assertTrue(is_real_user_state_path(path))
        self.assertFalse(is_real_user_state_path(REPO_ROOT / "output_manifest.py"))
        self.assertFalse(is_real_user_state_path(Path(tempfile.gettempdir()) / "output"))

    def test_enumerating_and_reading_the_real_output_root_is_refused(self):
        with self.assertRaises(RealRuntimeAccess):
            os.listdir(REAL_OUTPUT_ROOT)
        with self.assertRaises(RealRuntimeAccess):
            list(REAL_OUTPUT_ROOT.iterdir())
        with self.assertRaises(RealRuntimeAccess):
            open(REAL_OUTPUT_ROOT / "any_chapter" / "timing_report.json", "rb")
        with self.assertRaises(RealRuntimeAccess):
            REAL_UI_HISTORY_PATHS[0].write_text("[]", encoding="utf-8")

    def test_a_default_history_store_no_longer_discovers_legacy_repo_runs(self):
        with tempfile.TemporaryDirectory(prefix="tradutor-history-") as folder:
            store = UIHistoryStore(Path(folder) / "ui_history.json")
            self.assertFalse(is_real_user_state_path(store.output_root))

    def test_an_isolated_output_root_discovers_its_own_runs_and_only_those(self):
        """Real discovery semantics, isolated data — not a mocked-away history."""
        with tempfile.TemporaryDirectory(prefix="tradutor-history-") as folder:
            output_root = Path(folder) / "output"
            run = output_root / "isolated_chapter_01"
            run.mkdir(parents=True)
            (run / "timing_report.json").write_text(
                '{"total_seconds": 12, "processed_images": 3}', encoding="utf-8")
            store = UIHistoryStore(
                Path(folder) / "ui_history.json", output_root=output_root)
            slugs = [record["slug"] for record in store.discover_outputs()]
        self.assertEqual(slugs, ["isolated_chapter_01"])

    def test_production_construction_resolves_the_user_data_output_root(self):
        """Name only — resolving the root does not enumerate it."""
        self.assertEqual(
            UIHistoryStore(Path(tempfile.gettempdir()) / "ui_history.json").output_root,
            default_user_data_root() / "output",
        )

    def test_the_real_output_root_was_never_read_or_modified(self):
        self.assertFalse((REAL_OUTPUT_ROOT / "tripwire").exists())


class RealWorkerSpawnTripwireTests(unittest.TestCase):
    def test_launching_the_worker_without_an_isolated_db_fails_before_process_creation(self):
        """No ``--db`` means worker_service falls back to the production queue."""
        with self.assertRaises(RealRuntimeAccess):
            subprocess.Popen([sys.executable, str(REPO_ROOT / "worker_service.py")])
        with self.assertRaises(RealRuntimeAccess):
            subprocess.Popen(
                [sys.executable, str(REPO_ROOT / "worker_service.py"),
                 "--db", str(REAL_JOBS_DB)]
            )

    def test_a_worker_pointed_at_an_isolated_db_is_still_allowed(self):
        """Mission §25B: subprocess worker semantics stay testable, just not on real state."""
        with tempfile.TemporaryDirectory(prefix="tradutor-worker-") as folder:
            argv = [
                sys.executable, str(REPO_ROOT / "worker_service.py"),
                "--status", "--db", str(Path(folder) / "jobs.sqlite3"),
            ]
            completed = subprocess.run(
                argv, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120
            )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_launching_the_real_launcher_fails_before_process_creation(self):
        with self.assertRaises(RealRuntimeAccess):
            subprocess.run([sys.executable, str(REPO_ROOT / "start_tradutor.py")])
        with self.assertRaises(RealRuntimeAccess):
            subprocess.run([str(REPO_ROOT / "start_tradutor.bat"), "all"])

    def test_launching_the_ui_without_an_isolated_runtime_root_is_refused(self):
        """app_ui builds a module-level UiBridge(), so its runtime root must be isolated."""
        environment = {
            key: value for key, value in os.environ.items()
            if key != "TRADUTOR_TEST_RUNTIME_ROOT"
        }
        with self.assertRaises(RealRuntimeAccess):
            subprocess.Popen([sys.executable, "app_ui.py"], cwd=REPO_ROOT, env=environment)
        with self.assertRaises(RealRuntimeAccess):
            subprocess.Popen(
                [sys.executable, "app_ui.py"], cwd=REPO_ROOT,
                env={**environment, "TRADUTOR_TEST_RUNTIME_ROOT": str(REAL_RUNTIME_ROOT)},
            )

    def test_any_subprocess_argument_naming_the_real_runtime_is_refused(self):
        with self.assertRaises(RealRuntimeAccess):
            subprocess.Popen([sys.executable, "-c", "pass", "--db", str(REAL_JOBS_DB)])

    def test_ensure_worker_uses_the_isolated_runtime_inside_tests(self):
        """``ensure_worker`` must never route tests to user runtime state.

        The launcher module resolves its DB at import time, so the hermetic runtime root
        has to be visible to ``runtime_paths`` before the module is imported.
        """
        import start_tradutor

        self.assertEqual(start_tradutor.DB_PATH, Path(os.environ["TRADUTOR_TEST_RUNTIME_ROOT"]) / "jobs.sqlite3")
        self.assertFalse(is_real_runtime_path(start_tradutor.DB_PATH))


class IsolatedRuntimeVisibilityTests(unittest.TestCase):
    """Real SQLite semantics against an isolated path — not a mock (mission §18)."""

    def _bridge_root(self) -> Path:
        return Path(os.environ["TRADUTOR_TEST_RUNTIME_ROOT"])

    def test_all_default_mutable_paths_stay_inside_test_root(self):
        import runtime_paths

        overrides = {key: "" for key in (
            "CACHE_ROOT", "TEMP_FOLDER", "TEMP_OUT", "TRADUTOR_OUTPUT_ROOT",
            "TRADUTOR_USER_DATA_ROOT", "TRADUTOR_RUNTIME_ROOT")}
        with patch.dict(os.environ, overrides):
            for resolver in (runtime_paths.runtime_root, runtime_paths.cache_root,
                             runtime_paths.output_root, runtime_paths.temp_input_root,
                             runtime_paths.temp_output_root):
                resolved = resolver().resolve()
                self.assertTrue(resolved == self._bridge_root() or self._bridge_root() in resolved.parents)

    def test_installed_beta_runtime_and_output_are_guarded(self):
        root = hermetic_runtime.REAL_BETA_USER_ROOT
        self.assertTrue(is_real_runtime_path(root / "runtime" / "jobs.sqlite3"))
        for name in ("cache", "output", "temp"):
            self.assertTrue(is_real_user_state_path(root / name / "sentinel"))
        with self.assertRaises(RealRuntimeAccess):
            sqlite3.connect((root / "runtime" / "jobs.sqlite3").as_uri() + "?mode=ro", uri=True)

    def test_the_suite_runs_with_a_temporary_runtime_root(self):
        root = self._bridge_root()
        self.assertTrue(root.is_absolute())
        self.assertFalse(is_real_runtime_path(root))
        self.assertTrue(root.name.startswith(hermetic_runtime.TEST_RUNTIME_PREFIX))

    def test_stale_test_runtime_roots_are_swept_but_live_ones_are_kept(self):
        """Windows keeps open sqlite handles locked at exit, so cleanup carries over."""
        temp_root = Path(tempfile.gettempdir())
        stale = temp_root / f"{hermetic_runtime.TEST_RUNTIME_PREFIX}stale-fixture"
        fresh = temp_root / f"{hermetic_runtime.TEST_RUNTIME_PREFIX}fresh-fixture"
        stale.mkdir(exist_ok=True)
        fresh.mkdir(exist_ok=True)
        try:
            old = time.time() - hermetic_runtime.STALE_TEST_RUNTIME_SECONDS - 60
            os.utime(stale, (old, old))
            hermetic_runtime._sweep_stale_test_runtimes(self._bridge_root())
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
        finally:
            for folder in (stale, fresh):
                if folder.exists():
                    folder.rmdir()

    def test_app_ui_bridge_resolves_only_isolated_paths(self):
        import app_ui

        bridge = app_ui.BRIDGE
        for path in (
            bridge.runtime_root, bridge.output_root,
            bridge.profile_root, bridge.profile_media_root,
        ):
            self.assertFalse(is_real_runtime_path(path), path)
        self.assertEqual(Path(bridge.store.db_path).parent, bridge.runtime_root)

    def test_an_isolated_store_sees_only_its_own_jobs(self):
        with tempfile.TemporaryDirectory(prefix="tradutor-isolation-") as folder:
            db = Path(folder) / "jobs.sqlite3"
            store = JobStore(db)
            try:
                job_id = store.create_job(
                    source_url="", output_dir=str(Path(folder) / "out"),
                    command=["offline-fixture"],
                    configuration={"job_type": "translation", "fixture": True},
                    initial_status=JobStatus.QUEUED,
                )
                listed = [job["id"] for job in store.list_jobs()]
                self.assertEqual(listed, [job_id])  # never a job from the real queue
                store.transition(job_id, JobStatus.CANCELLED)
                self.assertEqual(store.get_job(job_id)["status"], JobStatus.CANCELLED)
            finally:
                store.close()
            self.assertTrue(db.exists())
        self.assertFalse(Path(folder).exists())  # cleanup succeeds


class FailClosedConstructionTests(unittest.TestCase):
    def test_ui_bridge_refuses_to_build_without_an_isolated_root_inside_tests(self):
        environment = dict(os.environ)
        environment.pop("TRADUTOR_TEST_RUNTIME_ROOT", None)
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                ui_bridge.UiBridge()
        self.assertEqual(str(caught.exception), "hermetic_test_runtime_root_required")

    def test_production_runtime_resolution_uses_writable_user_data(self):
        """Outside the hermetic marker, resolution no longer points into the repo.

        Read-only source inspection: no bridge is constructed, so nothing is opened.
        """
        source = (REPO_ROOT / "ui_bridge.py").read_text(encoding="utf-8")
        self.assertIn('JOBS_DB_PATH = DEFAULT_RUNTIME_ROOT / "jobs.sqlite3"', source)
        self.assertIn('DEFAULT_RUNTIME_ROOT.resolve()', source)
        launcher = (REPO_ROOT / "start_tradutor.py").read_text(encoding="utf-8")
        self.assertIn('DB_PATH = RUNTIME_ROOT / "jobs.sqlite3"', launcher)

    def test_a_clean_production_interpreter_uses_user_data_runtime_root(self):
        environment = {
            key: value for key, value in os.environ.items()
            if key not in {
                "TRADUTOR_TEST_RUNTIME_ROOT",
                "TRADUTOR_IA_HERMETIC_TEST_ENV",
                "TRADUTOR_IA_OFFLINE_TEST_GUARD",
                "TRADUTOR_IA_RUNTIME_ISOLATION_GUARD",
                "TRADUTOR_USER_DATA_ROOT",
                "TRADUTOR_RUNTIME_ROOT",
                "PYTHONPATH",
            }
        }
        completed = subprocess.run(
            [
                sys.executable, "-c",
                "import runtime_paths;"
                "print(runtime_paths.runtime_root() == runtime_paths.default_user_data_root() / 'runtime')",
            ],
            cwd=REPO_ROOT, env=environment, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.strip().splitlines()[-1], "True")


class EntryPointHermeticityTests(unittest.TestCase):
    def test_unittest_discovery_child_inherits_the_isolated_runtime(self):
        """``python -m unittest`` gets the guard through ``sitecustomize``, not conftest."""
        environment = dict(os.environ)
        for key in (
            "TRADUTOR_TEST_RUNTIME_ROOT",
            "TRADUTOR_IA_HERMETIC_TEST_ENV",
            "TRADUTOR_IA_OFFLINE_TEST_GUARD",
        ):
            environment.pop(key, None)
        environment["PYTHONPATH"] = str(REPO_ROOT)
        completed = subprocess.run(
            [
                sys.executable, "-m", "unittest",
                "test_runtime_isolation_contract.IsolatedRuntimeVisibilityTests",
            ],
            cwd=REPO_ROOT, env=environment, capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
