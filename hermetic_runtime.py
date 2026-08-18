"""Fail-closed runtime isolation for the offline test suite.

The project's real runtime state lives in ``<repo>/.cache/runtime`` (jobs.sqlite3, worker
logs, PID/lease rows).  Test code that constructs a runtime component without supplying an
explicit root used to fall back to exactly that directory, so a plain ``python -m unittest
discover`` could open the user's real queue, mutate real job rows and launch the real
worker.

Installing this guard gives a test process its own unique temporary runtime root and turns
every remaining route to the real one into an immediate failure *before* the side effect.
Nothing here runs in production: the guard is installed only by ``_test_bootstrap``,
``conftest`` and the test-entrypoint branch of ``sitecustomize``.
"""

from __future__ import annotations

import atexit
import builtins
import io
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
REAL_RUNTIME_ROOT = (REPO_ROOT / ".cache" / "runtime").resolve()
REAL_JOBS_DB = REAL_RUNTIME_ROOT / "jobs.sqlite3"
TEST_RUNTIME_ROOT_ENV = "TRADUTOR_TEST_RUNTIME_ROOT"
# Deliberately distinct from TRADUTOR_IA_HERMETIC_TEST_ENV, which governs .env loading and
# which individual tests strip from child processes on purpose. Runtime isolation must stay
# in force for those children regardless.
RUNTIME_GUARD_ENV = "TRADUTOR_IA_RUNTIME_ISOLATION_GUARD"
# start_tradutor pins itself to the production queue and worker regardless of arguments or
# environment, so a test can never launch it.
FORBIDDEN_ENTRYPOINTS = ("start_tradutor.py", "start_tradutor.bat")
# The worker may run as a test subprocess, but only when the caller states which database it
# drains. Without ``--db`` it falls back to worker_service.DEFAULT_DB, the real queue.
ISOLATED_DB_REQUIRED_ENTRYPOINTS = ("worker_service.py",)
# The UI may run as an isolated test server, but only when the child environment carries an
# isolated runtime root; otherwise its module-level UiBridge() binds the real runtime.
ISOLATED_ROOT_REQUIRED_ENTRYPOINTS = ("app_ui.py",)

_ORIGINAL_SQLITE_CONNECT = sqlite3.connect
_ORIGINAL_OPEN = builtins.open
_ORIGINAL_MAKEDIRS = os.makedirs
_ORIGINAL_MKDIR = os.mkdir
_ORIGINAL_REMOVE = os.remove
_ORIGINAL_UNLINK = os.unlink
_ORIGINAL_RENAME = os.rename
_ORIGINAL_REPLACE = os.replace
_ORIGINAL_POPEN_INIT = subprocess.Popen.__init__

#: Counters the tripwire suite asserts on. Every entry is an attempt that was refused.
ATTEMPTS: dict[str, list[str]] = {
    "sqlite": [], "open": [], "mkdir": [], "unlink": [], "rename": [], "spawn": [],
}


class RealRuntimeAccess(AssertionError):
    """Raised before a test can reach the project's real runtime state."""


def _canonical_forms(value: str) -> tuple[str, ...]:
    forms = {os.path.normcase(os.path.abspath(value))}
    try:  # junctions/symlinks resolve to the same physical directory
        forms.add(os.path.normcase(os.path.realpath(value)))
    except OSError:
        pass
    return tuple(forms)


_REAL_FORMS = _canonical_forms(str(REAL_RUNTIME_ROOT))


def is_real_runtime_path(value: object) -> bool:
    """True when ``value`` names the real runtime root or anything under it.

    Comparison is canonical: relative paths, mixed separators, case-insensitive Windows
    volumes and junctions all normalize to the same form.
    """

    try:
        text = os.fspath(value)  # type: ignore[arg-type]
    except TypeError:
        return False  # a file descriptor or a stream can never name a new runtime path
    if isinstance(text, bytes):
        text = text.decode("utf-8", "ignore")
    if ".cache" not in text.casefold():
        return False  # cheap reject keeps the guard off the hot path of ordinary file I/O
    for candidate in _canonical_forms(text):
        for real in _REAL_FORMS:
            if candidate == real or candidate.startswith(real + os.sep):
                return True
    return False


def _refuse(kind: str, target: str) -> None:
    ATTEMPTS[kind].append(target)
    raise RealRuntimeAccess(
        f"Teste tentou alcançar o runtime real do projeto ({kind}): {target}. "
        f"Use o runtime isolado em {TEST_RUNTIME_ROOT_ENV} (raiz temporária por processo)."
    )


def _guarded_connect(database, *args, **kwargs):
    if is_real_runtime_path(database):
        _refuse("sqlite", str(database))
    return _ORIGINAL_SQLITE_CONNECT(database, *args, **kwargs)


def _guarded_open(file, mode="r", *args, **kwargs):
    if is_real_runtime_path(file):
        _refuse("open", f"{file} (mode={mode})")
    return _ORIGINAL_OPEN(file, mode, *args, **kwargs)


def _guard_path(kind: str, original):
    def guarded(path, *args, **kwargs):
        if is_real_runtime_path(path):
            _refuse(kind, str(path))
        return original(path, *args, **kwargs)

    return guarded


def _guard_two_paths(kind: str, original):
    def guarded(src, dst, *args, **kwargs):
        for candidate in (src, dst):
            if is_real_runtime_path(candidate):
                _refuse(kind, str(candidate))
        return original(src, dst, *args, **kwargs)

    return guarded


def _guarded_popen_init(self, args, *rest, **kwargs):
    command = args if isinstance(args, (list, tuple)) else [args]
    parts = [str(part) for part in command]
    text = " ".join(parts)
    lowered = text.casefold()
    for entrypoint in FORBIDDEN_ENTRYPOINTS:
        if entrypoint in lowered:
            _refuse("spawn", text)
    for part in command:
        if is_real_runtime_path(part):
            _refuse("spawn", text)
    if any(entrypoint in lowered for entrypoint in ISOLATED_DB_REQUIRED_ENTRYPOINTS):
        index = parts.index("--db") if "--db" in parts else -1
        if index < 0 or index + 1 >= len(parts):
            _refuse("spawn", text)  # would silently drain the production queue
    if any(entrypoint in lowered for entrypoint in ISOLATED_ROOT_REQUIRED_ENTRYPOINTS):
        child_env = kwargs.get("env")
        if child_env is None and len(rest) >= 10:
            # Popen(args, bufsize, executable, stdin, stdout, stderr, preexec_fn,
            #       close_fds, shell, cwd, env, ...)
            child_env = rest[9]
        if child_env is None:
            child_env = os.environ
        requested = str(child_env.get(TEST_RUNTIME_ROOT_ENV) or "").strip()
        if not requested or is_real_runtime_path(requested):
            _refuse("spawn", text)  # would bind the real queue through UiBridge()
    return _ORIGINAL_POPEN_INIT(self, args, *rest, **kwargs)


TEST_RUNTIME_PREFIX = "tradutor-test-runtime-"
#: A root whose sqlite handles are still open at interpreter exit cannot be removed on
#: Windows, so the next run sweeps the ones old enough that no suite can still own them.
STALE_TEST_RUNTIME_SECONDS = 6 * 60 * 60


def _sweep_stale_test_runtimes(current: Path) -> None:
    cutoff = time.time() - STALE_TEST_RUNTIME_SECONDS
    try:
        candidates = list(Path(tempfile.gettempdir()).glob(TEST_RUNTIME_PREFIX + "*"))
    except OSError:
        return
    for candidate in candidates:
        try:
            if candidate == current or candidate.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(candidate, ignore_errors=True)


def install_runtime_isolation_guard() -> Path:
    """Give this test process a private runtime root and fail closed on the real one.

    Returns the isolated runtime root. Idempotent, and inherited by child Python processes
    through the environment so a worker subprocess started by a test stays isolated too.
    """

    os.environ[RUNTIME_GUARD_ENV] = "1"
    existing = str(os.environ.get(TEST_RUNTIME_ROOT_ENV) or "").strip()
    if existing:
        root = Path(existing)
    else:
        # Unique per process: two suites running in parallel never share a jobs.sqlite3,
        # a lease row or an artifact directory.
        root = Path(tempfile.mkdtemp(prefix=TEST_RUNTIME_PREFIX))
        os.environ[TEST_RUNTIME_ROOT_ENV] = str(root)
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        _sweep_stale_test_runtimes(root)

    if getattr(sqlite3, "_tradutor_ia_runtime_guard", False):
        return root

    sqlite3.connect = _guarded_connect
    builtins.open = _guarded_open
    io.open = _guarded_open  # pathlib.Path.open resolves through io, not builtins
    os.makedirs = _guard_path("mkdir", _ORIGINAL_MAKEDIRS)
    os.mkdir = _guard_path("mkdir", _ORIGINAL_MKDIR)
    os.remove = _guard_path("unlink", _ORIGINAL_REMOVE)
    os.unlink = _guard_path("unlink", _ORIGINAL_UNLINK)
    os.rename = _guard_two_paths("rename", _ORIGINAL_RENAME)
    os.replace = _guard_two_paths("rename", _ORIGINAL_REPLACE)
    subprocess.Popen.__init__ = _guarded_popen_init
    sqlite3._tradutor_ia_runtime_guard = True
    return root
