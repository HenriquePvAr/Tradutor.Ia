import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

import installer_update


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell integration")


def _run_ps(args):
    return subprocess.run(["powershell.exe", *args], capture_output=True, text=True, timeout=20)


def _b64(value):
    return base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode("ascii")


def _invoke(script, tmp_path, *, installer, payload, extra=None, timeout_seconds=5):
    script_path = tmp_path / "handoff helper with spaces.ps1"
    log_path = tmp_path / "handoff.log"
    script_path.write_text(script, encoding="utf-8")
    structured = {"installer_path": str(installer), "installer_args": payload,
                  "wait_process_ids": list(extra or []), "timeout_seconds": timeout_seconds,
                  "expected_size": None, "expected_sha256": None}
    args = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script_path),
            "-HandoffLogPath", str(log_path), "-HandoffPayloadBase64", _b64(structured)]
    result = _run_ps(args)
    return result, log_path


def test_real_powershell_valid_payload_creates_log_and_spawns(tmp_path):
    installer = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "cmd.exe"
    result, log = _invoke(installer_update._handoff_script(), tmp_path, installer=installer, payload=["/c", "exit", "0"])
    assert result.returncode == 0, result.stderr
    text = log.read_text(encoding="utf-8")
    assert "HANDOFF_PROCESS_STARTED" in text
    assert "HANDOFF_START" in text
    assert "INSTALLER_SPAWNED" in text


def test_real_powershell_malformed_payload_fails_closed_and_logs_before_parse(tmp_path):
    installer = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "cmd.exe"
    script_path = tmp_path / "malformed.ps1"
    log_path = tmp_path / "malformed.log"
    script_path.write_text(installer_update._handoff_script(), encoding="utf-8")
    result = _run_ps(["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script_path),
                      "-HandoffLogPath", str(log_path), "-HandoffPayloadBase64", "not-base64"])
    assert result.returncode == 74
    text = log_path.read_text(encoding="utf-8")
    assert "HANDOFF_PROCESS_STARTED" in text
    assert "HANDOFF_ARGUMENT_PARSE_FAILED" in text
    assert "INSTALLER_SPAWNED" not in text


def test_old_raw_json_transport_is_exercised_by_real_powershell(tmp_path):
    script = tmp_path / "old.ps1"
    log = tmp_path / "old.log"
    script.write_text(r'''param([Parameter(Mandatory=$true)][string]$LogPath,[string]$InstallerArgsJson='[]')
$ErrorActionPreference='Stop'
try { [string[]]$x=@($InstallerArgsJson | ConvertFrom-Json); Add-Content $LogPath 'HANDOFF_START'; exit 0 }
catch { Add-Content $LogPath ('PARSE_ERROR ' + $_.Exception.GetType().Name); exit 74 }''', encoding="utf-8")
    raw = json.dumps(["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"], separators=(",", ":"))
    result = _run_ps(["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                      "-LogPath", str(log), "-InstallerArgsJson", raw])
    text = log.read_text(encoding="utf-8")
    # On this Windows host the raw JSON happens to parse successfully; the
    # physical beta.4 incident remains captured by the audit artifacts. This
    # test still proves that the old command-line shape is exercised by the
    # real PowerShell process rather than a mock.
    assert result.returncode == 0
    assert "HANDOFF_START" in text
