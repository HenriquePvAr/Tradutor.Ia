"""Central, testable helper for validating and terminating a process tree.

Every termination in the worker goes through here so the "never kill by name, always
validate ownership" rule lives in one place. Ownership is a PID plus its start time and
a command-line fingerprint: a bare PID can be reused by an unrelated process, so a match
requires the start time (within a small tolerance) and every expected substring to be
present in the command line. A process that fails validation is never signalled.

Backed by psutil, which is already a dependency, so child discovery
(``children(recursive=True)``), start time and command line are cross-checked without
shelling out to taskkill.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

import psutil

# Start times from different sources can differ by sub-second rounding; treat two within
# this window as the same process instance.
CREATE_TIME_TOLERANCE = 1.0


def snapshot(pid: int | None) -> dict[str, Any] | None:
    """Return {pid, create_time, cmdline} for a live process, or None if it is gone."""
    if not pid:
        return None
    try:
        proc = psutil.Process(int(pid))
        return {
            "pid": int(pid),
            "create_time": float(proc.create_time()),
            "cmdline": list(proc.cmdline()),
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return None


def matches(pid: int | None, *, create_time: float | None = None,
            substrings: Iterable[str] = ()) -> bool:
    """True when the live process at ``pid`` is the one we think it is.

    Fail-closed: a missing process, a start-time mismatch (PID reuse), or a command line
    missing an expected fingerprint all return False, so a reused or foreign PID is never
    treated as ours.
    """
    info = snapshot(pid)
    if info is None:
        return False
    if create_time is not None and abs(info["create_time"] - float(create_time)) > CREATE_TIME_TOLERANCE:
        return False
    joined = " ".join(info["cmdline"])
    return all(str(token) in joined for token in substrings)


def is_alive(pid: int | None, *, create_time: float | None = None,
             substrings: Iterable[str] = ()) -> bool:
    return matches(pid, create_time=create_time, substrings=substrings)


def terminate_tree(pid: int | None, *, create_time: float | None = None,
                   substrings: Iterable[str] = (), timeout: float = 8.0) -> dict[str, Any]:
    """Terminate a validated process and all of its descendants.

    The root PID is validated against ``create_time``/``substrings`` first; a mismatch
    returns ``ownership_mismatch`` and touches nothing. Otherwise the root and every
    descendant are asked to terminate, waited for, then killed if still alive. Returns a
    report of what was found and stopped.
    """
    report: dict[str, Any] = {
        "root_pid": int(pid) if pid else None,
        "validated": False,
        "reason": "",
        "terminated": [],
        "killed": [],
        "survivors": [],
    }
    if not pid:
        report["reason"] = "no_pid"
        return report
    try:
        root = psutil.Process(int(pid))
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        report["reason"] = "not_running"
        return report
    if not matches(pid, create_time=create_time, substrings=substrings):
        report["reason"] = "ownership_mismatch"
        return report
    report["validated"] = True

    try:
        members = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        members = [root]

    for proc in members:
        try:
            proc.terminate()
            report["terminated"].append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(members, timeout=timeout)
    for proc in alive:
        try:
            proc.kill()
            report["killed"].append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, still = psutil.wait_procs(alive, timeout=max(1.0, timeout / 2))
    report["survivors"] = [proc.pid for proc in still]
    report["reason"] = "stopped" if not report["survivors"] else "survivors_remain"
    return report


def wait_gone(pid: int | None, *, timeout: float = 8.0) -> bool:
    """True once the process is gone (or was never there) within the timeout."""
    if not pid:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if snapshot(pid) is None:
            return True
        time.sleep(0.1)
    return snapshot(pid) is None
