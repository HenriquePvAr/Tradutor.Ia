"""Private entry point for the killable Comix dynamic-reader child process."""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        return 2
    request_path, result_path, status_path = map(Path, args)
    # Parent assigns this process to its kill-on-close boundary before allowing work.
    if sys.stdin.readline().strip() != "go":
        return 2
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        url = str(request.get("url") or "")
        timeout = float(request.get("timeout") or 30.0)
        deadline_seconds = float(request.get("deadline_seconds") or 177.0)
        from chapter_source import select_adapter
        from scrapling_reader_resolver import _resolve_inline

        adapter = select_adapter(url)
        adapter.validate_url(url)
        adapter.validate_path(url)
        analysis = _resolve_inline(
            url, adapter=adapter, timeout=timeout,
            deadline_seconds=deadline_seconds,
        )
        with result_path.open("wb") as stream:
            pickle.dump(analysis, stream, protocol=pickle.HIGHEST_PROTOCOL)
        status = {"status": "pass"}
    except Exception as exc:
        if type(exc).__name__ == "DynamicReaderError":
            reason_code = str(getattr(exc, "code", "") or "dynamic_resolver_child_failed")
        else:
            reason_code = "dynamic_resolver_child_failed"
        status = {"status": "fail", "reason_code": reason_code[:80]}
    try:
        status_path.write_text(json.dumps(status, sort_keys=True), encoding="utf-8")
    except OSError:
        return 3
    return 0 if status["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
