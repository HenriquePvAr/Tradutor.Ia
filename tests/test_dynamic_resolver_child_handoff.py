"""Subprocess contract tests for the Comix dynamic resolver handoff."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from yomu.download.readers import scrapling_reader_resolver as parent


def _launch(tmp_path: Path, body: str, *, stall_timeout: float = 1.0):
    request, result, status, progress = (tmp_path / "request.json", tmp_path / "result.pkl",
                                         tmp_path / "status.json", tmp_path / "progress.json")
    request.write_text(json.dumps({"fixture": True}), encoding="utf-8")
    script = (
        "import sys,time\n"
        "from pathlib import Path\n"
        "from yomu.download.readers.dynamic_resolver_process import run_child_protocol\n"
        "def resolver(request, progress):\n"
        + body + "\n"
        + f"sys.exit(run_child_protocol(Path({str(request)!r}),Path({str(result)!r}),"
        + f"Path({str(status)!r}),Path({str(progress)!r}),resolver,"
        + "gate=lambda:sys.stdin.readline().strip()=='go'))\n"
    )
    started = time.monotonic()
    outcome = parent._run_child_command(
        [sys.executable, "-u", "-c", script], deadline=started + 10,
        progress_path=progress, status_path=status, stall_timeout=stall_timeout,
    )
    return outcome, result, status, progress, time.monotonic() - started


def test_child_protocol_publishes_atomic_result_hash_and_progress(tmp_path):
    body = "    progress('materializing', controls_found=183, materialized_count=183)\n    return {'pages': list(range(183))}"
    outcome, result, status, progress, _ = _launch(tmp_path, body)
    state = json.loads(status.read_text("utf-8"))
    assert outcome["returncode"] == 0 and outcome["reaped"]
    assert state["state"] == "pass"
    assert state["result_bytes"] == result.stat().st_size
    assert state["result_sha256"]
    assert json.loads(progress.read_text("utf-8"))["materialized_count"] == 183


def test_child_progress_prevents_stall_termination(tmp_path):
    body = "    for count in range(8):\n        progress('materializing', controls_found=8, materialized_count=count)\n        time.sleep(0.04)\n    return {'ok': True}"
    outcome, _, status, _, elapsed = _launch(tmp_path, body, stall_timeout=0.15)
    assert not outcome["terminated"] and outcome["returncode"] == 0
    assert elapsed > 0.2
    assert json.loads(status.read_text("utf-8"))["state"] == "pass"


def test_stalled_child_has_distinct_reason_and_is_reaped(tmp_path):
    body = "    progress('resolver_start')\n    time.sleep(1)\n    return {'unreachable': True}"
    outcome, _, status, _, elapsed = _launch(tmp_path, body, stall_timeout=0.12)
    assert outcome["terminated"] and outcome["stalled"] and outcome["reaped"]
    assert elapsed < 0.8
    assert json.loads(status.read_text("utf-8"))["reason_code"] == "dynamic_resolver_child_stalled"


def test_child_exception_status_redacts_urls_and_credentials(tmp_path):
    body = "    raise ValueError('https://private.example/path token=secret')"
    outcome, _, status, _, _ = _launch(tmp_path, body)
    text = status.read_text("utf-8")
    state = json.loads(text)
    assert outcome["returncode"] == 1
    assert state["state"] == "fail" and state["exception_type"] == "ValueError"
    assert "private.example" not in text and "secret" not in text


def test_persistent_failure_evidence_survives_temp_cleanup(tmp_path, monkeypatch):
    diagnostic_root = tmp_path / "diagnostics"
    monkeypatch.setattr(parent, "_child_diagnostic_root", lambda: diagnostic_root)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        outcome, _, status_path, _, _ = _launch(
            root, "    raise RuntimeError('https://private.example/?token=secret')",
        )
        target = parent._persist_child_failure(
            "synthetic", status=json.loads(status_path.read_text("utf-8")),
            outcome=outcome, command=[sys.executable, "-m", "fake.child"],
            started=time.monotonic() - 0.1, temp_root=root,
        )
        assert target.exists()
    assert not root.exists()
    evidence = "".join(path.read_text("utf-8") for path in target.iterdir())
    assert "private.example" not in evidence and "secret" not in evidence
    assert {"child_status.json", "child_stderr.txt", "child_stdout_tail.txt",
            "launch_context.json", "failure.json"}.issubset(
                {path.name for path in target.iterdir()})


def test_source_and_frozen_launch_modes_are_explicit(tmp_path):
    paths = [tmp_path / name for name in ("request.json", "result.pkl", "status.json", "progress.json")]
    source, env = parent._build_child_launch(*paths, frozen=False)
    assert source[1:4] == ["-u", "-m", "yomu.download.readers.dynamic_resolver_process"]
    assert env and str(Path(parent.__file__).resolve().parents[3]) in env["PYTHONPATH"].split(__import__("os").pathsep)
    frozen, frozen_env = parent._build_child_launch(*paths, frozen=True)
    assert frozen[1:3] == ["--internal-child", "dynamic-resolver"]
    assert frozen[-1] == str(paths[-1]) and frozen_env is None
