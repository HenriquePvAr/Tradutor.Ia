"""Private entry point for the killable Comix dynamic-reader child process."""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


def _safe_exception_message(exc: BaseException) -> str:
    text = str(exc)
    text = re.sub(r"https?://\S+", "<url-redacted>", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)(cookie|authorization|token|password|secret|api[_-]?key|cf_clearance)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>", text,
    )
    text = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer <redacted>", text)
    return " ".join(text.split())[:240]


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(20):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.005)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write(path, json.dumps(payload, sort_keys=True).encode("utf-8"))


def run_child_protocol(
    request_path: Path, result_path: Path, status_path: Path, progress_path: Path,
    resolver: Callable[[dict[str, Any], Callable[..., None]], Any],
    *, gate: Callable[[], bool] | None = None,
) -> int:
    """Run the handoff protocol; exposed so subprocess contract tests use real code."""
    started = time.monotonic()
    terminal = False
    latest_progress: dict[str, Any] = {}

    def progress(stage: str, **counts: Any) -> None:
        nonlocal latest_progress
        safe = {key: max(0, int(value)) for key, value in counts.items()
                if key in {"controls_found", "materialized_count"}}
        latest_progress = {
            **{key: latest_progress[key] for key in ("controls_found", "materialized_count")
               if key in latest_progress},
            "stage": str(stage)[:48], "elapsed_ms": round((time.monotonic() - started) * 1000),
            **safe,
        }
        _write_json(progress_path, latest_progress)

    def status(state: str, **fields: Any) -> None:
        nonlocal terminal
        payload: dict[str, Any] = {
            "state": state, "elapsed_ms": round((time.monotonic() - started) * 1000),
            **{key: latest_progress[key] for key in ("controls_found", "materialized_count")
               if key in latest_progress},
        }
        payload.update(fields)
        _write_json(status_path, payload)
        terminal = state in {"pass", "fail"}

    try:
        status("starting", stage="bootstrap")
        progress("bootstrap")
        if gate is not None and not gate():
            status("fail", reason_code="child_start_gate_rejected", stage="bootstrap")
            return 2
        request = json.loads(request_path.read_text(encoding="utf-8"))
        progress("resolver_start")
        analysis = resolver(request, progress)
        progress("result_serialization")
        result = pickle.dumps(analysis, protocol=pickle.HIGHEST_PROTOCOL)
        _atomic_write(result_path, result)
        status("pass", stage="complete", result_bytes=len(result),
               result_sha256=hashlib.sha256(result).hexdigest())
        return 0
    except Exception as exc:
        if type(exc).__name__ == "DynamicReaderError":
            reason = str(getattr(exc, "code", "") or "dynamic_resolver_child_failed")
        else:
            reason = "dynamic_resolver_child_failed"
        try:
            status("fail", reason_code=reason[:80], safe_error_code=reason[:80],
                   exception_type=type(exc).__name__[:80],
                   safe_message=_safe_exception_message(exc), stage="resolver")
        except OSError:
            return 3
        return 1
    finally:
        if not terminal:
            try:
                status("fail", reason_code="dynamic_resolver_child_incomplete", stage="child_exit")
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in {3, 4}:
        return 2
    request_path, result_path, status_path = map(Path, args[:3])
    progress_path = Path(args[3]) if len(args) == 4 else status_path.with_name("progress.json")

    def real_resolver(request: dict[str, Any], progress: Callable[..., None]) -> Any:
        url = str(request.get("url") or "")
        timeout = float(request.get("timeout") or 30.0)
        deadline_seconds = float(request.get("deadline_seconds") or 177.0)
        max_pages = request.get("max_pages")
        max_pages = int(max_pages) if max_pages is not None else None
        from yomu.download.sources.chapter_source import select_adapter
        from yomu.download.readers.scrapling_reader_resolver import _resolve_inline

        adapter = select_adapter(url)
        adapter.validate_url(url)
        adapter.validate_path(url)
        return _resolve_inline(
            url, adapter=adapter, timeout=timeout, deadline_seconds=deadline_seconds,
            max_pages=max_pages, progress_callback=progress,
        )

    return run_child_protocol(
        request_path, result_path, status_path, progress_path, real_resolver,
        gate=lambda: sys.stdin.readline().strip() == "go",
    )


if __name__ == "__main__":
    raise SystemExit(main())
