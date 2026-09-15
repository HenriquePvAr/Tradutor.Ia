"""Bounded HTTPS transport for the signed updater (TDD #58).

This module moves bytes and nothing else. It does not know what a signature is, never decides
whether an update should be installed, and never touches the installed application — that is
``update_manifest`` (trust) and ``update_installer`` (installation). Keeping transport out of
the trust chain is what lets the whole #57 security matrix stay testable with no socket in
sight.

**HTTPS is not a trust root here.** A perfectly valid TLS session only proves who served the
bytes, not who *built* them; a compromised or substituted release host is exactly the threat
the Ed25519 signature exists for. So this module downloads, and ``update_bootstrap`` refuses to
stage anything the signed manifest has not already described.

The ceilings mirror ``download_transport.DownloadLimits``, which solved the same problem for
chapter images: explicit connect/read timeouts, a redirect budget with **every hop
re-validated** (a 302 from ``https`` to ``http`` is a downgrade attack, not a detail), and a
byte ceiling so a hostile or broken host cannot turn one request into unbounded memory.
Redirects are followed manually (``allow_redirects=False``) precisely so that re-validation
cannot be skipped.

``ALLOWED_SCHEMES`` is a class attribute rather than a constructor flag on purpose: there is no
way to ask a production ``UpdateTransport`` for plain HTTP. The loopback test server is reached
by a subclass that lives in the test file, so no shipped code path can be talked into TLS-less
transport, and TLS verification is never disabled.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from app_version import user_agent
from update_manifest import PackageHashMismatch, PackageSizeMismatch, UpdateError

log = logging.getLogger(__name__)


class TransportError(UpdateError):
    """The bytes could not be obtained. Never a verdict about their authenticity."""


class InsecureTransport(TransportError):
    """A URL — or a redirect hop — that is not HTTPS. Fatal, never retried."""


class ResponseTooLarge(TransportError):
    """The response exceeded the ceiling for this kind of artifact."""


class NetworkUnavailable(TransportError):
    """Timeout, DNS or connection failure: transient, and never fatal for a running install."""


@dataclass(frozen=True)
class TransportLimits:
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    max_redirects: int = 3
    #: A manifest is a few hundred bytes of JSON; 64 KiB is ~100x the real thing, so it can
    #: absorb key rotation and future fields while still making an unbounded body impossible.
    #: The package has no constant of its own: its exact size comes from the signed manifest.
    max_manifest_bytes: int = 64 * 1024
    #: One extra attempt for a genuinely transient transport failure. Verification failures are
    #: never retried — they are answers, not accidents.
    attempts: int = 2
    retry_delay: float = 0.5

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout, self.read_timeout)


DEFAULT_LIMITS = TransportLimits()

_DOWNLOAD_CHUNK = 256 * 1024


class UpdateTransport:
    """Fetches update artifacts over HTTPS, with every ceiling applied."""

    #: Production accepts exactly one scheme. Widened only by a test-only subclass.
    ALLOWED_SCHEMES: tuple[str, ...] = ("https",)

    def __init__(self, *, session: requests.Session | None = None,
                 limits: TransportLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits
        self._session = session or requests.Session()
        # Ignore machine proxy/CA environment: the update path must behave identically on
        # every tester's machine, and an env-configured proxy is an unreviewed middlebox.
        self._session.trust_env = False

    # --- public API ----------------------------------------------------------------------

    def fetch_manifest(self, url: str) -> bytes:
        """Return the raw manifest bytes. Parsing and verification happen elsewhere."""
        with self._open(url, stream=True) as response:
            body = bytearray()
            for chunk in response.iter_content(_DOWNLOAD_CHUNK):
                body.extend(chunk)
                if len(body) > self.limits.max_manifest_bytes:
                    raise ResponseTooLarge(
                        f"manifest exceeds {self.limits.max_manifest_bytes} bytes"
                    )
        log.info("update_manifest_fetched", extra={"bytes": len(body)})
        return bytes(body)

    def download_package(self, url: str, destination: Path, *, sha256: str, size: int,
                         progress_callback=None, cancel_event=None) -> Path:
        """Stream the package to a temporary file; promote it only once it is provably right.

        Size and hash both come from the already-verified signed manifest. The file is hashed
        as it arrives (one pass, never held in memory), the read stops the moment the declared
        size is exceeded, and a failure at any point deletes the partial file — so nothing that
        was not fully proven can ever be mistaken for a downloaded release.
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            dir=str(destination.parent), prefix=destination.name + ".", suffix=".part"
        )
        digest = hashlib.sha256()
        received = 0
        try:
            with os.fdopen(handle, "wb") as stream, self._open(url, stream=True) as response:
                for chunk in response.iter_content(_DOWNLOAD_CHUNK):
                    if cancel_event is not None and cancel_event.is_set():
                        raise TransportError("update download cancelled")
                    received += len(chunk)
                    if received > size:
                        raise PackageSizeMismatch(f"package is larger than the declared {size}")
                    digest.update(chunk)
                    stream.write(chunk)
                    if progress_callback is not None:
                        progress_callback(received, size)
            if received != size:
                raise PackageSizeMismatch(f"package size {received} != declared {size}")
            if digest.hexdigest() != sha256:
                raise PackageHashMismatch("package sha256 does not match the signed manifest")
            os.replace(temp_name, destination)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        log.info("update_package_downloaded", extra={"bytes": received})
        return destination

    # --- internals -----------------------------------------------------------------------

    def _check_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme.casefold() not in self.ALLOWED_SCHEMES or not parsed.hostname:
            raise InsecureTransport(f"update transport requires https: {parsed.scheme or url!r}")
        return url

    def _open(self, url: str, *, stream: bool) -> requests.Response:
        """One request, redirects followed manually so every hop is re-validated."""
        current = self._check_url(url)
        headers = {"User-Agent": user_agent(), "Accept-Encoding": "identity"}
        for _hop in range(self.limits.max_redirects + 1):
            response = self._request(current, headers=headers, stream=stream)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                response.close()
                if not location:
                    raise TransportError("redirect without a location")
                current = self._check_url(urljoin(current, location))
                continue
            if response.status_code != 200:
                status = response.status_code
                response.close()
                raise TransportError(f"release host answered HTTP {status}")
            return response
        raise TransportError("too many redirects")

    def _request(self, url: str, *, headers: dict, stream: bool) -> requests.Response:
        last: Exception | None = None
        for attempt in range(self.limits.attempts):
            try:
                return self._session.get(
                    url, headers=headers, timeout=self.limits.timeout,
                    stream=stream, allow_redirects=False,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last = exc
                if attempt + 1 < self.limits.attempts:
                    time.sleep(self.limits.retry_delay)
            except requests.RequestException as exc:
                raise TransportError(f"update request failed: {exc}") from exc
        raise NetworkUnavailable(f"release host unreachable: {last}")
