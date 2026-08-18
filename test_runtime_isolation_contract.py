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
    REAL_RUNTIME_ROOT,
    RealRuntimeAccess,
    is_real_runtime_path,
)
from job_store import JobStatus, JobStore

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

    def test_the_module_default_still_names_the_real_db_so_the_guard_is_load_bearing(self):
        """Production semantics are unchanged; only the test process refuses the path."""
        self.assertEqual(ui_bridge.JOBS_DB_PATH, REAL_JOBS_DB)
        self.assertTrue(is_real_runtime_path(ui_bridge.JOBS_DB_PATH))


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

    def test_ensure_worker_cannot_start_the_real_worker(self):
        """``ensure_worker`` routes to ``start_tradutor.start_worker`` (real DB + real spawn).

        It reports the failure instead of raising, so assert the refusal was recorded rather
        than a worker having been started.
        """
        def total() -> int:
            return sum(len(values) for values in hermetic_runtime.ATTEMPTS.values())

        before = total()
        import start_tradutor

        with self.assertRaises(RealRuntimeAccess):
            start_tradutor.start_worker()
        self.assertGreater(total(), before)


class IsolatedRuntimeVisibilityTests(unittest.TestCase):
    """Real SQLite semantics against an isolated path — not a mock (mission §18)."""

    def _bridge_root(self) -> Path:
        return Path(os.environ["TRADUTOR_TEST_RUNTIME_ROOT"])

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

    def test_production_runtime_resolution_semantics_are_unchanged(self):
        """Outside the hermetic marker, resolution still points at the real runtime.

        Read-only source inspection: no bridge is constructed, so nothing is opened.
        """
        source = (REPO_ROOT / "ui_bridge.py").read_text(encoding="utf-8")
        self.assertIn('JOBS_DB_PATH = REPO_ROOT / ".cache" / "runtime" / "jobs.sqlite3"', source)
        self.assertIn('(REPO_ROOT / ".cache" / "runtime").resolve()', source)
        launcher = (REPO_ROOT / "start_tradutor.py").read_text(encoding="utf-8")
        self.assertIn('DB_PATH = REPO_ROOT / ".cache" / "runtime" / "jobs.sqlite3"', launcher)

    def test_a_clean_production_interpreter_keeps_the_real_runtime_root(self):
        environment = {
            key: value for key, value in os.environ.items()
            if key not in {
                "TRADUTOR_TEST_RUNTIME_ROOT",
                "TRADUTOR_IA_HERMETIC_TEST_ENV",
                "TRADUTOR_IA_OFFLINE_TEST_GUARD",
                "PYTHONPATH",
            }
        }
        completed = subprocess.run(
            [
                sys.executable, "-c",
                "import ui_bridge, hermetic_runtime;"
                "print(hermetic_runtime.is_real_runtime_path(ui_bridge.JOBS_DB_PATH))",
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
