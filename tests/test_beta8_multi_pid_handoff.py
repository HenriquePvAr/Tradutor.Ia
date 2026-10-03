import base64
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

import installer_update


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell integration")


def _b64(value):
    return base64.b64encode(json.dumps(value, separators=(",", ":")).encode("utf-8")).decode("ascii")


def _run(script_path, log_path, payload):
    return subprocess.run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(script_path), "-HandoffLogPath", str(log_path),
        "-HandoffPayloadBase64", _b64(payload),
    ], capture_output=True, text=True, timeout=20)


def _sleep_process(milliseconds):
    return subprocess.Popen([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        f"Start-Sleep -Milliseconds {milliseconds}",
    ], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _payload(installer, pids, timeout=5, args=None):
    return {
        "installer_path": str(installer),
        "installer_args": args or ["/c", "exit", "0"],
        "wait_process_ids": list(pids),
        "timeout_seconds": timeout,
        "expected_size": None,
        "expected_sha256": None,
    }


def test_real_powershell_payload_transport_supports_0_1_2_3_and_10_pids(tmp_path):
    installer = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "cmd.exe"
    script = tmp_path / "handoff.ps1"
    script.write_text(installer_update._handoff_script(), encoding="utf-8")
    for count in (0, 1, 2, 3, 10):
        processes = [_sleep_process(250) for _ in range(count)]
        try:
            log = tmp_path / f"pids-{count}.log"
            result = _run(script, log, _payload(installer, [p.pid for p in processes]))
            text = log.read_text(encoding="utf-8")
            assert result.returncode == 0, result.stderr
            assert "ParameterAlreadyBound" not in result.stderr
            assert "HANDOFF_PROCESS_STARTED" in text
            assert "PAYLOAD_DECODE_OK" in text
            assert "INSTALLER_SPAWNED" in text
        finally:
            for process in processes:
                process.wait(timeout=5)


def test_wait_all_pids_and_already_exited_pid(tmp_path):
    installer = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "cmd.exe"
    script = tmp_path / "handoff.ps1"
    script.write_text(installer_update._handoff_script(), encoding="utf-8")
    processes = [_sleep_process(150), _sleep_process(450), _sleep_process(800)]
    try:
        log = tmp_path / "all.log"
        result = _run(script, log, _payload(installer, [p.pid for p in processes]))
        assert result.returncode == 0
        text = log.read_text(encoding="utf-8")
        assert "wait_pid_count=3" in text
        assert text.index("INSTALLER_SPAWN") > text.index("HANDOFF_START")
        assert all(p.poll() is not None for p in processes)
    finally:
        for process in processes:
            process.wait(timeout=5)
    exited = _sleep_process(100)
    exited.wait(timeout=5)
    live = _sleep_process(450)
    try:
        result = _run(script, tmp_path / "exited.log", _payload(installer, [exited.pid, live.pid]))
        assert result.returncode == 0
    finally:
        live.wait(timeout=5)


def test_timeout_and_malformed_payload_fail_closed(tmp_path):
    installer = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "cmd.exe"
    script = tmp_path / "handoff.ps1"
    script.write_text(installer_update._handoff_script(), encoding="utf-8")
    live = _sleep_process(2000)
    try:
        timeout_log = tmp_path / "timeout.log"
        result = _run(script, timeout_log, _payload(installer, [live.pid], timeout=1))
        assert result.returncode == 71
        assert "HANDOFF_TIMEOUT" in timeout_log.read_text(encoding="utf-8")
    finally:
        live.terminate(); live.wait(timeout=5)
    malformed = [
        "not-base64",
        _b64({"installer_path": str(installer), "installer_args": [], "wait_process_ids": "1", "timeout_seconds": 1}),
        _b64({"installer_path": str(installer), "installer_args": [], "wait_process_ids": [-1], "timeout_seconds": 1}),
        _b64({"installer_path": str(installer), "installer_args": [], "wait_process_ids": [], "timeout_seconds": 0}),
    ]
    for index, encoded in enumerate(malformed):
        log = tmp_path / f"malformed-{index}.log"
        result = subprocess.run([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-HandoffLogPath", str(log), "-HandoffPayloadBase64", encoded,
        ], capture_output=True, text=True, timeout=20)
        assert result.returncode == 74
        text = log.read_text(encoding="utf-8")
        assert "HANDOFF_PROCESS_STARTED" in text
        assert "HANDOFF_ARGUMENT_PARSE_FAILED" in text
        assert "INSTALLER_SPAWNED" not in text


def test_duplicate_pid_is_normalized_and_command_has_one_payload_parameter(tmp_path):
    installer = tmp_path / "YomuSekai-0.9.1-beta.8-Setup-x64.exe"
    installer.write_bytes(b"fixture")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(installer_update.subprocess, "Popen", lambda *args, **kwargs: (args[0], kwargs))
        command, _ = installer_update.spawn_installer_after_processes_exit(installer, owned_pids=[1234, 1234, 5678])
    payload = json.loads(base64.b64decode(command[command.index("-HandoffPayloadBase64") + 1]))
    assert command.count("-WaitProcessIds") == 0
    assert command.count("-HandoffPayloadBase64") == 1
    assert payload["wait_process_ids"] == [1234, 5678]
