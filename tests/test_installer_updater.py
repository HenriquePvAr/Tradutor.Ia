from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import installer_update
import update_manifest
from scripts.sign_release import public_key_material, sign_manifest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class _Transport:
    def __init__(self, source: bytes):
        self.source = source
        self.calls = []

    def download_package(self, url, destination, *, sha256, size):
        self.calls.append((url, destination, sha256, size))
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(self.source)
        update_manifest.verify_package(Path(destination), sha256=sha256, size=size)
        return Path(destination)


def _manifest(package: Path, *, artifact_type: str = "windows-installer"):
    key = Ed25519PrivateKey.generate()
    payload = {
        "schema_version": 1, "app_id": "tradutor-ia", "channel": "beta",
        "artifact_type": artifact_type, "version": "0.9.0-beta.35",
        "minimum_version": "0.9.0-beta.34", "published_at": "2026-09-14T12:00:00+00:00",
        "release_notes": "Atualização de teste", "package": {
            "filename": package.name, "url": "https://updates.example.invalid/" + package.name,
            "sha256": update_manifest.sha256_file(package), "size": package.stat().st_size,
        },
    }
    return update_manifest.verify_manifest(
        sign_manifest(payload, key, key_id="test-key"),
        trusted_keys={"test-key": public_key_material(key)},
    )


class InstallerUpdaterTests(unittest.TestCase):
    def test_installer_filename_is_version_bound(self):
        self.assertEqual(
            installer_update.installer_filename("0.9.0-beta.35"),
            "YomuSekai-0.9.0-beta.35-Setup-x64.exe",
        )

    def test_download_promotes_only_after_transport_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "YomuSekai-0.9.0-beta.35-Setup-x64.exe"
            source.write_bytes(b"verified installer")
            manifest = _manifest(source)
            transport = _Transport(source.read_bytes())
            result = installer_update.download_verified_installer(
                manifest, transport, data_root=Path(tmp) / "data"
            )
            self.assertTrue(result.is_file())
            self.assertEqual(result.name, source.name)
            self.assertFalse(result.with_name(result.name + ".download").exists())

    def test_zip_artifact_is_not_accepted_as_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "YomuSekai-0.9.0-beta.35-Setup-x64.exe"
            package.write_bytes(b"fixture")
            manifest = _manifest(package, artifact_type="zip")
            with self.assertRaises(installer_update.InstallerUpdateError):
                installer_update.download_verified_installer(manifest, _Transport(b"fixture"), data_root=Path(tmp))

    def test_spawn_uses_argument_list_without_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "YomuSekai-0.9.0-beta.35-Setup-x64.exe"
            path.write_bytes(b"fixture")
            with patch("installer_update.subprocess.Popen") as popen:
                installer_update.spawn_verified_installer(path)
                args, kwargs = popen.call_args
            self.assertEqual(args[0][0], str(path))
            self.assertIn("/CLOSEAPPLICATIONS", args[0])
            self.assertFalse(kwargs["shell"])
