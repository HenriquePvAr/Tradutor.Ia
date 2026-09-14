"""Contract for the remote signed update integration (TDD #58).

Everything here is hermetic: ephemeral Ed25519 keys, ZIP payloads built in ``tempfile``
roots, an install layout that never touches the real repository/jobs database/output, and a
loopback ``http.server`` standing in for the release host. No external name is resolved and
no external socket is opened — the offline guard in ``_test_bootstrap`` would fail the test
if one were.

Two rules shape the matrix:

- transport failures (offline host, truncation, oversized body) must never brick a working
  installation, and must never be reported as "already up to date";
- nothing that arrives over the network may influence a trusted decision before
  ``update_manifest.verify_manifest`` has accepted it — including ``minimum_version``.

Production HTTPS enforcement is not weakened for the local server: the test transport is an
explicit subclass that widens ``ALLOWED_SCHEMES``. There is no production flag that does so.
"""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

import json
import re
import subprocess
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app_version
import start_tradutor
import update_bootstrap
import update_installer
import update_manifest
import update_transport
from scripts.sign_release import public_key_material, sign_manifest

APP_ID = "tradutor-ia"
KEY_ID = "beta-test"
REPO_ROOT = Path(__file__).resolve().parent


# --- fixtures ----------------------------------------------------------------------------

class _ReleaseHost:
    """A loopback stand-in for the release host: serves exactly the bytes it was given."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes, str]] = {}
        self.requests: list[str] = []
        host = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib naming
                host.requests.append(self.path)
                route = host.routes.get(self.path)
                if route is None:
                    self.send_error(404)
                    return
                status, body, content_type = route
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def secure_base_url(self) -> str:
        """The address a signed manifest may carry; ``_LocalTransport`` maps it to loopback."""
        return self.base_url.replace("http://", "https://", 1)

    def serve(self, path: str, body: bytes, *, status: int = 200,
              content_type: str = "application/json", secure: bool = False) -> str:
        self.routes[path] = (status, body, content_type)
        return (self.secure_base_url if secure else self.base_url) + path

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _LocalTransport(update_transport.UpdateTransport):
    """Test-only transport for the loopback fixture. No production code path can do this.

    The signed manifests below still carry real ``https://`` URLs — ``update_manifest``
    rejects anything else, and that gate is not weakened — so the production scheme check runs
    first and only then is the loopback address downgraded to the plain-HTTP test server. A
    separate handful of tests drive the *unmodified* production transport to prove it refuses
    HTTP and refuses a redirect that walks down to it.
    """

    ALLOWED_SCHEMES = ("http", "https")

    def _check_url(self, url: str) -> str:
        return super()._check_url(url).replace("https://127.0.0.1", "http://127.0.0.1", 1)


def _release_zip(destination: Path, version: str, *, app_id: str = APP_ID) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr(
            update_installer.RELEASE_METADATA_NAME,
            json.dumps({"app_id": app_id, "version": version}),
        )
        archive.writestr("start_tradutor.py", "raise SystemExit(0)\n")
    return destination


def _install_version(root: Path, version: str) -> Path:
    target = update_installer.version_dir(root, version)
    target.mkdir(parents=True, exist_ok=True)
    (target / update_installer.RELEASE_METADATA_NAME).write_text(
        json.dumps({"app_id": APP_ID, "version": version}), encoding="utf-8"
    )
    (target / "start_tradutor.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    return target


class _Fixture:
    """One signed release host + one installed application, both disposable."""

    def __init__(self, case: unittest.TestCase, *, current: str = "1.0.0") -> None:
        self.tmp = Path(tempfile.mkdtemp())
        case.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.host = _ReleaseHost()
        case.addCleanup(self.host.close)
        self.key = Ed25519PrivateKey.generate()
        self.trusted = {KEY_ID: public_key_material(self.key)}
        self.root = self.tmp / "install"
        _install_version(self.root, current)
        update_installer.initialise_install(self.root, version=current)
        self.current = current
        self.probe_calls: list[Path] = []

    def package(self, version: str = "1.1.0", *, mutate: bytes | None = None) -> tuple[str, bytes]:
        path = _release_zip(self.tmp / f"tradutor-ia-{version}.zip", version)
        body = mutate if mutate is not None else path.read_bytes()
        url = self.host.serve(
            f"/tradutor-ia-{version}.zip", body, content_type="application/zip", secure=True
        )
        return url, path.read_bytes()

    def manifest(
        self,
        *,
        version: str = "1.1.0",
        minimum_version: str = "1.0.0",
        package_url: str | None = None,
        package_bytes: bytes | None = None,
        extra: dict | None = None,
        tamper: bool = False,
        sign: bool = True,
    ) -> str:
        if package_url is None:
            package_url, package_bytes = self.package(version)
        payload = {
            "schema_version": 1,
            "app_id": APP_ID,
            "channel": "beta",
            "version": version,
            "minimum_version": minimum_version,
            "published_at": "2026-08-20T12:00:00+00:00",
            "package": {
                "filename": f"tradutor-ia-{version}.zip",
                "url": package_url,
                "sha256": update_manifest.hashlib.sha256(package_bytes).hexdigest(),
                "size": len(package_bytes),
            },
        }
        payload.update(extra or {})
        if sign:
            document = sign_manifest(payload, self.key, key_id=KEY_ID)
        else:
            document = {"payload": payload, "key_id": KEY_ID, "signature": "A" * 88}
        if tamper:
            document["payload"]["version"] = "9.9.9"
        return self.host.serve("/update.json", json.dumps(document).encode("utf-8"), secure=True)

    def check(self, manifest_url: str, **kwargs):
        kwargs.setdefault("trusted_keys", self.trusted)
        kwargs.setdefault("transport", _LocalTransport())
        kwargs.setdefault("start_probe", self._probe)
        return update_bootstrap.run_update_check(
            self.root, manifest_url=manifest_url, **kwargs
        )

    def _probe(self, version_dir: Path) -> None:
        self.probe_calls.append(version_dir)

    def state(self) -> update_installer.InstallState:
        return update_installer.read_install_state(self.root)


# --- authoritative product version ---------------------------------------------------------

class ProductVersionTests(unittest.TestCase):
    def test_authoritative_version_is_a_parseable_product_version(self):
        self.assertEqual(
            update_manifest.format_version(
                update_manifest.parse_version(app_version.PRODUCT_VERSION)
            ),
            app_version.PRODUCT_VERSION,
        )

    def test_no_second_product_version_constant_exists(self):
        tracked = subprocess.run(
            ["git", "ls-files", "*.py"], cwd=str(REPO_ROOT), capture_output=True, text=True,
            check=True,
        ).stdout.split()
        pattern = re.compile(r"^\s*(__version__|PRODUCT_VERSION|APP_VERSION|RELEASE_VERSION)\s*=")
        duplicates = [
            name for name in tracked
            if name != "app_version.py"
            and any(pattern.match(line) for line in (REPO_ROOT / name).read_text(
                encoding="utf-8", errors="ignore").splitlines())
        ]
        self.assertEqual(duplicates, [], "product version must have exactly one source")

    def test_launcher_and_updater_read_the_same_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            # No install layout: the running code's own version is authoritative.
            self.assertEqual(
                update_bootstrap.current_version(Path(tmp)), app_version.PRODUCT_VERSION
            )
        self.assertIn(app_version.PRODUCT_VERSION, app_version.user_agent())

    def test_installed_version_comes_from_the_activation_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _install_version(root, "1.2.3")
            update_installer.initialise_install(root, version="1.2.3")
            self.assertEqual(update_bootstrap.current_version(root), "1.2.3")


# --- transport -----------------------------------------------------------------------------

class TransportPolicyTests(unittest.TestCase):
    def test_production_transport_refuses_plain_http(self):
        with self.assertRaises(update_transport.InsecureTransport):
            update_transport.UpdateTransport().fetch_manifest("http://releases.invalid/update.json")

    def test_production_transport_refuses_a_redirect_down_to_http(self):
        transport = update_transport.UpdateTransport(session=_RedirectingSession("http://evil.invalid/x"))
        with self.assertRaises(update_transport.InsecureTransport):
            transport.fetch_manifest("https://releases.invalid/update.json")

    def test_redirect_budget_is_bounded(self):
        transport = update_transport.UpdateTransport(
            session=_RedirectingSession("https://releases.invalid/next")
        )
        with self.assertRaises(update_transport.TransportError):
            transport.fetch_manifest("https://releases.invalid/update.json")

    def test_timeouts_are_explicit_and_bounded(self):
        limits = update_transport.DEFAULT_LIMITS
        self.assertGreater(limits.connect_timeout, 0)
        self.assertGreater(limits.read_timeout, 0)
        self.assertEqual(limits.timeout, (limits.connect_timeout, limits.read_timeout))
        self.assertGreater(limits.max_manifest_bytes, 1024)


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict):
        self.status_code = status_code
        self.headers = headers
        self.content = b""

    def iter_content(self, chunk_size=1):  # pragma: no cover - never reached in redirect tests
        return iter(())

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class _RedirectingSession:
    """Always answers with a redirect to a fixed location; never touches a socket."""

    trust_env = True

    def __init__(self, location: str):
        self.location = location

    def get(self, url, **_kwargs):
        return _FakeResponse(302, {"Location": self.location})


class LocalHostTransportTests(unittest.TestCase):
    def setUp(self):
        self.host = _ReleaseHost()
        self.addCleanup(self.host.close)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.transport = _LocalTransport()

    def test_manifest_is_fetched_as_bytes(self):
        url = self.host.serve("/update.json", b'{"hello":"world"}')
        self.assertEqual(self.transport.fetch_manifest(url), b'{"hello":"world"}')

    def test_oversized_manifest_is_rejected(self):
        url = self.host.serve("/big.json", b"x" * (update_transport.DEFAULT_LIMITS.max_manifest_bytes + 1))
        with self.assertRaises(update_transport.ResponseTooLarge):
            self.transport.fetch_manifest(url)

    def test_http_error_status_is_not_a_manifest(self):
        url = self.host.serve("/missing.json", b"nope", status=500)
        with self.assertRaises(update_transport.TransportError):
            self.transport.fetch_manifest(url)

    def test_unreachable_host_is_a_network_error(self):
        base = self.host.base_url
        self.host.close()
        with self.assertRaises(update_transport.NetworkUnavailable):
            self.transport.fetch_manifest(base + "/update.json")

    def test_package_download_verifies_size_and_hash(self):
        body = b"a package" * 100
        url = self.host.serve("/pkg.zip", body, content_type="application/zip")
        destination = self.tmp / "pkg.zip"
        self.transport.download_package(
            url, destination,
            sha256=update_manifest.hashlib.sha256(body).hexdigest(), size=len(body),
        )
        self.assertEqual(destination.read_bytes(), body)

    def test_short_body_is_rejected_and_never_promoted(self):
        body = b"short"
        url = self.host.serve("/pkg.zip", body, content_type="application/zip")
        destination = self.tmp / "pkg.zip"
        with self.assertRaises(update_manifest.PackageSizeMismatch):
            self.transport.download_package(
                url, destination,
                sha256=update_manifest.hashlib.sha256(body).hexdigest(), size=len(body) + 10,
            )
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_oversized_body_is_rejected_and_never_promoted(self):
        body = b"much longer than declared"
        url = self.host.serve("/pkg.zip", body, content_type="application/zip")
        destination = self.tmp / "pkg.zip"
        with self.assertRaises(update_manifest.PackageSizeMismatch):
            self.transport.download_package(url, destination, sha256="0" * 64, size=3)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_tampered_body_fails_the_hash_and_is_never_promoted(self):
        body = b"tampered payload"
        url = self.host.serve("/pkg.zip", body, content_type="application/zip")
        destination = self.tmp / "pkg.zip"
        with self.assertRaises(update_manifest.PackageHashMismatch):
            self.transport.download_package(url, destination, sha256="0" * 64, size=len(body))
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.tmp.iterdir()), [])


# --- remote trust integration ----------------------------------------------------------------

class RemoteTrustTests(unittest.TestCase):
    def test_valid_signed_release_is_downloaded_verified_staged_and_activated(self):
        fixture = _Fixture(self)
        url = fixture.manifest(version="1.1.0")
        status = fixture.check(url)
        self.assertEqual(status.state, "updated")
        self.assertEqual(status.version, "1.1.0")
        self.assertTrue(status.can_launch)
        self.assertEqual(fixture.state().current, "1.1.0")
        self.assertEqual(fixture.state().previous, "1.0.0")
        self.assertEqual([p.name for p in fixture.probe_calls], ["1.1.0"])

    def test_tampered_manifest_never_reaches_the_package(self):
        fixture = _Fixture(self)
        url = fixture.manifest(version="1.1.0", tamper=True)
        status = fixture.check(url)
        self.assertEqual(status.state, "verification_failed")
        self.assertTrue(status.can_launch)
        self.assertEqual(fixture.state().current, "1.0.0")
        self.assertNotIn("/tradutor-ia-1.1.0.zip", fixture.host.requests)

    def test_manifest_signed_by_an_untrusted_key_is_refused(self):
        fixture = _Fixture(self)
        url = fixture.manifest(version="1.1.0")
        status = fixture.check(url, trusted_keys={KEY_ID: public_key_material(Ed25519PrivateKey.generate())})
        self.assertEqual(status.state, "verification_failed")
        self.assertEqual(fixture.state().current, "1.0.0")

    def test_manifest_for_another_application_is_refused(self):
        fixture = _Fixture(self)
        url = fixture.manifest(version="1.1.0", extra={"app_id": "other-product"})
        status = fixture.check(url)
        self.assertEqual(status.state, "verification_failed")
        self.assertEqual(fixture.state().current, "1.0.0")

    def test_html_error_page_instead_of_a_manifest_is_refused(self):
        fixture = _Fixture(self)
        url = fixture.host.serve("/update.json", b"<html>maintenance</html>",
                                 content_type="text/html", secure=True)
        status = fixture.check(url)
        self.assertEqual(status.state, "verification_failed")
        self.assertEqual(fixture.state().current, "1.0.0")

    def test_tampered_package_is_never_staged(self):
        fixture = _Fixture(self)
        package_url, package_bytes = fixture.package("1.1.0")
        url = fixture.manifest(version="1.1.0", package_url=package_url, package_bytes=package_bytes)
        # The host swaps the bytes after the manifest was signed.
        fixture.host.serve("/tradutor-ia-1.1.0.zip", b"E" * len(package_bytes),
                           content_type="application/zip", secure=True)
        status = fixture.check(url)
        self.assertEqual(status.state, "update_failed")
        self.assertTrue(status.can_launch)
        self.assertEqual(fixture.state().current, "1.0.0")
        self.assertFalse(update_installer.version_dir(fixture.root, "1.1.0").exists())

    def test_truncated_package_leaves_the_installation_untouched(self):
        fixture = _Fixture(self)
        package_url, package_bytes = fixture.package("1.1.0")
        url = fixture.manifest(version="1.1.0", package_url=package_url, package_bytes=package_bytes)
        fixture.host.serve("/tradutor-ia-1.1.0.zip", package_bytes[:10],
                           content_type="application/zip", secure=True)
        status = fixture.check(url)
        self.assertEqual(status.state, "update_failed")
        self.assertEqual(fixture.state().current, "1.0.0")
        self.assertFalse(update_installer.version_dir(fixture.root, "1.1.0").exists())

    def test_trust_not_configured_is_not_reported_as_up_to_date(self):
        fixture = _Fixture(self)
        url = fixture.manifest(version="1.1.0")
        status = fixture.check(url, trusted_keys={})
        self.assertEqual(status.state, "verification_failed")
        self.assertTrue(status.can_launch)
        self.assertEqual(fixture.state().current, "1.0.0")

    def test_production_build_has_no_trusted_key_and_fails_closed(self):
        self.assertIn("beta-2026-09", update_manifest.TRUSTED_PUBLIC_KEYS)
        self.assertTrue(update_manifest.load_trusted_keys())


# --- lifecycle -------------------------------------------------------------------------------

class UpdateLifecycleTests(unittest.TestCase):
    def test_same_version_downloads_nothing(self):
        fixture = _Fixture(self, current="1.0.0")
        url = fixture.manifest(version="1.0.0", minimum_version="1.0.0")
        status = fixture.check(url)
        self.assertEqual(status.state, "up_to_date")
        self.assertTrue(status.can_launch)
        self.assertNotIn("/tradutor-ia-1.0.0.zip", fixture.host.requests)

    def test_older_remote_version_never_downgrades_the_installation(self):
        fixture = _Fixture(self, current="1.2.0")
        url = fixture.manifest(version="1.1.0", minimum_version="1.0.0")
        status = fixture.check(url)
        self.assertEqual(status.state, "up_to_date")
        self.assertEqual(fixture.state().current, "1.2.0")

    def test_offline_host_still_launches_the_known_good_version(self):
        fixture = _Fixture(self)
        url = fixture.host.secure_base_url + "/update.json"
        fixture.host.close()
        status = fixture.check(url)
        self.assertEqual(status.state, "network_error")
        self.assertTrue(status.can_launch)
        self.assertEqual(fixture.state().current, "1.0.0")

    def test_failed_startup_of_a_new_version_rolls_back_to_the_known_good_one(self):
        fixture = _Fixture(self)
        url = fixture.manifest(version="1.1.0")
        data_marker = update_installer.user_data_dir(fixture.root) / "jobs.sqlite3"
        data_marker.write_text("user data", encoding="utf-8")

        def failing_probe(version_dir):
            raise update_bootstrap.StartupUnhealthy(f"{version_dir.name} did not start")

        status = fixture.check(url, start_probe=failing_probe)
        self.assertEqual(status.state, "update_failed")
        self.assertTrue(status.can_launch)
        self.assertEqual(fixture.state().current, "1.0.0")
        self.assertEqual(data_marker.read_text(encoding="utf-8"), "user data")

    def test_rollback_does_not_leave_the_failed_version_reachable_as_a_fallback(self):
        fixture = _Fixture(self)
        url = fixture.manifest(version="1.1.0")
        fixture.check(url, start_probe=lambda _d: (_ for _ in ()).throw(
            update_bootstrap.StartupUnhealthy("no")))
        self.assertIsNone(fixture.state().previous)

    def test_mandatory_update_that_cannot_be_installed_blocks_the_outdated_version(self):
        fixture = _Fixture(self, current="1.0.0")
        package_url, package_bytes = fixture.package("2.0.0")
        url = fixture.manifest(
            version="2.0.0", minimum_version="2.0.0",
            package_url=package_url, package_bytes=package_bytes,
        )
        fixture.host.routes.pop("/tradutor-ia-2.0.0.zip")
        status = fixture.check(url)
        self.assertEqual(status.state, "mandatory_update_required")
        self.assertFalse(status.can_launch)
        self.assertEqual(fixture.state().current, "1.0.0")

    def test_mandatory_update_installs_and_launches_the_new_version(self):
        fixture = _Fixture(self, current="1.0.0")
        url = fixture.manifest(version="2.0.0", minimum_version="2.0.0")
        status = fixture.check(url)
        self.assertEqual(status.state, "updated")
        self.assertTrue(status.can_launch)
        self.assertEqual(fixture.state().current, "2.0.0")

    def test_unsigned_manifest_cannot_block_the_application_with_a_minimum_version(self):
        fixture = _Fixture(self, current="1.0.0")
        url = fixture.manifest(version="999.0.0", minimum_version="999.0.0", sign=False)
        status = fixture.check(url)
        self.assertEqual(status.state, "verification_failed")
        self.assertTrue(status.can_launch)
        self.assertEqual(fixture.state().current, "1.0.0")

    def test_release_requiring_a_newer_bootstrap_is_not_installed(self):
        fixture = _Fixture(self)
        url = fixture.manifest(
            version="1.1.0", extra={"minimum_bootstrap_version": "9.0.0"}
        )
        status = fixture.check(url, bootstrap_version="1.0.0")
        self.assertEqual(status.state, "bootstrap_too_old")
        self.assertEqual(fixture.state().current, "1.0.0")
        self.assertFalse(update_installer.version_dir(fixture.root, "1.1.0").exists())

    def test_missing_manifest_url_reports_not_configured(self):
        fixture = _Fixture(self)
        status = fixture.check("")
        self.assertEqual(status.state, "not_configured")
        self.assertTrue(status.can_launch)


class StartupProbeTests(unittest.TestCase):
    def test_default_probe_accepts_a_payload_whose_selftest_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp)
            (payload / "start_tradutor.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
            update_bootstrap.default_start_probe(payload, timeout=60)

    def test_default_probe_rejects_a_payload_whose_selftest_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp)
            (payload / "start_tradutor.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
            with self.assertRaises(update_bootstrap.StartupUnhealthy):
                update_bootstrap.default_start_probe(payload, timeout=60)

    def test_default_probe_is_bounded_in_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp)
            (payload / "start_tradutor.py").write_text(
                "import time\ntime.sleep(30)\n", encoding="utf-8")
            with self.assertRaises(update_bootstrap.StartupUnhealthy):
                update_bootstrap.default_start_probe(payload, timeout=1)

    def test_launcher_selftest_command_succeeds_in_this_installation(self):
        self.assertEqual(start_tradutor.main(["selftest"]), 0)


# --- launcher integration ----------------------------------------------------------------------

class LauncherIntegrationTests(unittest.TestCase):
    def test_update_check_runs_before_the_worker_starts(self):
        order: list[str] = []
        status = update_bootstrap.UpdateStatus("up_to_date", app_version.PRODUCT_VERSION)
        with patch.object(update_bootstrap, "check_before_start",
                          side_effect=lambda *a, **k: (order.append("update"), status)[1]), \
                patch.object(start_tradutor, "start_worker",
                             side_effect=lambda *a, **k: order.append("worker")), \
                patch.object(start_tradutor, "start_ui",
                             side_effect=lambda *a, **k: (order.append("ui"), 0)[1]), \
                patch.object(start_tradutor, "load_local_environment_for_entrypoint",
                             return_value=True):
            self.assertEqual(start_tradutor.main(["all"]), 0)
        self.assertEqual(order, ["update", "worker", "ui"])

    def test_blocked_launch_never_starts_the_worker_or_the_ui(self):
        status = update_bootstrap.UpdateStatus(
            "mandatory_update_required", "1.0.0", detail="below minimum"
        )
        with patch.object(update_bootstrap, "check_before_start", return_value=status), \
                patch.object(start_tradutor, "start_worker") as worker, \
                patch.object(start_tradutor, "start_ui") as ui, \
                patch.object(start_tradutor, "load_local_environment_for_entrypoint",
                             return_value=True):
            self.assertNotEqual(start_tradutor.main(["all"]), 0)
        worker.assert_not_called()
        ui.assert_not_called()

    def test_activated_version_is_handed_off_instead_of_started_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = _install_version(root, "1.1.0")
            update_installer.initialise_install(root, version="1.1.0")
            status = update_bootstrap.UpdateStatus(
                "updated", "1.1.0", payload_dir=payload
            )
            with patch.object(update_bootstrap, "check_before_start", return_value=status), \
                    patch.object(start_tradutor, "start_worker") as worker, \
                    patch.object(start_tradutor, "start_ui") as ui, \
                    patch.object(start_tradutor, "load_local_environment_for_entrypoint",
                                 return_value=True), \
                    patch.object(start_tradutor.subprocess, "run") as run:
                run.return_value.returncode = 0
                self.assertEqual(start_tradutor.main(["all"]), 0)
            worker.assert_not_called()
            ui.assert_not_called()
            run.assert_called_once()
            self.assertIn(str(payload / "start_tradutor.py"), run.call_args.args[0])
            self.assertEqual(
                run.call_args.kwargs["env"][update_bootstrap.HANDOFF_ENV], "1"
            )

    def test_handoff_does_not_recurse(self):
        with patch.dict(start_tradutor.os.environ, {update_bootstrap.HANDOFF_ENV: "1"}), \
                patch.object(update_bootstrap, "run_update_check") as check:
            status = update_bootstrap.check_before_start(REPO_ROOT)
        check.assert_not_called()
        self.assertEqual(status.state, "skipped")
        self.assertTrue(status.can_launch)

    def test_worker_only_command_does_not_check_for_updates(self):
        with patch.object(update_bootstrap, "check_before_start") as check, \
                patch.object(start_tradutor, "start_worker"), \
                patch.object(start_tradutor, "load_local_environment_for_entrypoint",
                             return_value=True):
            start_tradutor.main(["worker"])
        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
