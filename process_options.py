"""Hermetic subprocess options shared by background pipeline processes.

The UI and worker may be started from a visible PowerShell/CMD window.  Child
processes must never inherit a console window on Windows, while their streams
remain captured by the caller for logs and exit-code accounting.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def configure_hidden_multiprocessing() -> None:
    """Use pythonw for Windows multiprocessing children when available.

    ``ProcessPoolExecutor`` and ``multiprocessing.Process`` do not accept the
    ``creationflags`` used by our Popen helper. Selecting the sibling pythonw
    executable is the supported way to keep those children console-free while
    preserving normal stdout/stderr capture in the parent.
    """

    if os.name != "nt":
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        return
    import multiprocessing

    multiprocessing.set_executable(str(pythonw))


def background_python_executable() -> str:
    """Return pythonw for long-lived Windows UI/worker processes when present."""

    if os.name == "nt":
        candidate = Path(sys.executable).with_name("pythonw.exe")
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def hidden_console_options() -> dict[str, Any]:
    """Windows-only kwargs that keep a short-lived child console-free.

    ``build_background_process_options`` also fixes the standard streams, which
    conflicts with ``subprocess.run(input=..., capture_output=True)``.  Callers
    that capture output themselves need only the window flags, so they stay
    auditable (stdout/stderr still reach the caller) without flashing a console.
    """

    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": startupinfo,
    }


def build_background_process_options(
    *,
    stdin: Any = subprocess.DEVNULL,
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    new_session: bool = False,
) -> dict[str, Any]:
    """Return safe ``Popen`` kwargs for a non-interactive child.

    ``CREATE_NO_WINDOW`` and hidden startup info are Windows-only.  POSIX callers
    can request a new session for process-tree ownership.  ``shell`` is always
    disabled so a background stage cannot unexpectedly start a command shell.
    """

    options: dict[str, Any] = {
        "stdin": stdin,
        "stdout": stdout,
        "stderr": stderr,
        "shell": False,
    }
    if env is not None:
        options["env"] = env
    if cwd is not None:
        options["cwd"] = cwd
    if os.name == "nt":
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        options["creationflags"] = flags
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        options["startupinfo"] = startupinfo
    elif new_session:
        options["start_new_session"] = True
    return options
