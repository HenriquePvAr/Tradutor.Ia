from pathlib import Path
import tempfile
from unittest.mock import patch

import installer_update


ROOT = Path(__file__).resolve().parent
UI_SOURCE = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
APP_SOURCE = (ROOT / "app_ui.py").read_text(encoding="utf-8")


def test_handoff_waits_for_explicit_processes_before_installer():
    script = installer_update._handoff_script()
    assert "HANDOFF_TIMEOUT" in script
    assert "INSTALLER_SPAWN" in script
    assert "Get-Process -Id $_" in script
    assert "Start-Process -FilePath $Installer" in script


def test_handoff_is_external_and_receives_pid_list():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "YomuSekai-0.9.1-beta.4-Setup-x64.exe"
        path.write_bytes(b"fixture")
        with patch("installer_update.subprocess.Popen") as popen:
            installer_update.spawn_installer_after_processes_exit(path, owned_pids=[11, 12])
    args, kwargs = popen.call_args
    assert args[0][0].casefold() == "powershell.exe"
    assert "-OwnedPid" in args[0]
    assert "11" in args[0] and "12" in args[0]
    assert kwargs["shell"] is False


def test_physical_gesture_marker_is_opt_in_only():
    assert "window.__yomuInputAuditVisible === true" in UI_SOURCE
    assert "__yomuInputAuditVisible" in APP_SOURCE


def test_ready_control_plane_does_not_refresh_policy_on_every_click():
    marker = "TRANSLATION_POLICY_REFRESH_SKIPPED_READY"
    assert marker in UI_SOURCE
    assert "const readySnapshot = controlPlane.state.ready === true" in UI_SOURCE
