"""Fail-closed start gate shared by worker runner subprocesses."""

from __future__ import annotations

import os
import time
from pathlib import Path


def wait_for_start_gate(path: str, *, timeout: float = 15.0) -> bool:
    """Wait until the worker persisted our PID; time out without running a job."""
    if not path:
        return True
    gate = Path(path)
    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(gate), os.O_RDONLY)
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        except OSError:
            return False
        else:
            os.close(fd)
            try:
                gate.unlink(missing_ok=True)
            except OSError:
                pass
            return True
    return False
