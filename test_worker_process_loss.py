"""Worker hard-crash detection, dead-worker job reconciliation and safe recovery.

A worker that dies by ``os._exit``, a forced termination, a native crash or an OOM kill
never runs its own ``finally``: its lease row survives the process. Heartbeat freshness
alone therefore cannot see the death, and for a whole stale window the product reports a
worker that no longer exists - refusing to start a replacement, refusing to reconcile, and
answering "online" from the health surface.

These tests use real, isolated child processes that actually exit, never the real runtime:
an isolated SQLite database per test, the TDD #51 hermetic guard, and every Popen handle
closed in ``tearDown``. No real worker, UI, launcher, provider, network or Drive is touched.
"""

import _test_bootstrap  # noqa: F401
from _test_processes import assert_owned_pid, stop_owned_process

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

import psutil

import hermetic_runtime
import process_tree
import ui_bridge
from job_store import JobStatus, JobStore

REPO = Path(__file__).resolve().parent


def _wait(predicate, *, timeout: float, label: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    print("TIMEOUT:", label)
    return False


def _fake_command(output_dir: Path, *, steps: int = 40, sleep: float = 0.3, hang: bool = True):
    command = [sys.executable, "-u", str(REPO / "fake_pipeline.py"),
               "--output-dir", str(output_dir), "--outcome", "finished",
               "--steps", str(steps), "--sleep", str(sleep)]
    if hang:
        command.append("--hang")
    return command


class _Bridge(ui_bridge.UiBridge):
    """UiBridge with only the job store wired - no profile/history side effects."""

    def __init__(self, db_path):
        self.store = JobStore(db_path)
        self.history_revision = 1

    def _is_translation_job(self, job):
        return bool(job)


class WorkerLeaseIdentityTests(unittest.TestCase):
    """The liveness contract itself, without needing a full worker run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = JobStore(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.store.close()

    def _self_identity(self):
        snapshot = process_tree.snapshot(os.getpid()) or {}
        return os.getpid(), snapshot.get("create_time")

    def test_live_worker_instance_is_healthy(self):
        pid, create_time = self._self_identity()
        self.store.register_worker("w-live", pid, create_time=create_time)
        healthy = self.store.healthy_worker(stale_seconds=15)
        self.assertIsNotNone(healthy)
        self.assertEqual(healthy["worker_id"], "w-live")

    def test_reused_pid_is_not_the_expected_worker_instance(self):
        # The PID is genuinely alive (it is this very process), but it is not the process
        # that took the lease. Bare PID existence must never be enough.
        pid, _ = self._self_identity()
        self.store.register_worker("w-old", pid, create_time=1.0)
        self.assertIsNone(self.store.healthy_worker(stale_seconds=15))

    def test_dead_pid_with_a_fresh_heartbeat_is_not_healthy(self):
        gone = _dead_pid()
        self.store.register_worker("w-dead", gone, create_time=time.time())
        self.store.worker_heartbeat("w-dead")
        self.assertIsNone(self.store.healthy_worker(stale_seconds=15))

    def test_unverifiable_lease_keeps_the_heartbeat_contract(self):
        # A lease that never recorded a process identity carries no evidence to check;
        # it keeps the pre-existing heartbeat-only semantics rather than failing closed
        # on a worker that may well be alive.
        self.store.register_worker("w-legacy", 4321)
        self.assertIsNotNone(self.store.healthy_worker(stale_seconds=15))

    def test_a_dead_newest_lease_does_not_mask_a_live_worker(self):
        pid, create_time = self._self_identity()
        self.store.register_worker("w-live", pid, create_time=create_time)
        time.sleep(0.01)
        self.store.register_worker("w-dead", _dead_pid(), create_time=time.time())
        healthy = self.store.healthy_worker(stale_seconds=15)
        self.assertIsNotNone(healthy)
        self.assertEqual(healthy["worker_id"], "w-live")

    def test_graceful_shutdown_is_not_a_crash(self):
        pid, create_time = self._self_identity()
        self.store.register_worker("w-live", pid, create_time=create_time)
        self.store.unregister_worker("w-live")
        self.assertIsNone(self.store.healthy_worker(stale_seconds=15))
        # And with no job in flight there is nothing to reconcile.
        self.assertEqual(
            self.store.orphaned_in_flight_jobs(exclude_worker="", worker_stale_seconds=15), [])


class DeadWorkerOrphanTests(unittest.TestCase):
    """Job ownership reconciliation must follow the same liveness contract."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = JobStore(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.store.close()

    def _in_flight_job(self, worker_id, pid, create_time):
        job_id = self.store.create_job(
            source_url="https://example/x", output_dir=str(self.tmp / "chapter"),
            command=["python", "run_webtoon.py"], configuration={"job_type": "translation"})
        self.store.transition(job_id, JobStatus.CLAIMING, worker_id=worker_id,
                              worker_pid=pid, worker_create_time=create_time)
        self.store.transition(job_id, JobStatus.STARTING)
        self.store.transition(job_id, JobStatus.RUNNING)
        return job_id

    def test_fresh_lease_of_a_dead_worker_still_orphans_its_job(self):
        # The crash is what matters, not how recently the corpse heartbeated. Waiting out
        # the stale window before reconciling leaves the job falsely running meanwhile.
        gone = _dead_pid()
        self.store.register_worker("w-dead", gone, create_time=time.time())
        self.store.worker_heartbeat("w-dead")
        job_id = self._in_flight_job("w-dead", gone, time.time())
        orphans = self.store.orphaned_in_flight_jobs(
            exclude_worker="w-other", worker_stale_seconds=15)
        self.assertEqual([job["id"] for job in orphans], [job_id])

    def test_live_worker_keeps_its_own_job(self):
        pid = os.getpid()
        create_time = (process_tree.snapshot(pid) or {}).get("create_time")
        self.store.register_worker("w-live", pid, create_time=create_time)
        self.store.worker_heartbeat("w-live")
        self._in_flight_job("w-live", pid, create_time)
        self.assertEqual(
            self.store.orphaned_in_flight_jobs(
                exclude_worker="w-other", worker_stale_seconds=15),
            [])

    def test_a_worker_never_orphans_the_job_it_owns(self):
        pid = os.getpid()
        create_time = (process_tree.snapshot(pid) or {}).get("create_time")
        self.store.register_worker("w-self", pid, create_time=create_time)
        self._in_flight_job("w-self", pid, create_time)
        self.assertEqual(
            self.store.orphaned_in_flight_jobs(
                exclude_worker="w-self", worker_stale_seconds=15),
            [])

    def test_queued_work_is_never_orphaned_by_a_worker_death(self):
        gone = _dead_pid()
        self.store.register_worker("w-dead", gone, create_time=time.time())
        queued_id = self.store.create_job(
            source_url="https://example/y", output_dir=str(self.tmp / "c2"),
            command=["python", "run_webtoon.py"], configuration={"job_type": "translation"})
        orphans = self.store.orphaned_in_flight_jobs(
            exclude_worker="w-other", worker_stale_seconds=15)
        self.assertEqual(orphans, [])
        self.assertEqual(self.store.get_job(queued_id)["status"], JobStatus.QUEUED)

    def test_terminal_jobs_are_never_reopened_by_a_dead_lease(self):
        gone = _dead_pid()
        self.store.register_worker("w-dead", gone, create_time=time.time())
        for target in (JobStatus.FINISHED, JobStatus.CANCELLED, JobStatus.FAILED):
            job_id = self._in_flight_job("w-dead", gone, time.time())
            if target == JobStatus.CANCELLED:
                self.store.transition(job_id, JobStatus.CANCELLING)
            self.store.transition(job_id, target)
            self.assertNotIn(
                job_id,
                [job["id"] for job in self.store.orphaned_in_flight_jobs(
                    exclude_worker="w-other", worker_stale_seconds=15)],
                target,
            )
            self.assertEqual(self.store.get_job(job_id)["status"], target)


def _dead_pid() -> int:
    """A PID that certainly belongs to no live process: spawn a child and let it exit."""
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    proc.wait(timeout=30)
    process_tree.wait_gone(proc.pid, timeout=10)
    return proc.pid


class HardExitProcessTests(unittest.TestCase):
    """Real process-level death, never a mocked boolean."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "jobs.sqlite3"
        self.store = JobStore(self.db)
        self._children: list[subprocess.Popen] = []

    def tearDown(self):
        for proc in self._children:
            if proc.poll() is None:
                try:
                    stop_owned_process(proc)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
        self.store.close()

    def test_worker_that_os_exits_is_reported_dead_immediately(self):
        # The child registers a lease with its real process identity, heartbeats, and then
        # leaves by os._exit: no finally, no unregister, no exception handler. Exactly what
        # a segfault, an OOM kill or a forced termination looks like to the database.
        script = textwrap.dedent(
            """
            import os, sys
            sys.path.insert(0, %r)
            import _test_bootstrap  # noqa: F401
            import process_tree
            from job_store import JobStore
            store = JobStore(%r)
            snapshot = process_tree.snapshot(os.getpid()) or {}
            store.register_worker("crashed", os.getpid(),
                                  create_time=snapshot.get("create_time"))
            store.worker_heartbeat("crashed")
            print(os.getpid(), flush=True)
            os._exit(9)
            """
        ) % (str(REPO), str(self.db))
        proc = subprocess.Popen([sys.executable, "-c", script], cwd=str(REPO),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        self._children.append(proc)
        self.assertEqual(proc.wait(timeout=60), 9, "child did not hard-exit")
        self.assertTrue(process_tree.wait_gone(proc.pid, timeout=15))

        lease = self.store.get_worker("crashed")
        self.assertIsNotNone(lease, "the lease row survives the process, as expected")
        self.assertEqual(lease["pid"], int(proc.stdout.read().strip()))
        proc.stdout.close()
        self.assertTrue(process_tree.wait_gone(lease["pid"], timeout=15))
        # Heartbeat freshness says "alive"; the process says otherwise, and the process wins.
        self.assertGreater(lease["heartbeat_at"], time.time() - 15)
        self.assertIsNone(self.store.healthy_worker(stale_seconds=15))

    def test_replacement_worker_gets_a_new_identity_and_restores_health(self):
        script = textwrap.dedent(
            """
            import os, sys
            sys.path.insert(0, %r)
            import _test_bootstrap  # noqa: F401
            import process_tree
            from job_store import JobStore
            store = JobStore(%r)
            snapshot = process_tree.snapshot(os.getpid()) or {}
            store.register_worker(sys.argv[1], os.getpid(),
                                  create_time=snapshot.get("create_time"))
            store.worker_heartbeat(sys.argv[1])
            os._exit(7)
            """
        ) % (str(REPO), str(self.db))
        proc = subprocess.Popen([sys.executable, "-c", script, "worker-a"], cwd=str(REPO),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self._children.append(proc)
        self.assertEqual(proc.wait(timeout=60), 7)
        self.assertTrue(process_tree.wait_gone(proc.pid, timeout=15))
        self.assertIsNone(self.store.healthy_worker(stale_seconds=15))

        # A replacement registers its own instance; the stale lease is not reused.
        pid = os.getpid()
        create_time = (process_tree.snapshot(pid) or {}).get("create_time")
        self.store.register_worker("worker-b", pid, create_time=create_time)
        healthy = self.store.healthy_worker(stale_seconds=15)
        self.assertIsNotNone(healthy)
        self.assertEqual(healthy["worker_id"], "worker-b")
        self.assertNotEqual(healthy["worker_id"], "worker-a")


class WorkerCrashRecoveryIntegrationTests(unittest.TestCase):
    """A real worker process claims a real job, then dies without cleanup."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "jobs.sqlite3"
        self.store = JobStore(self.db)
        self._workers: list[tuple[subprocess.Popen, object]] = []
        self._extra_stores: list[JobStore] = []

    def tearDown(self):
        for proc, log in self._workers:
            if proc.poll() is None:
                process_tree.terminate_tree(
                    proc.pid, substrings=["worker_service.py"], timeout=10)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            log.close()
        for store in self._extra_stores:
            store.close()
        self.store.close()

    def _start_worker(self, name):
        log = (self.tmp / name).open("wb")
        proc = subprocess.Popen(
            [sys.executable, "-u", str(REPO / "worker_service.py"),
             "--db", str(self.db), "--poll-interval", "0.2"],
            cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        self._workers.append((proc, log))
        return proc

    def test_hard_killed_worker_leaves_no_eternal_running_job(self):
        out = self.tmp / "chapter"
        (out / "checkpoints").mkdir(parents=True, exist_ok=True)
        # A completed stage boundary that must survive the crash untouched.
        (out / "checkpoints" / "download.done").write_text('{"stage": "download"}',
                                                           encoding="utf-8")
        job_id = self.store.create_job(
            source_url="https://example/x", output_dir=str(out),
            command=_fake_command(out), run_id="crash-52")
        queued_id = self.store.create_job(
            source_url="https://example/y", output_dir=str(self.tmp / "c2"),
            command=_fake_command(self.tmp / "c2"), run_id="crash-52-queued")

        worker = self._start_worker("w1.out")
        self.assertTrue(_wait(
            lambda: self.store.get_job(job_id)["status"] == JobStatus.RUNNING,
            timeout=60, label="job never reached running"))
        job = self.store.get_job(job_id)
        runner_pid = job["runner_pid"]
        self.assertTrue(runner_pid, "runner pid was never persisted")
        self.assertIsNotNone(process_tree.snapshot(worker.pid), "worker process not alive")
        self.assertIsNotNone(process_tree.snapshot(runner_pid), "runner process not alive")
        self.assertIsNotNone(self.store.healthy_worker(stale_seconds=15),
                             "a live worker must be reported healthy before the crash")
        self.assertEqual(self.store.get_job(queued_id)["status"], JobStatus.QUEUED)

        # HARD CRASH: terminate the worker and its whole runner tree with no cleanup path,
        # the way an OOM kill or a machine-level process loss would.
        tree = [runner_pid]
        try:
            tree += [child.pid for child in psutil.Process(runner_pid).children(recursive=True)]
        except psutil.Error:
            pass
        try:
            psutil.Process(worker.pid).kill()
        except psutil.Error:
            pass
        for pid in tree:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
        worker.wait(timeout=30)
        for pid in tree:
            self.assertTrue(process_tree.wait_gone(pid, timeout=15), f"pid {pid} survived")

        # 1) Health tells the truth at once, with no stale window.
        self.assertIsNone(self.store.healthy_worker(stale_seconds=15))

        # 2) The active job is reconciled to an interrupted, recoverable state.
        bridge = _Bridge(self.db)
        self._extra_stores.append(bridge.store)
        reconciled = bridge.reconcile_orphans()
        self.assertEqual([entry["job_id"] for entry in reconciled], [job_id])
        recovered = self.store.get_job(job_id)
        self.assertEqual(recovered["status"], JobStatus.INTERRUPTED)
        self.assertTrue(recovered["recoverable"])
        self.assertNotIn(recovered["status"], JobStatus.TERMINAL)

        # 3) Unclaimed work is untouched.
        self.assertEqual(self.store.get_job(queued_id)["status"], JobStatus.QUEUED)

        # 4) The checkpoint written before the crash is intact.
        self.assertTrue((out / "checkpoints" / "download.done").is_file())

        # 5) Reconciliation is idempotent: no second transition, no extra attempt.
        attempt = recovered["attempt"]
        self.assertEqual(bridge.reconcile_orphans(), [])
        again = self.store.get_job(job_id)
        self.assertEqual(again["status"], JobStatus.INTERRUPTED)
        self.assertEqual(again["attempt"], attempt)

        # 6) A replacement worker starts instead of exiting on a dead worker's lease, and
        #    it takes a new identity rather than inheriting the crashed one.
        crashed_lease = self.store.get_worker(
            str(self.store.get_job(job_id).get("worker_id") or ""))
        replacement = self._start_worker("w2.out")
        self.assertTrue(_wait(
            lambda: self.store.healthy_worker(stale_seconds=15) is not None,
            timeout=60, label="replacement worker never became healthy"))
        self.assertIsNone(replacement.poll(), "replacement exited on a dead worker's lease")
        healthy = self.store.healthy_worker(stale_seconds=15)
        assert_owned_pid(self, replacement, healthy["pid"])
        if crashed_lease:
            self.assertNotEqual(healthy["worker_id"], crashed_lease["worker_id"])


class HermeticIsolationRegressionTests(unittest.TestCase):
    """TDD #51 isolation must never be bypassed by the crash tests added here."""

    def test_worker_loss_tests_run_against_an_isolated_runtime(self):
        root = os.environ.get(hermetic_runtime.TEST_RUNTIME_ROOT_ENV, "")
        self.assertTrue(root, "hermetic runtime root is not installed")
        self.assertFalse(hermetic_runtime.is_real_runtime_path(root))

    def test_this_module_cannot_reach_the_real_queue(self):
        with self.assertRaises(hermetic_runtime.RealRuntimeAccess):
            JobStore(hermetic_runtime.REAL_JOBS_DB)

    def test_this_module_cannot_launch_the_real_launcher(self):
        with self.assertRaises(hermetic_runtime.RealRuntimeAccess):
            subprocess.Popen([sys.executable, str(REPO / "start_tradutor.py")])

    def test_a_worker_subprocess_without_an_isolated_db_is_refused(self):
        with self.assertRaises(hermetic_runtime.RealRuntimeAccess):
            subprocess.Popen([sys.executable, str(REPO / "worker_service.py")])


if __name__ == "__main__":
    unittest.main()
