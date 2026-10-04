"""Bounded supervision of the worker process a launcher instance started.

Scope is deliberately one thing: **process availability**. TDD #52 made a worker's death
observable (a fresh heartbeat is not liveness; the live process with the same identity is),
but nothing restored availability, because the launcher dropped the ``Popen`` it created.

This supervisor keeps that handle and blocks on the real process completion primitive
(``Popen.wait``) - no polling loop, no CPU spin, no health probing. When the child exits
unexpectedly it applies one bounded restart policy and starts a replacement, which then
enters the *normal* worker startup and recovery path.

Job state stays out of here. The supervisor never marks a job interrupted, never resumes
one, never claims one and never reorders the queue: JobStore and the replacement worker own
all of that. Ownership is equally narrow - a supervisor drives only the process it was
handed, never a worker that already existed.
"""

from __future__ import annotations

import itertools
import json
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

#: One canonical policy for a Windows desktop Beta. A crashed worker should be back within
#: seconds; a worker that keeps crashing must stop being respawned fast enough that the user
#: gets an honest "offline" instead of a process storm. Worst case here is 3 restarts spread
#: over 22 seconds before the supervisor gives up for good.
BACKOFF_SECONDS: tuple[float, ...] = (2.0, 5.0, 15.0)
MAX_RESTARTS = len(BACKOFF_SECONDS)
#: A worker that ran this long proved it can stay up, so it earns its restart budget back.
#: Deliberately tied to process uptime, never to job completion: an idle queue must still
#: count as stability, and one crash today plus one crash next week must not exhaust anyone.
STABILITY_SECONDS = 120.0

STARTING = "starting"
RUNNING = "running"
BACKOFF = "backoff"
STOPPING = "stopping"
DEGRADED = "degraded"


def _default_log(event: str, **details: Any) -> None:
    payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **details}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)


class WorkerSupervisor:
    """Restart the worker process this launcher owns, a bounded number of times."""

    def __init__(
        self,
        spawn: Callable[[], Any],
        *,
        backoff: Iterable[float] = BACKOFF_SECONDS,
        stability_seconds: float = STABILITY_SECONDS,
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[..., Any] | None = None,
    ) -> None:
        self._spawn = spawn
        self._backoff = tuple(backoff)
        self._stability_seconds = float(stability_seconds)
        self._sleep = sleep
        self._clock = clock
        self._log = _default_log if log is None else log
        self._generations = itertools.count(1)
        self._stop = threading.Event()
        self.state = STARTING
        self.process: Any = None
        self.generation = 0
        self.restarts = 0                     #: restarts spent from the current budget
        self.spawn_count = 0                  #: children this supervisor created itself
        self.backoff_calls: list[float] = []

    # -- lifecycle ---------------------------------------------------------------

    def request_stop(self) -> None:
        """Declare the next worker exit intentional, *before* anything terminates it.

        Exit status can never carry this: a worker may vanish unexpectedly with code 0, and
        a deliberate stop may end non-zero. Intent lives in the launcher, not in the child.
        """

        self._stop.set()
        self.state = STOPPING

    def run(self, process: Any = None) -> str:
        """Supervise until an intentional stop or an exhausted restart budget.

        ``process`` is an already-started worker this launcher owns; it is adopted as the
        first generation rather than spawned, so the single-worker rule stays intact.
        """

        while True:
            if process is None:
                self._event("worker_spawn_started", attempt=self.restarts)
                try:
                    process = self._spawn()
                except Exception as exc:  # noqa: BLE001 - a failed start is a restart case
                    self._event("worker_spawn_failed", error_type=type(exc).__name__,
                                attempt=self.restarts)
                    if not self._schedule_restart(reason="spawn_failed"):
                        return self.state
                    continue
                self.spawn_count += 1
                if self.restarts:
                    self._event("worker_restart_succeeded", attempt=self.restarts,
                                pid=getattr(process, "pid", None))

            self.process = process
            self.generation = next(self._generations)
            self._event("worker_spawned", pid=getattr(process, "pid", None),
                        generation=self.generation)
            if not self._stop.is_set():
                self.state = RUNNING
            started = self._clock()
            exit_code = process.wait()
            uptime = self._clock() - started
            process = None

            if self._stop.is_set():
                self.state = STOPPING
                self._event("worker_expected_stop", exit_code=_as_int(exit_code),
                            generation=self.generation)
                return self.state

            self._event("worker_process_exited", exit_code=_as_int(exit_code),
                        generation=self.generation, uptime=round(uptime, 3))
            if uptime >= self._stability_seconds:
                self.restarts = 0
            if not self._schedule_restart(reason="process_exited"):
                return self.state

    # -- policy ------------------------------------------------------------------

    def _schedule_restart(self, *, reason: str) -> bool:
        if self.restarts >= len(self._backoff):
            self.state = DEGRADED
            self._event("worker_restart_exhausted", reason=reason, restarts=self.restarts)
            return False
        delay = self._backoff[self.restarts]
        self.restarts += 1
        self.state = BACKOFF
        self.backoff_calls.append(delay)
        self._event("worker_restart_scheduled", reason=reason, attempt=self.restarts,
                    delay=delay)
        self._sleep(delay)
        if self._stop.is_set():
            self.state = STOPPING
            self._event("worker_expected_stop", reason="stop_during_backoff")
            return False
        return True

    def _event(self, event: str, **details: Any) -> None:
        self._log(event, **{key: value for key, value in details.items() if value is not None})


def _as_int(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def supervise(spawn: Callable[[], Any], process: Any = None, **kwargs: Any) -> WorkerSupervisor:
    """Supervise ``process`` on a daemon thread and return the supervisor.

    Daemon on purpose: supervision belongs to this launcher instance and dies with it
    (TDD #53 does not resurrect launchers), and it must never keep the process alive.
    """

    supervisor = WorkerSupervisor(spawn, **kwargs)
    thread = threading.Thread(target=supervisor.run, args=(process,),
                              name="worker-supervisor", daemon=True)
    supervisor.thread = thread
    thread.start()
    return supervisor
