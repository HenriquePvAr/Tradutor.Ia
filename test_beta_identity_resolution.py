"""Beta license identity must survive isolated runtime roots."""

from pathlib import Path
from unittest import mock

import ui_bridge


def test_default_and_isolated_runtime_roots_share_canonical_identity(tmp_path: Path):
    canonical = "ys-canonical-installation"
    with mock.patch.object(ui_bridge.InstallIdentity, "device_id", return_value=canonical):
        default = ui_bridge._read_or_create_install_id(tmp_path / "default")
        isolated_a = ui_bridge._read_or_create_install_id(tmp_path / "a")
        isolated_b = ui_bridge._read_or_create_install_id(tmp_path / "b")

    assert default == isolated_a == isolated_b == canonical
    assert not (tmp_path / "a" / "install_id").exists()
    assert not (tmp_path / "b" / "install_id").exists()


def test_explicit_install_identity_override_remains_supported(tmp_path: Path):
    with mock.patch.object(ui_bridge.InstallIdentity, "device_id") as device_id:
        value = ui_bridge._read_or_create_install_id(
            tmp_path / "isolated",
            {"TRADUTOR_INSTALL_ID": "hermetic-install"},
        )

    assert value == "hermetic-install"
    device_id.assert_not_called()


def test_different_canonical_installations_keep_distinct_identities(tmp_path: Path):
    with mock.patch.object(ui_bridge.InstallIdentity, "device_id", return_value="ys-a"):
        first = ui_bridge._read_or_create_install_id(tmp_path / "a")
    with mock.patch.object(ui_bridge.InstallIdentity, "device_id", return_value="ys-b"):
        second = ui_bridge._read_or_create_install_id(tmp_path / "b")

    assert first != second
