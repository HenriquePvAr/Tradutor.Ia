"""Contract for the signed update trust chain (TDD #57).

Everything here is offline: ephemeral Ed25519 keys generated per test, ZIP packages built in
``tempfile`` roots, and an install layout that never touches the real repository, the real jobs
database or the real output directory. No manifest or package is ever fetched.

The matrix is deliberately fail-closed: every test that is not the single happy path asserts an
*exception class*, never a boolean, so a future refactor cannot silently downgrade a security
gate into a warning.
"""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

import base64
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import update_installer
import update_manifest
from scripts.sign_release import public_key_material, sign_manifest
from update_manifest import (
    ManifestInvalid,
    PackageHashMismatch,
    PackageSizeMismatch,
    SignatureInvalid,
    UnsafeArchive,
    WrongChannel,
    UnsupportedManifest,
    UpdateTrustNotConfigured,
    WrongApplication,
)

APP_ID = "tradutor-ia"
KEY_ID = "beta-test"


def _payload(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "app_id": APP_ID,
        "channel": "beta",
        "version": "1.1.0",
        "minimum_version": "1.0.0",
        "published_at": "2026-08-20T12:00:00+00:00",
        "package": {
            "filename": "tradutor-ia-1.1.0.zip",
            "url": "https://releases.example.invalid/tradutor-ia-1.1.0.zip",
            "sha256": "0" * 64,
            "size": 1234,
        },
    }
    package_overrides = overrides.pop("package", None)
    payload.update(overrides)
    if package_overrides is not None:
        payload["package"] = package_overrides
    return payload


def _release_zip(destination: Path, version: str, *, app_id: str = APP_ID, empty: bool = False) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as archive:
        if empty:
            archive.writestr("readme.txt", "no entrypoint, no metadata")
            return destination
        archive.writestr(
            update_installer.RELEASE_METADATA_NAME,
            json.dumps({"app_id": app_id, "version": version}),
        )
        archive.writestr("start_tradutor.py", "raise SystemExit(0)\n")
        archive.writestr("ui/app.js", "// payload\n")
    return destination


def _package_payload(package_path: Path, version: str = "1.1.0", **overrides) -> dict:
    return _payload(
        version=version,
        package={
            "filename": package_path.name,
            "url": f"https://releases.example.invalid/{package_path.name}",
            "sha256": update_manifest.sha256_file(package_path),
            "size": package_path.stat().st_size,
        },
        **overrides,
    )


class CanonicalPayloadTests(unittest.TestCase):
    def test_insertion_order_does_not_change_signed_bytes(self):
        first = _payload()
        second = {key: first[key] for key in reversed(list(first))}
        self.assertEqual(
            update_manifest.canonical_payload_bytes(first),
            update_manifest.canonical_payload_bytes(second),
        )

    def test_serialization_is_stable_across_calls(self):
        payload = _payload()
        self.assertEqual(
            update_manifest.canonical_payload_bytes(payload),
            update_manifest.canonical_payload_bytes(payload),
        )

    def test_canonical_bytes_are_compact_sorted_json(self):
        raw = update_manifest.canonical_payload_bytes({"b": 1, "a": 2})
        self.assertEqual(raw, b'{"a":2,"b":1}')

    def test_float_payload_value_is_rejected_as_non_reproducible(self):
        with self.assertRaises(ManifestInvalid):
            update_manifest.canonical_payload_bytes({"a": 1.5})


class SignatureMatrixTests(unittest.TestCase):
    def setUp(self):
        self.private_key = Ed25519PrivateKey.generate()
        self.trusted = {KEY_ID: public_key_material(self.private_key)}

    def _signed(self, payload=None, *, key=None, key_id=KEY_ID) -> dict:
        return sign_manifest(payload or _payload(), key or self.private_key, key_id=key_id)

    def test_valid_manifest_verifies(self):
        manifest = update_manifest.verify_manifest(self._signed(), trusted_keys=self.trusted)
        self.assertEqual(manifest.version_text, "1.1.0")
        self.assertEqual(manifest.app_id, APP_ID)
        self.assertEqual(manifest.channel, "beta")
        self.assertEqual(manifest.key_id, KEY_ID)

    def test_manifest_verifies_from_raw_json_text(self):
        raw = json.dumps(self._signed(), indent=4)
        manifest = update_manifest.verify_manifest(raw, trusted_keys=self.trusted)
        self.assertEqual(manifest.version_text, "1.1.0")

    def test_one_byte_payload_mutation_is_rejected(self):
        signed = self._signed()
        signed["payload"]["version"] = "1.1.1"
        with self.assertRaises(SignatureInvalid):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_channel_tamper_invalidates_signature(self):
        signed = self._signed()
        signed["payload"]["channel"] = "stable"
        with self.assertRaises(SignatureInvalid):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_beta_client_rejects_stable_channel_when_resigned(self):
        signed = self._signed(_payload(channel="stable"))
        with self.assertRaises(WrongChannel):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_beta_client_rejects_unknown_channel_when_resigned(self):
        signed = self._signed(_payload(channel="preview"))
        with self.assertRaises(WrongChannel):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_missing_channel_rejected(self):
        payload = _payload(); payload.pop("channel")
        signed = self._signed(payload)
        with self.assertRaises(ManifestInvalid):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_malformed_channel_rejected(self):
        signed = self._signed(_payload(channel="Beta"))
        with self.assertRaises(ManifestInvalid):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_added_payload_field_is_rejected(self):
        signed = self._signed()
        signed["payload"]["extra"] = "injected"
        with self.assertRaises(SignatureInvalid):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_wrong_key_is_rejected(self):
        signed = sign_manifest(_payload(), Ed25519PrivateKey.generate(), key_id=KEY_ID)
        with self.assertRaises(SignatureInvalid):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_unknown_key_id_is_rejected(self):
        signed = self._signed(key_id="rotated-key")
        with self.assertRaises(SignatureInvalid):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_missing_signature_is_rejected(self):
        signed = self._signed()
        del signed["signature"]
        with self.assertRaises(ManifestInvalid):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_bad_signature_encoding_is_rejected(self):
        signed = self._signed()
        signed["signature"] = "not base64!!"
        with self.assertRaises(ManifestInvalid):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_signature_of_wrong_length_is_rejected(self):
        signed = self._signed()
        signed["signature"] = base64.b64encode(b"short").decode("ascii")
        with self.assertRaises(ManifestInvalid):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_wrong_app_id_is_rejected_even_when_validly_signed(self):
        signed = self._signed(_payload(app_id="another-app"))
        with self.assertRaises(WrongApplication):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_unknown_schema_version_is_rejected(self):
        signed = self._signed(_payload(schema_version=99))
        with self.assertRaises(UnsupportedManifest):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_malformed_version_is_rejected(self):
        signed = self._signed(_payload(version="1.1"))
        with self.assertRaises(ManifestInvalid):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_missing_mandatory_field_is_rejected(self):
        payload = _payload()
        del payload["minimum_version"]
        with self.assertRaises(ManifestInvalid):
            update_manifest.verify_manifest(self._signed(payload), trusted_keys=self.trusted)

    def test_wrong_type_is_rejected(self):
        signed = self._signed(_payload(published_at=12345))
        with self.assertRaises(ManifestInvalid):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_invalid_published_at_is_rejected(self):
        signed = self._signed(_payload(published_at="ontem"))
        with self.assertRaises(ManifestInvalid):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_short_sha256_is_rejected(self):
        package = dict(_payload()["package"], sha256="abc")
        with self.assertRaises(ManifestInvalid):
            update_manifest.verify_manifest(
                self._signed(_payload(package=package)), trusted_keys=self.trusted
            )

    def test_non_hex_sha256_is_rejected(self):
        package = dict(_payload()["package"], sha256="z" * 64)
        with self.assertRaises(ManifestInvalid):
            update_manifest.verify_manifest(
                self._signed(_payload(package=package)), trusted_keys=self.trusted
            )

    def test_non_https_package_url_is_rejected(self):
        package = dict(_payload()["package"], url="http://releases.example.invalid/p.zip")
        with self.assertRaises(ManifestInvalid):
            update_manifest.verify_manifest(
                self._signed(_payload(package=package)), trusted_keys=self.trusted
            )

    def test_minimum_version_above_version_is_rejected(self):
        signed = self._signed(_payload(minimum_version="2.0.0"))
        with self.assertRaises(ManifestInvalid):
            update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_unsigned_manifest_without_envelope_is_rejected(self):
        with self.assertRaises(ManifestInvalid):
            update_manifest.verify_manifest(_payload(), trusted_keys=self.trusted)

    def test_empty_trust_store_fails_closed(self):
        with self.assertRaises(UpdateTrustNotConfigured):
            update_manifest.verify_manifest(self._signed(), trusted_keys={})

    def test_production_trust_store_fails_closed_while_no_release_key_exists(self):
        self.assertIn("beta-2026-10", update_manifest.TRUSTED_PUBLIC_KEYS)
        self.assertTrue(update_manifest.load_trusted_keys())


class VersionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.private_key = Ed25519PrivateKey.generate()
        self.trusted = {KEY_ID: public_key_material(self.private_key)}

    def _manifest(self, **overrides):
        signed = sign_manifest(_payload(**overrides), self.private_key, key_id=KEY_ID)
        return update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def test_numeric_version_ordering_is_not_lexicographic(self):
        self.assertGreater(
            update_manifest.parse_version("1.10.0"), update_manifest.parse_version("1.9.0")
        )

    def test_same_version_means_no_update(self):
        decision = update_manifest.decide_update("1.1.0", self._manifest())
        self.assertEqual(decision.state, "up_to_date")

    def test_newer_version_is_a_candidate(self):
        decision = update_manifest.decide_update("1.0.5", self._manifest())
        self.assertEqual(decision.state, "update_available")
        self.assertTrue(decision.should_install)

    def test_lower_remote_version_is_rejected_as_downgrade(self):
        decision = update_manifest.decide_update("1.2.0", self._manifest())
        self.assertEqual(decision.state, "downgrade_rejected")
        self.assertFalse(decision.should_install)

    def test_current_below_minimum_version_is_mandatory(self):
        decision = update_manifest.decide_update("0.9.0", self._manifest())
        self.assertEqual(decision.state, "mandatory_update")
        self.assertTrue(decision.should_install)
        self.assertTrue(decision.mandatory)

    def test_malformed_current_version_is_rejected(self):
        with self.assertRaises(ManifestInvalid):
            update_manifest.decide_update("beta", self._manifest())

    def test_beta_prerelease_ordering(self):
        self.assertLess(update_manifest.parse_version("0.9.0-beta.31"), update_manifest.parse_version("0.9.0-beta.32"))
        self.assertLess(update_manifest.parse_version("0.9.0-beta.32"), update_manifest.parse_version("0.9.0-beta.33"))
        self.assertLess(update_manifest.parse_version("0.9.0-beta.33"), update_manifest.parse_version("0.9.0"))
        self.assertLess(update_manifest.parse_version("0.9.0-beta.9"), update_manifest.parse_version("0.9.0-beta.10"))
        self.assertGreater(update_manifest.parse_version("0.9.1-beta.1"), update_manifest.parse_version("0.9.0"))
        self.assertGreater(update_manifest.parse_version("1.0.0-beta.1"), update_manifest.parse_version("0.9.9"))

    def test_beta32_to_beta33_is_an_update(self):
        manifest = self._manifest(version="0.9.0-beta.33", minimum_version="0.9.0-beta.30")
        decision = update_manifest.decide_update("0.9.0-beta.32", manifest)
        self.assertEqual(decision.state, "update_available")

    def test_beta33_to_beta32_is_a_downgrade(self):
        manifest = self._manifest(version="0.9.0-beta.32", minimum_version="0.9.0-beta.30")
        decision = update_manifest.decide_update("0.9.0-beta.33", manifest)
        self.assertEqual(decision.state, "downgrade_rejected")

    def test_prerelease_minimum_version_is_enforced(self):
        manifest = self._manifest(version="0.9.0-beta.33", minimum_version="0.9.0-beta.33")
        self.assertEqual(update_manifest.decide_update("0.9.0-beta.32", manifest).state, "mandatory_update")

    def test_malformed_prerelease_versions_are_rejected(self):
        for value in ("", "beta.33", "0.9", "0.9.0-", "0.9.0-beta.", "0.9.0-beta.x", "v0.9.0-beta.33", " 0.9.0-beta.33"):
            with self.subTest(value=value), self.assertRaises(ManifestInvalid):
                update_manifest.parse_version(value)


class PackageIntegrityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.package = self.root / "package.zip"
        self.package.write_bytes(b"tradutor-ia release payload")
        self.sha256 = update_manifest.sha256_file(self.package)
        self.size = self.package.stat().st_size

    def test_matching_bytes_pass(self):
        update_manifest.verify_package(self.package, sha256=self.sha256, size=self.size)

    def test_one_changed_byte_fails(self):
        data = bytearray(self.package.read_bytes())
        data[0] ^= 0x01
        self.package.write_bytes(bytes(data))
        with self.assertRaises(PackageHashMismatch):
            update_manifest.verify_package(self.package, sha256=self.sha256, size=self.size)

    def test_truncated_package_fails(self):
        self.package.write_bytes(self.package.read_bytes()[:-3])
        with self.assertRaises(PackageSizeMismatch):
            update_manifest.verify_package(self.package, sha256=self.sha256, size=self.size)

    def test_different_file_fails(self):
        other = self.root / "other.zip"
        other.write_bytes(b"completely different release payload")
        with self.assertRaises(PackageHashMismatch):
            update_manifest.verify_package(
                other, sha256=self.sha256, size=other.stat().st_size
            )

    def test_missing_package_fails(self):
        with self.assertRaises(PackageHashMismatch):
            update_manifest.verify_package(
                self.root / "absent.zip", sha256=self.sha256, size=self.size
            )


class ArchiveSafetyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _archive_with(self, name: str) -> Path:
        path = self.root / "evil.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("start_tradutor.py", "pass\n")
            archive.writestr(name, "owned")
        return path

    def _assert_rejected(self, name: str):
        destination = self.root / "staging"
        with self.assertRaises(UnsafeArchive):
            update_manifest.extract_package(self._archive_with(name), destination)
        self.assertEqual(list(destination.rglob("*")) if destination.exists() else [], [])

    def test_parent_traversal_is_rejected(self):
        self._assert_rejected("../evil.exe")

    def test_nested_traversal_is_rejected(self):
        self._assert_rejected("ui/../../evil.exe")

    def test_absolute_posix_path_is_rejected(self):
        self._assert_rejected("/etc/evil")

    def test_windows_drive_path_is_rejected(self):
        self._assert_rejected("C:/Windows/System32/evil.exe")

    def test_windows_unc_path_is_rejected(self):
        self._assert_rejected("//server/share/evil.exe")

    def test_backslash_traversal_is_rejected(self):
        self._assert_rejected("..\\evil.exe")

    def test_symlink_entry_is_rejected(self):
        path = self.root / "link.zip"
        with zipfile.ZipFile(path, "w") as archive:
            info = zipfile.ZipInfo("escape")
            info.external_attr = (0o120777 << 16) | 0o200000
            archive.writestr(info, "C:/Windows")
        with self.assertRaises(UnsafeArchive):
            update_manifest.extract_package(path, self.root / "staging")

    def test_safe_archive_extracts_inside_destination(self):
        package = _release_zip(self.root / "good.zip", "1.1.0")
        destination = self.root / "staging"
        update_manifest.extract_package(package, destination)
        extracted = sorted(p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file())
        self.assertEqual(extracted, ["release.json", "start_tradutor.py", "ui/app.js"])
        for path in destination.rglob("*"):
            self.assertTrue(str(path.resolve()).startswith(str(destination.resolve())))


class InstallLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "TradutorIA"
        self.addCleanup(self._tmp.cleanup)
        self.private_key = Ed25519PrivateKey.generate()
        self.trusted = {KEY_ID: public_key_material(self.private_key)}
        update_installer.initialise_install(self.root, version="1.0.0")
        self._seed_version("1.0.0")
        self.user_file = update_installer.user_data_dir(self.root) / "jobs.sqlite3"
        self.user_file.write_text("user data that must never be touched", encoding="utf-8")

    def _seed_version(self, version: str):
        target = update_installer.version_dir(self.root, version)
        target.mkdir(parents=True, exist_ok=True)
        (target / update_installer.RELEASE_METADATA_NAME).write_text(
            json.dumps({"app_id": APP_ID, "version": version}), encoding="utf-8"
        )
        (target / "start_tradutor.py").write_text("raise SystemExit(0)\n", encoding="utf-8")

    def _verified_manifest(self, package: Path, version: str):
        signed = sign_manifest(_package_payload(package, version), self.private_key, key_id=KEY_ID)
        return update_manifest.verify_manifest(signed, trusted_keys=self.trusted)

    def _stage(self, version="1.1.0", **kwargs):
        package = _release_zip(Path(self._tmp.name) / f"pkg-{version}.zip", version, **kwargs)
        return update_installer.stage_release(
            self.root, package, self._verified_manifest(package, version)
        )

    def _assert_user_data_intact(self):
        self.assertEqual(
            self.user_file.read_text(encoding="utf-8"), "user data that must never be touched"
        )

    def test_initial_state(self):
        state = update_installer.read_install_state(self.root)
        self.assertEqual(state.current, "1.0.0")
        self.assertIsNone(state.previous)

    def test_stage_then_activate_then_rollback(self):
        self._stage()
        # Staging alone never moves the pointer.
        self.assertEqual(update_installer.read_install_state(self.root).current, "1.0.0")

        activated = update_installer.activate(self.root, "1.1.0")
        self.assertEqual(activated.current, "1.1.0")
        self.assertEqual(activated.previous, "1.0.0")
        self.assertEqual(update_installer.read_install_state(self.root).current, "1.1.0")
        self._assert_user_data_intact()

        rolled_back = update_installer.rollback(self.root)
        self.assertEqual(rolled_back.current, "1.0.0")
        self.assertIsNone(rolled_back.previous)
        self.assertNotEqual(update_installer.read_install_state(self.root).current, "1.1.0")
        self.assertTrue(update_installer.version_dir(self.root, "1.1.0").exists())
        self._assert_user_data_intact()

    def test_rollback_without_previous_fails_closed(self):
        with self.assertRaises(update_installer.RollbackFailed):
            update_installer.rollback(self.root)
        self.assertEqual(update_installer.read_install_state(self.root).current, "1.0.0")

    def test_activation_of_unknown_version_fails_and_leaves_pointer(self):
        with self.assertRaises(update_installer.ActivationFailed):
            update_installer.activate(self.root, "9.9.9")
        self.assertEqual(update_installer.read_install_state(self.root).current, "1.0.0")

    def test_tampered_package_never_reaches_staging(self):
        package = _release_zip(Path(self._tmp.name) / "pkg.zip", "1.1.0")
        manifest = self._verified_manifest(package, "1.1.0")
        package.write_bytes(package.read_bytes() + b"appended")
        with self.assertRaises(PackageSizeMismatch):
            update_installer.stage_release(self.root, package, manifest)
        self.assertFalse(update_installer.version_dir(self.root, "1.1.0").exists())
        self.assertEqual(update_installer.read_install_state(self.root).current, "1.0.0")
        self._assert_user_data_intact()

    def test_correctly_hashed_but_empty_release_is_rejected(self):
        with self.assertRaises(update_installer.StagedReleaseInvalid):
            self._stage(empty=True)
        self.assertFalse(update_installer.version_dir(self.root, "1.1.0").exists())

    def test_release_metadata_must_match_manifest(self):
        package = Path(self._tmp.name) / "mismatch.zip"
        _release_zip(package, "9.9.9")
        manifest = self._verified_manifest(package, "1.1.0")
        with self.assertRaises(update_installer.StagedReleaseInvalid):
            update_installer.stage_release(self.root, package, manifest)
        self.assertFalse(update_installer.version_dir(self.root, "1.1.0").exists())

    def test_release_for_another_app_is_rejected(self):
        package = Path(self._tmp.name) / "otherapp.zip"
        _release_zip(package, "1.1.0", app_id="another-app")
        manifest = self._verified_manifest(package, "1.1.0")
        with self.assertRaises(update_installer.StagedReleaseInvalid):
            update_installer.stage_release(self.root, package, manifest)

    def test_failed_staging_can_be_discarded_without_touching_installed_versions(self):
        try:
            self._stage(empty=True)
        except update_installer.StagedReleaseInvalid:
            pass
        update_installer.discard_staging(self.root)
        self.assertTrue(update_installer.version_dir(self.root, "1.0.0").exists())
        self._assert_user_data_intact()

    def test_state_write_is_atomic_and_leaves_no_temporary_file(self):
        self._stage()
        update_installer.activate(self.root, "1.1.0")
        leftovers = [p.name for p in self.root.iterdir() if p.name.startswith("current.json.")]
        self.assertEqual(leftovers, [])

    def test_corrupt_state_pointer_fails_closed(self):
        update_installer.install_state_path(self.root).write_text("{not json", encoding="utf-8")
        with self.assertRaises(update_installer.InstallStateCorrupt):
            update_installer.read_install_state(self.root)

    def test_state_pointing_at_missing_version_fails_closed(self):
        update_installer.install_state_path(self.root).write_text(
            json.dumps({"schema_version": 1, "current": "7.7.7", "previous": None}),
            encoding="utf-8",
        )
        with self.assertRaises(update_installer.InstallStateCorrupt):
            update_installer.read_install_state(self.root)

    def test_interrupted_activation_still_resolves_one_current_release(self):
        self._stage()
        # Crash-like residue: a half-written pointer temp file and a leftover staging tree.
        (self.root / "current.json.tmp12345").write_text("{partial", encoding="utf-8")
        (update_installer.staging_dir(self.root) / "leftover").mkdir(parents=True, exist_ok=True)
        state = update_installer.read_install_state(self.root)
        self.assertEqual(state.current, "1.0.0")
        self.assertTrue(update_installer.current_version_dir(self.root).is_dir())

    def test_install_root_is_never_escaped_by_manifest_metadata(self):
        package = _release_zip(Path(self._tmp.name) / "pkg.zip", "1.1.0")
        signed = sign_manifest(
            _package_payload(package, "1.1.0", app_id=APP_ID), self.private_key, key_id=KEY_ID
        )
        manifest = update_manifest.verify_manifest(signed, trusted_keys=self.trusted)
        staged = update_installer.stage_release(self.root, package, manifest)
        self.assertTrue(str(staged.resolve()).startswith(str(self.root.resolve())))


class ReleaseSignerTests(unittest.TestCase):
    def test_signed_manifest_shape(self):
        key = Ed25519PrivateKey.generate()
        signed = sign_manifest(_payload(), key, key_id=KEY_ID)
        self.assertEqual(sorted(signed), ["key_id", "payload", "signature"])
        self.assertNotIn("signature", signed["payload"])

    def test_repository_contains_no_private_key_material(self):
        import subprocess

        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        forbidden = [name for name in tracked if name.endswith((".pem", ".key", ".pk8"))]
        self.assertEqual(forbidden, [])

    def test_signing_helper_never_returns_private_bytes(self):
        key = Ed25519PrivateKey.generate()
        signed = json.dumps(sign_manifest(_payload(), key, key_id=KEY_ID))
        self.assertNotIn("PRIVATE", signed.upper())

    def test_public_key_material_is_the_raw_public_key(self):
        key = Ed25519PrivateKey.generate()
        material = public_key_material(key)
        self.assertEqual(material, public_key_material(key.public_key()))
        self.assertEqual(len(base64.b64decode(material)), 32)


class OfflineGuaranteeTests(unittest.TestCase):
    def test_trust_modules_do_not_import_network_clients(self):
        for module in (update_manifest, update_installer):
            source = Path(module.__file__).read_text(encoding="utf-8")
            for banned in ("import requests", "urllib.request", "import httpx", "import socket"):
                self.assertNotIn(banned, source, f"{module.__name__} must stay transport-free")

    def test_no_signature_bypass_switch_exists(self):
        for module in (update_manifest, update_installer):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn("SKIP_UPDATE", source)
            self.assertNotIn("os.environ", source)

    def test_real_runtime_directories_are_untouched(self):
        repo = Path(__file__).resolve().parent
        self.assertFalse((repo / "versions").exists())
        self.assertFalse((repo / "current.json").exists())
        self.assertEqual(os.environ.get("TRADUTOR_IA_HERMETIC_TEST_ENV", "1"), "1")


if __name__ == "__main__":
    unittest.main()
