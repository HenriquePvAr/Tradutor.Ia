"""Small, local Comix clearance state shared by the downloader stacks.

Only the owner-obtained clearance and its matching user-agent are persisted.  Browser
profiles, passwords and other session material remain outside this file.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def session_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "YomuSekai" / "comix_cf_cookie.json"


def load_comix_session() -> tuple[str, str] | None:
    try:
        data = json.loads(session_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    cookie = str((data or {}).get("cf_clearance") or "").strip()
    user_agent = str((data or {}).get("user_agent") or "").strip()
    if not cookie or not user_agent or len(cookie) > 8192 or len(user_agent) > 512:
        return None
    return cookie, user_agent


def save_comix_session(cookie: str, user_agent: str) -> None:
    cookie = str(cookie or "").strip()
    user_agent = str(user_agent or "").strip()
    if not cookie or not user_agent:
        raise ValueError("invalid_comix_session")
    target = session_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f"{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"cf_clearance": cookie, "user_agent": user_agent}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def invalidate_comix_session() -> None:
    """Remove only the cached clearance; never remove the dedicated browser profile."""
    try:
        session_path().unlink()
    except FileNotFoundError:
        pass


@contextmanager
def human_capture_lock(timeout: float = 300.0) -> Iterator[None]:
    """Cross-process lock for the single dedicated human capture profile."""
    lock_path = session_path().with_name("comix_cf_capture.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(1.0, float(timeout))
    handle: Any = None
    while handle is None:
        try:
            handle = open(lock_path, "x", encoding="utf-8")
            handle.write(str(os.getpid()))
            handle.flush()
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError("comix_capture_lock_timeout")
            time.sleep(0.25)
    try:
        yield
    finally:
        try:
            handle.close()
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

