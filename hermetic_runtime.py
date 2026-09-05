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
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parent
REAL_RUNTIME_ROOT = (REPO_ROOT / ".cache" / "runtime").resolve()
REAL_JOBS_DB = REAL_RUNTIME_ROOT / "jobs.sqlite3"
#: The user's real translated chapters and the UI history that indexes them. Reading these
#: is not destructive, but a test that discovers real runs is not hermetic: its result
#: depends on the developer's machine. Writing ui_history.json *is* destructive.
REAL_OUTPUT_ROOT = (REPO_ROOT / "output").resolve()
REAL_UI_HISTORY_PATHS = (
    (REPO_ROOT / ".cache" / "ui_history.json").resolve(),
    (REPO_ROOT / ".cache" / "ui_hidden_history.json").resolve(),
)
TEST_RUNTIME_ROOT_ENV = "TRADUTOR_TEST_RUNTIME_ROOT"
# Deliberately distinct from TRADUTOR_IA_HERMETIC_TEST_ENV, which governs .env loading and
# which individual tests strip from child processes on purpose. Runtime isolation must stay
# in force for those children regardless.
RUNTIME_GUARD_ENV = "TRADUTOR_IA_RUNTIME_ISOLATION_GUARD"
# start_tradutor pins itself to the production queue and worker regardless of arguments or
# environment, so a test can never launch it.
FORBIDDEN_ENTRYPOINTS = ("start_tradutor.py", "start_tradutor.bat")
# ...with one command excepted: ``start_tradutor.py selftest`` (TDD #58) only imports the
# runtime to prove a payload is runnable — it starts no worker, no UI and no job, and opens no
# database. The updater's startup health gate spawns exactly this, so an updater test must be
# able to as well; every other start_tradutor invocation stays refused.
SPAWNABLE_LAUNCHER_COMMANDS = ("selftest",)
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
_ORIGINAL_SCANDIR = os.scandir
_ORIGINAL_LISTDIR = os.listdir

#: Counters the tripwire suite asserts on. Every entry is an attempt that was refused.
ATTEMPTS: dict[str, list[str]] = {
    "sqlite": [], "open": [], "mkdir": [], "unlink": [], "rename": [], "spawn": [],
    "listdir": [],
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


# Keep the installed Beta's mutable data protected as well as legacy repo state.
# Do not call runtime_paths here: its test-aware resolver intentionally redirects.
REAL_BETA_USER_ROOT = (
    Path(os.environ["LOCALAPPDATA"]) / "TradutorIA"
    if os.environ.get("LOCALAPPDATA") else
    Path(os.environ["XDG_STATE_HOME"]) / "TradutorIA"
    if os.environ.get("XDG_STATE_HOME") else Path.home() / ".tradutoria"
)
_REAL_FORMS = (_canonical_forms(str(REAL_RUNTIME_ROOT))
               + _canonical_forms(str(REAL_BETA_USER_ROOT / "runtime")))
_REAL_USER_STATE_FORMS = _canonical_forms(str(REAL_OUTPUT_ROOT)) + tuple(
    form for path in REAL_UI_HISTORY_PATHS for form in _canonical_forms(str(path))
) + tuple(form for name in ("cache", "output", "temp")
          for form in _canonical_forms(str(REAL_BETA_USER_ROOT / name)))


def sqlite_uri_path(text: str) -> str | None:
    """Filesystem path named by a sqlite3 ``file:`` URI, or ``None`` for anything else.

    ``sqlite3.connect(..., uri=True)`` accepts ``file:jobs.sqlite3?mode=ro``,
    ``file:C:/x/jobs.sqlite3`` and ``file:///C:/x/jobs.sqlite3``. Only the path component
    names a file: query parameters (``mode``, ``cache``, ``vfs``) never do, and
    ``file::memory:`` names no file at all.
    """

    if not text[:5].casefold() == "file:":
        return None
    parts = urlsplit(text)
    if parts.netloc and parts.netloc.casefold() != "localhost":
        return None  # a remote authority is not a path on this filesystem
    path = unquote(parts.path)
    if path.startswith(":"):
        return None  # file::memory: and friends are not filesystem paths
    # file:///C:/x -> /C:/x; strip the URI's root slash ahead of the drive letter.
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path or None


def _matches(text: str, forms: tuple[str, ...]) -> bool:
    for candidate in _canonical_forms(text):
        for real in forms:
            if candidate == real or candidate.startswith(real + os.sep):
                return True
    return False


def _as_text(value: object) -> str | None:
    try:
        text = os.fspath(value)  # type: ignore[arg-type]
    except TypeError:
        return None  # a file descriptor or a stream can never name a new runtime path
    if isinstance(text, bytes):
        text = text.decode("utf-8", "ignore")
    if text[:5].casefold() == "file:":
        # sqlite3 URIs must be compared as the path they resolve to, not as raw text.
        return sqlite_uri_path(text)
    return text


def is_real_runtime_path(value: object) -> bool:
    """True when ``value`` names the real runtime root or anything under it.

    Comparison is canonical: relative paths, mixed separators, case-insensitive Windows
    volumes, junctions and sqlite ``file:`` URIs all normalize to the same form.
    """

    text = _as_text(value)
    if text is None or not any(part in text.casefold() for part in (".cache", "tradutoria")):
        return False  # cheap reject keeps the guard off the hot path of ordinary file I/O
    return _matches(text, _REAL_FORMS)


def is_real_user_state_path(value: object) -> bool:
    """True for the user's real ``output/`` tree or the real UI history files."""

    text = _as_text(value)
    if text is None:
        return False
    lowered = text.casefold()
    if not any(part in lowered for part in ("output", "ui_history", "ui_hidden_history", "tradutoria")):
        return False
    return _matches(text, _REAL_USER_STATE_FORMS)


def _is_forbidden(value: object) -> bool:
    return is_real_runtime_path(value) or is_real_user_state_path(value)


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
    if _is_forbidden(file):
        _refuse("open", f"{file} (mode={mode})")
    return _ORIGINAL_OPEN(file, mode, *args, **kwargs)


def _guard_path(kind: str, original):
    def guarded(path, *args, **kwargs):
        if _is_forbidden(path):
            _refuse(kind, str(path))
        return original(path, *args, **kwargs)

    return guarded


def _guard_listing(original):
    def guarded(path=".", *args, **kwargs):
        if is_real_user_state_path(path):
            _refuse("listdir", str(path))
        return original(path, *args, **kwargs)

    return guarded


def _guard_two_paths(kind: str, original):
    def guarded(src, dst, *args, **kwargs):
        for candidate in (src, dst):
            if _is_forbidden(candidate):
                _refuse(kind, str(candidate))
        return original(src, dst, *args, **kwargs)

    return guarded


def _guarded_popen_init(self, args, *rest, **kwargs):
    command = args if isinstance(args, (list, tuple)) else [args]
    parts = [str(part) for part in command]
    text = " ".join(parts)
    lowered = text.casefold()
    launcher_selftest = bool(parts) and parts[-1] in SPAWNABLE_LAUNCHER_COMMANDS
    for entrypoint in FORBIDDEN_ENTRYPOINTS:
        if entrypoint in lowered and not launcher_selftest:
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
    # Path.iterdir/glob resolve through os.scandir; discover_outputs() enumerates a root.
    # Only user state is listed-guarded: the runtime root's hazard is open/connect/write,
    # already refused above, and repo-wide source scans legitimately walk past it.
    os.scandir = _guard_listing(_ORIGINAL_SCANDIR)
    os.listdir = _guard_listing(_ORIGINAL_LISTDIR)
    subprocess.Popen.__init__ = _guarded_popen_init
    sqlite3._tradutor_ia_runtime_guard = True
    return root
