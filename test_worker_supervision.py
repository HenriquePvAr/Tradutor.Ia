"""Bounded launcher supervision of the worker process: respawn, backoff, crash-loop stop.

TDD #52 made worker death *observable* (fresh heartbeat is not liveness; the process is).
It did not make worker availability *recoverable*: the launcher dropped the ``Popen`` it
created, so nothing ever noticed the exit and nothing ever started a replacement.

These tests pin the smallest safe supervisor: it owns process availability only. Job
state, ownership, interruption and resume stay with JobStore and the worker's own startup
path - the replacement worker recovers work through the normal queue, never through the
launcher. Everything runs against isolated runtimes and isolated child processes; the real
queue, worker, UI and runtime root are never touched.
"""

import _test_bootstrap  # noqa: F401
from _test_processes import assert_owned_pid, stop_owned_process

import itertools
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

import hermetic_runtime
import process_tree
import start_tradutor
import worker_supervisor
from job_store import JobStatus, JobStore

REPO = Path(__file__).resolve().parent


def _wait(predicate, *, timeout: float, label: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    print("TIMEOUT:", label)
    return False


class _Clock:
    """Controlled time: production sleeps for real, tests do not."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _FakeProcess:
    """A process handle whose exit is scripted; no child code ever runs."""

    _pids = itertools.count(90000)

    def __init__(self, clock, *, exit_code=1, uptime=0.0):
        self.pid = next(self._pids)
        self.returncode = None
        self._clock = clock
        self._exit_code = exit_code
        self._uptime = uptime

    def wait(self, timeout=None):
        del timeout
        self._clock.now += self._uptime
        self.returncode = self._exit_code
        return self._exit_code

    def poll(self):
        return self.returncode


class _Events(list):
    def __call__(self, event, **details):
        self.append((event, details))

    def names(self):
        return [name for name, _ in self]


class RestartPolicyTests(unittest.TestCase):
    """The bounded policy itself, driven by a fake clock instead of real delays."""

    def setUp(self):
        self.clock = _Clock()
        self.events = _Events()

    def _supervisor(self, spawn, **kwargs):
        kwargs.setdefault("clock", self.clock)
        kwargs.setdefault("sleep", self.clock.sleep)
        kwargs.setdefault("log", self.events)
        return worker_supervisor.WorkerSupervisor(spawn, **kwargs)

    def _crash_forever(self, *, exit_code=1, uptime=0.0):
        def spawn():
            return _FakeProcess(self.clock, exit_code=exit_code, uptime=uptime)

        return spawn

    def test_a_single_crash_is_replaced_exactly_once(self):
        made: list[_FakeProcess] = []

        def spawn():
            # First replacement survives: its wait() never returns during the test.
            process = _FakeProcess(self.clock, exit_code=0)
            process.wait = lambda timeout=None: supervisor.request_stop() or 0
            made.append(process)
            return process

        first = _FakeProcess(self.clock, exit_code=9)
        supervisor = self._supervisor(spawn)
        supervisor.run(first)

        self.assertEqual(supervisor.restarts, 1)
        self.assertEqual(supervisor.spawn_count, 1, "exactly one replacement")
        self.assertEqual(len(made), 1)
        self.assertIsNot(made[0], first, "replacement must be a new process instance")
        self.assertNotEqual(made[0].pid, first.pid)
        self.assertEqual(self.clock.sleeps, [worker_supervisor.BACKOFF_SECONDS[0]])
        self.assertIn("worker_process_exited", self.events.names())
        self.assertIn("worker_restart_scheduled", self.events.names())

    def test_a_crash_loop_stops_after_a_finite_budget(self):
        supervisor = self._supervisor(self._crash_forever())
        state = supervisor.run()

        self.assertEqual(state, worker_supervisor.DEGRADED)
        self.assertEqual(supervisor.state, worker_supervisor.DEGRADED)
        self.assertEqual(supervisor.restarts, worker_supervisor.MAX_RESTARTS)
        self.assertEqual(supervisor.spawn_count, worker_supervisor.MAX_RESTARTS + 1)
        self.assertEqual(supervisor.backoff_calls, list(worker_supervisor.BACKOFF_SECONDS))
        self.assertEqual(self.events.names().count("worker_restart_exhausted"), 1)

    def test_backoff_is_never_a_busy_loop(self):
        delays = worker_supervisor.BACKOFF_SECONDS
        self.assertTrue(delays, "a restart policy needs at least one backoff step")
        self.assertGreaterEqual(min(delays), 1.0, "sub-second respawn is a process storm")
        self.assertEqual(list(delays), sorted(delays), "backoff must not shrink")
        supervisor = self._supervisor(self._crash_forever())
        supervisor.run()
        # A worker that dies instantly must not produce hundreds of processes.
        self.assertLessEqual(supervisor.spawn_count, 8)
        self.assertGreaterEqual(self.clock.now, sum(delays))

    def test_a_stable_run_earns_the_restart_budget_back(self):
        # First replacement survives past the stability interval, then crashes anyway.
        uptimes = iter([worker_supervisor.STABILITY_SECONDS + 1.0])

        def spawn():
            return _FakeProcess(self.clock, exit_code=1, uptime=next(uptimes, 0.0))

        first = _FakeProcess(self.clock, exit_code=1, uptime=0.0)
        supervisor = self._supervisor(spawn)
        supervisor.run(first)

        self.assertEqual(supervisor.state, worker_supervisor.DEGRADED)
        # Without the reset the whole run would fit in one budget (MAX_RESTARTS spawns);
        # the stable run in the middle hands the budget back, buying one more.
        self.assertEqual(supervisor.spawn_count, worker_supervisor.MAX_RESTARTS + 1)
        self.assertEqual(
            supervisor.backoff_calls,
            [worker_supervisor.BACKOFF_SECONDS[0]] + list(worker_supervisor.BACKOFF_SECONDS),
        )

    def test_without_a_stable_run_the_budget_is_not_handed_back(self):
        # The discriminator for the test above: same shape, no stable run, smaller budget.
        supervisor = self._supervisor(
            lambda: _FakeProcess(self.clock, exit_code=1, uptime=0.0))
        supervisor.run(_FakeProcess(self.clock, exit_code=1, uptime=0.0))
        self.assertEqual(supervisor.spawn_count, worker_supervisor.MAX_RESTARTS)

    def test_exit_code_zero_is_not_proof_of_an_intentional_stop(self):
        supervisor = self._supervisor(self._crash_forever(exit_code=0))
        supervisor.run()
        self.assertEqual(supervisor.state, worker_supervisor.DEGRADED)
        self.assertEqual(supervisor.restarts, worker_supervisor.MAX_RESTARTS)
        self.assertNotIn("worker_expected_stop", self.events.names())

    def test_an_intentional_stop_never_respawns(self):
        supervisor = self._supervisor(self._crash_forever(exit_code=0))
        first = _FakeProcess(self.clock, exit_code=0)
        supervisor.request_stop()
        self.assertEqual(supervisor.state, worker_supervisor.STOPPING)
        state = supervisor.run(first)

        self.assertEqual(state, worker_supervisor.STOPPING)
        self.assertEqual(supervisor.spawn_count, 0, "no replacement was ever created")
        self.assertEqual(supervisor.restarts, 0)
        self.assertEqual(supervisor.backoff_calls, [])
        self.assertIn("worker_expected_stop", self.events.names())
        self.assertNotIn("worker_process_exited", self.events.names())
        self.assertNotIn("worker_restart_scheduled", self.events.names())

    def test_a_stop_during_backoff_ends_stopping_not_degraded(self):
        supervisor = self._supervisor(self._crash_forever())

        def sleep(seconds):
            self.clock.sleep(seconds)
            supervisor.request_stop()

        supervisor._sleep = sleep
        state = supervisor.run()
        self.assertEqual(state, worker_supervisor.STOPPING)
        self.assertEqual(supervisor.spawn_count, 1)
        self.assertNotIn("worker_restart_exhausted", self.events.names())

    def test_a_failing_spawn_is_bounded_and_diagnosed_apart(self):
        def spawn():
            raise OSError(8, "exec format error")

        supervisor = self._supervisor(spawn)
        state = supervisor.run()

        self.assertEqual(state, worker_supervisor.DEGRADED)
        self.assertEqual(supervisor.spawn_count, 0, "no child was ever created")
        self.assertEqual(supervisor.backoff_calls, list(worker_supervisor.BACKOFF_SECONDS))
        names = self.events.names()
        self.assertEqual(names.count("worker_spawn_failed"), worker_supervisor.MAX_RESTARTS + 1)
        self.assertNotIn("worker_process_exited", names)
        failure = next(details for name, details in self.events if name == "worker_spawn_failed")
        self.assertEqual(failure["error_type"], "OSError")

    def test_diagnostics_carry_no_secrets(self):
        supervisor = self._supervisor(self._crash_forever(exit_code=3))
        supervisor.run()
        allowed = {
            "attempt", "delay", "exit_code", "generation", "pid", "reason",
            "restarts", "uptime", "error_type",
        }
        for name, details in self.events:
            self.assertLessEqual(set(details), allowed, name)

    def test_health_is_representable_across_the_whole_sequence(self):
        supervisor = self._supervisor(self._crash_forever())
        seen = [supervisor.state]
        original = supervisor._sleep
        supervisor._sleep = lambda s: (seen.append(supervisor.state), original(s))[1]
        supervisor.run()
        seen.append(supervisor.state)
        self.assertIn(worker_supervisor.BACKOFF, seen)
        self.assertEqual(seen[-1], worker_supervisor.DEGRADED)


class LauncherOwnershipTests(unittest.TestCase):
    """What the launcher may supervise, and what it may never do to a job."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "jobs.sqlite3"
        self._patch = patch.object(start_tradutor, "DB_PATH", self.db)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_the_launcher_retains_the_process_handle_it_created(self):
        with patch.object(start_tradutor.subprocess, "Popen") as popen, \
                patch.object(start_tradutor.time, "sleep", lambda *_: None):
            handle = start_tradutor.start_worker()
        self.assertIs(handle, popen.return_value, "the Popen object must not be discarded")

    def test_a_pre_existing_healthy_worker_is_neither_duplicated_nor_supervised(self):
        store = JobStore(self.db)
        store.register_worker("existing", 4321)
        store.close()
        with patch.object(start_tradutor.subprocess, "Popen") as popen, \
                patch.object(start_tradutor, "supervise_worker") as supervise, \
                patch.object(start_tradutor, "start_ui", return_value=0):
            self.assertEqual(start_tradutor.main(["all"]), 0)
        popen.assert_not_called()
        supervise.assert_not_called()

    def test_the_launcher_supervises_only_the_worker_it_started(self):
        with patch.object(start_tradutor.subprocess, "Popen") as popen, \
                patch.object(start_tradutor.time, "sleep", lambda *_: None), \
                patch.object(start_tradutor, "supervise_worker") as supervise, \
                patch.object(start_tradutor, "start_ui", return_value=0):
            self.assertEqual(start_tradutor.main(["all"]), 0)
        supervise.assert_called_once_with(popen.return_value)

    def test_an_intentional_stop_marks_the_supervisor_before_terminating(self):
        store = JobStore(self.db)
        store.register_worker("existing", 4321)
        store.close()
        supervisor = worker_supervisor.WorkerSupervisor(lambda: None)
        # PID 4321 matches no worker process, so stop_worker signals nothing; the point is
        # that intent is recorded before any termination is even attempted.
        with patch.object(start_tradutor, "_SUPERVISOR", supervisor):
            start_tradutor.stop_worker()
        self.assertEqual(supervisor.state, worker_supervisor.STOPPING)

    def test_the_launcher_never_touches_job_state(self):
        forbidden = ("resume(", "mark_interrupted(", "claim_next_job(", "transition(")
        for module in (worker_supervisor, start_tradutor):
            source = Path(module.__file__).read_text(encoding="utf-8")
            for name in forbidden:
                self.assertNotIn(name, source, f"{module.__name__} must not touch job state")


class RealProcessSupervisionTests(unittest.TestCase):
    """Real isolated children that really exit; no mock stands in for the decisive path."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "jobs.sqlite3"
        self.store = JobStore(self.db)
        self._children: list[subprocess.Popen] = []
        self._supervisors: list[worker_supervisor.WorkerSupervisor] = []

    def tearDown(self):
        for supervisor in self._supervisors:
            supervisor.request_stop()
        for proc in self._children:
            if proc.poll() is None:
                try:
                    stop_owned_process(proc)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
        for supervisor in self._supervisors:
            thread = getattr(supervisor, "thread", None)
            if thread is not None:
                thread.join(timeout=20)
        self.store.close()

    _SCRIPT = textwrap.dedent(
        """
        import os, sys, time
        sys.path.insert(0, %r)
        import _test_bootstrap  # noqa: F401
        import process_tree
        from job_store import JobStore
        store = JobStore(%r)
        snapshot = process_tree.snapshot(os.getpid()) or {}
        store.register_worker(sys.argv[1], os.getpid(),
                              create_time=snapshot.get("create_time"))
        store.worker_heartbeat(sys.argv[1])
        store.close()
        if sys.argv[2] == "crash":
            os._exit(9)
        time.sleep(600)
        """
    )

    def _spawn_child(self, name, mode):
        script = self._SCRIPT % (str(REPO), str(self.db))
        proc = subprocess.Popen(
            [sys.executable, "-c", script, name, mode], cwd=str(REPO),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._children.append(proc)
        return proc

    def test_a_real_hard_exit_is_observed_and_replaced(self):
        names = iter(["worker-b", "worker-c"])

        def spawn():
            return self._spawn_child(next(names), "sleep")

        worker_a = self._spawn_child("worker-a", "crash")
        self.assertEqual(worker_a.wait(timeout=60), 9, "child did not hard-exit")
        self.assertTrue(process_tree.wait_gone(worker_a.pid, timeout=15))
        self.assertIsNone(self.store.healthy_worker(stale_seconds=15))

        supervisor = worker_supervisor.supervise(spawn, worker_a, backoff=(0.05,))
        self._supervisors.append(supervisor)
        self.assertTrue(_wait(lambda: supervisor.state == worker_supervisor.RUNNING,
                              timeout=60, label="no replacement became the running worker"))

        worker_b = supervisor.process
        self.assertEqual(supervisor.restarts, 1)
        self.assertEqual(supervisor.spawn_count, 1)
        self.assertIsNot(worker_b, worker_a)
        self.assertNotEqual(worker_b.pid, worker_a.pid)
        self.assertIsNone(worker_b.poll(), "replacement is not alive")
        self.assertTrue(_wait(lambda: self.store.healthy_worker(stale_seconds=15) is not None,
                              timeout=30, label="health never returned"))
        healthy = self.store.healthy_worker(stale_seconds=15)
        self.assertEqual(healthy["worker_id"], "worker-b")
        assert_owned_pid(self, worker_b, healthy["pid"])
        self.assertNotEqual(healthy["create_time"],
                            (self.store.get_worker("worker-a") or {}).get("create_time"))

    def test_a_real_intentional_stop_produces_no_replacement(self):
        def spawn():
            raise AssertionError("an intentional stop must never spawn a replacement")

        worker_a = self._spawn_child("worker-a", "sleep")
        supervisor = worker_supervisor.supervise(spawn, worker_a, backoff=(0.05,))
        self._supervisors.append(supervisor)
        self.assertTrue(_wait(lambda: supervisor.state == worker_supervisor.RUNNING,
                              timeout=30, label="worker never reached running"))

        supervisor.request_stop()          # launcher declares intent *before* terminating
        stop_owned_process(worker_a)

        supervisor.thread.join(timeout=30)
        self.assertFalse(supervisor.thread.is_alive())
        self.assertEqual(supervisor.state, worker_supervisor.STOPPING)
        self.assertEqual(supervisor.spawn_count, 0, "no replacement was ever created")
        self.assertEqual(supervisor.restarts, 0)

    def test_a_real_crash_loop_stops_spawning(self):
        spawned: list[subprocess.Popen] = []

        def spawn():
            proc = self._spawn_child(f"crasher-{len(spawned)}", "crash")
            spawned.append(proc)
            return proc

        worker_a = self._spawn_child("crasher-a", "crash")
        supervisor = worker_supervisor.supervise(
            spawn, worker_a, backoff=(0.05, 0.05, 0.05))
        self._supervisors.append(supervisor)
        supervisor.thread.join(timeout=180)
        self.assertFalse(supervisor.thread.is_alive(), "supervisor never stopped respawning")

        self.assertEqual(supervisor.state, worker_supervisor.DEGRADED)
        self.assertEqual(len(spawned), worker_supervisor.MAX_RESTARTS)
        before = len(spawned)
        time.sleep(1.0)
        self.assertEqual(len(spawned), before, "a degraded supervisor kept spawning")
        for proc in spawned:
            self.assertIsNotNone(proc.poll(), "crash-loop child survived")
        self.assertIsNone(self.store.healthy_worker(stale_seconds=15))


class SupervisedRecoveryIntegrationTests(unittest.TestCase):
    """A supervised replacement recovers work through the normal worker path (TDD #52)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "jobs.sqlite3"
        self.store = JobStore(self.db)
        self._workers: list[tuple[subprocess.Popen, object]] = []
        self._supervisors: list[worker_supervisor.WorkerSupervisor] = []

    def tearDown(self):
        for supervisor in self._supervisors:
            supervisor.request_stop()
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
        for supervisor in self._supervisors:
            thread = getattr(supervisor, "thread", None)
            if thread is not None:
                thread.join(timeout=20)
        self.store.close()

    def _start_worker(self, name):
        log = (self.tmp / name).open("wb")
        proc = subprocess.Popen(
            [sys.executable, "-u", str(REPO / "worker_service.py"),
             "--db", str(self.db), "--poll-interval", "0.2"],
            cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        self._workers.append((proc, log))
        return proc

    def _fake_command(self, output_dir):
        return [sys.executable, "-u", str(REPO / "fake_pipeline.py"),
                "--output-dir", str(output_dir), "--outcome", "finished",
                "--steps", "40", "--sleep", "0.3", "--hang"]

    def test_supervised_replacement_recovers_the_dead_workers_job(self):
        out = self.tmp / "chapter"
        (out / "checkpoints").mkdir(parents=True, exist_ok=True)
        (out / "checkpoints" / "download.done").write_text('{"stage": "download"}',
                                                           encoding="utf-8")
        running_id = self.store.create_job(
            source_url="https://example/x", output_dir=str(out),
            command=self._fake_command(out), run_id="sup-53")
        queued_id = self.store.create_job(
            source_url="https://example/y", output_dir=str(self.tmp / "c2"),
            command=self._fake_command(self.tmp / "c2"), run_id="sup-53-queued")
        cancelled_id = self.store.create_job(
            source_url="https://example/z", output_dir=str(self.tmp / "c3"),
            command=self._fake_command(self.tmp / "c3"), run_id="sup-53-cancelled")
        self.store.transition(cancelled_id, JobStatus.CANCELLED)

        worker_a = self._start_worker("a.out")
        self.assertTrue(_wait(
            lambda: self.store.get_job(running_id)["status"] == JobStatus.RUNNING,
            timeout=90, label="job never reached running"))
        runner_pid = self.store.get_job(running_id)["runner_pid"]
        self.assertTrue(runner_pid)

        # HARD CRASH of worker A and its runner tree: no cleanup path at all.
        tree = [runner_pid]
        try:
            tree += [c.pid for c in psutil.Process(runner_pid).children(recursive=True)]
        except psutil.Error:
            pass
        for pid in [worker_a.pid, *tree]:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
        worker_a.wait(timeout=30)
        for pid in tree:
            self.assertTrue(process_tree.wait_gone(pid, timeout=15), f"pid {pid} survived")
        self.assertIsNone(self.store.healthy_worker(stale_seconds=15))

        supervisor = worker_supervisor.supervise(
            lambda: self._start_worker("b.out"), worker_a, backoff=(0.05,))
        self._supervisors.append(supervisor)
        self.assertTrue(_wait(lambda: supervisor.state == worker_supervisor.RUNNING,
                              timeout=60, label="replacement worker never started"))
        self.assertEqual(supervisor.restarts, 1)
        worker_b = supervisor.process
        self.assertNotEqual(worker_b.pid, worker_a.pid)

        # The replacement recovers the job through its own normal startup path.
        self.assertTrue(_wait(
            lambda: self.store.get_job(running_id)["status"] != JobStatus.RUNNING,
            timeout=90, label="crashed job stayed eternally running"))
        recovered = self.store.get_job(running_id)
        self.assertIn(recovered["status"], (JobStatus.INTERRUPTED, JobStatus.QUEUED))
        self.assertNotIn(recovered["status"], JobStatus.TERMINAL)
        self.assertTrue((out / "checkpoints" / "download.done").is_file(),
                        "checkpoint lost across the supervised restart")
        self.assertEqual(self.store.get_job(cancelled_id)["status"], JobStatus.CANCELLED)
        self.assertTrue(_wait(
            lambda: self.store.healthy_worker(stale_seconds=15) is not None,
            timeout=60, label="health never returned after the supervised restart"))
        healthy = self.store.healthy_worker(stale_seconds=15)
        assert_owned_pid(self, worker_b, healthy["pid"])
        self.assertIsNotNone(self.store.get_job(queued_id))


class HermeticIsolationRegressionTests(unittest.TestCase):
    """TDD #51 isolation must never be bypassed by the supervision tests added here."""

    def test_supervision_tests_run_against_an_isolated_runtime(self):
        root = os.environ.get(hermetic_runtime.TEST_RUNTIME_ROOT_ENV, "")
        self.assertTrue(root, "hermetic runtime root is not installed")
        self.assertFalse(hermetic_runtime.is_real_runtime_path(root))

    def test_this_module_cannot_reach_the_real_queue(self):
        with self.assertRaises(hermetic_runtime.RealRuntimeAccess):
            JobStore(hermetic_runtime.REAL_JOBS_DB)

    def test_this_module_cannot_launch_the_real_launcher(self):
        with self.assertRaises(hermetic_runtime.RealRuntimeAccess):
            subprocess.Popen([sys.executable, str(REPO / "start_tradutor.py")])


if __name__ == "__main__":
    unittest.main()
