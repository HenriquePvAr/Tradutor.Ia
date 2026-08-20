"""Trust and integrity gates for a signed application update (TDD #57).

This module is the *authenticity* half of the updater and deliberately owns no transport: it
never opens a socket, never reads an environment variable and never touches the installed
application. It takes bytes that someone else obtained and decides, fail-closed, whether those
bytes may be trusted. ``update_installer.py`` owns the *installation* half; a future TDD owns
the HTTPS client that produces the bytes.

Why asymmetric and not a shared secret: the client is distributed to testers, so anything it
contains is public in practice. An HMAC key or API token embedded in the client would let
anyone who extracts it sign a malicious update. The client therefore holds only Ed25519
**public** verification keys; the private release key lives outside this repository and is used
only by ``scripts/sign_release.py`` in a trusted release process.

Ed25519 comes from ``cryptography``, already a pinned dependency (``cryptography>=42,<50``) used
by the auth boundary. No new crypto stack, no home-grown cryptography.

Two independent gates are both mandatory:

- the **signature** proves the release metadata was produced by the release key (authenticity);
- the **SHA-256** proves the package bytes are exactly the ones that metadata describes
  (integrity).

SHA-256 alone would be worthless: an attacker who controls the hosting can change the package
*and* the hash it publishes. The hash is only meaningful because it arrives inside a payload
that was signed offline. Hence the fixed order in ``verify_manifest``: schema, application
identity and signature are settled before any package byte is trusted, and
``update_installer.stage_release`` refuses to unpack anything that has not been through here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import ntpath
import posixpath
import re
import stat
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

log = logging.getLogger(__name__)

#: The application this client accepts updates for. A validly signed manifest for a different
#: product must never install here, even if it was signed by the same release key.
APP_ID = "tradutor-ia"

#: Only this manifest schema is understood. An unknown schema fails closed rather than being
#: interpreted with guessed semantics.
SUPPORTED_SCHEMA_VERSION = 1

SIGNATURE_ALGORITHM = "ed25519"
_SIGNATURE_LENGTH = 64
_KEY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

#: Local trust root: ``key_id -> base64 raw Ed25519 public key``. Public material only; nothing
#: here is secret. It is **intentionally empty**: no production release key exists yet, and
#: shipping an invented placeholder as the final trust root would be worse than shipping none.
#: ``load_trusted_keys`` therefore fails closed, and tests inject their own ephemeral key
#: explicitly instead of a flag that weakens the production gate.
TRUSTED_PUBLIC_KEYS: dict[str, str] = {}


class UpdateError(Exception):
    """Base class for every updater failure, so no caller has to catch bare ``Exception``."""


class ManifestInvalid(UpdateError):
    """Structure, types, encodings or field values are not a well-formed manifest."""


class UnsupportedManifest(UpdateError):
    """Well-formed but written against a schema version this client cannot interpret."""


class WrongApplication(UpdateError):
    """Signed for a different ``app_id``."""


class SignatureInvalid(UpdateError):
    """No trusted key verifies this payload."""


class UpdateTrustNotConfigured(UpdateError):
    """No local trust root is available, so nothing can be verified."""


class PackageSizeMismatch(UpdateError):
    """Package is not the exact size the signed manifest declares (truncated or padded)."""


class PackageHashMismatch(UpdateError):
    """Package bytes are not the ones the signed manifest describes."""


class UnsafeArchive(UpdateError):
    """Archive contains an entry that would write outside the staging directory."""


# --- canonical signed payload ------------------------------------------------------------

def canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    """Serialize the unsigned payload to the exact bytes that are signed and verified.

    Sorted keys and no whitespace, so the same semantic manifest always produces the same
    bytes regardless of dictionary insertion order or how the JSON was pretty-printed in
    transit. Signing a pretty-printed document would make the signature depend on formatting
    nobody controls end to end.

    Floats are rejected: they are the one JSON type whose text form does not round-trip
    predictably, and no manifest field needs one.
    """

    def _check(value: Any) -> None:
        if isinstance(value, float):
            raise ManifestInvalid("manifest payload must not contain floating point values")
        if isinstance(value, Mapping):
            for item in value.values():
                _check(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _check(item)

    _check(payload)
    try:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManifestInvalid(f"manifest payload is not serializable: {exc}") from exc


def parse_version(text: Any) -> tuple[int, int, int]:
    """Parse a strict ``MAJOR.MINOR.PATCH`` version into a comparable tuple.

    Tuple comparison is what makes ``1.10.0 > 1.9.0`` true; string comparison would get that
    backwards. The format is deliberately narrow (no pre-release, no build metadata) because
    the Beta has exactly one channel and an unparseable version must fail, never sort oddly.
    """
    if not isinstance(text, str) or not _VERSION_PATTERN.match(text):
        raise ManifestInvalid(f"invalid version: {text!r}")
    major, minor, patch = text.split(".")
    return int(major), int(minor), int(patch)


def format_version(version: tuple[int, int, int]) -> str:
    return "{}.{}.{}".format(*version)


# --- manifest ----------------------------------------------------------------------------

@dataclass(frozen=True)
class UpdatePackage:
    filename: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class UpdateManifest:
    schema_version: int
    app_id: str
    version: tuple[int, int, int]
    minimum_version: tuple[int, int, int]
    published_at: datetime
    package: UpdatePackage
    key_id: str

    @property
    def version_text(self) -> str:
        return format_version(self.version)


def _require(payload: Mapping[str, Any], field: str, expected_type: type) -> Any:
    if field not in payload:
        raise ManifestInvalid(f"missing mandatory manifest field: {field}")
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, expected_type):
        raise ManifestInvalid(f"manifest field {field} must be {expected_type.__name__}")
    return value


def _parse_package(raw: Any) -> UpdatePackage:
    if not isinstance(raw, Mapping):
        raise ManifestInvalid("manifest field package must be an object")
    filename = _require(raw, "filename", str)
    if not filename or filename != posixpath.basename(filename) or filename in {".", ".."}:
        raise ManifestInvalid(f"unsafe package filename: {filename!r}")
    url = _require(raw, "url", str)
    # The signature authorizes this exact artifact reference; a future transport must fetch the
    # signed URL and nothing else. HTTPS is required, but it never replaces the signature.
    if not url.startswith("https://"):
        raise ManifestInvalid("package url must be https")
    sha256 = _require(raw, "sha256", str)
    if not _SHA256_PATTERN.match(sha256):
        raise ManifestInvalid("package sha256 must be 64 lowercase hex characters")
    size = _require(raw, "size", int)
    if size <= 0:
        raise ManifestInvalid("package size must be positive")
    return UpdatePackage(filename=filename, url=url, sha256=sha256, size=size)


def parse_manifest(document: Any) -> tuple[dict, str, bytes]:
    """Validate the signed envelope and return ``(payload, key_id, signature)``.

    Only the envelope is checked here; the payload's own fields are validated after the
    signature is known to be good, in ``verify_manifest``.
    """
    if isinstance(document, (str, bytes, bytearray)):
        try:
            document = json.loads(document)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ManifestInvalid(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ManifestInvalid("manifest must be a JSON object")

    payload = document.get("payload")
    if not isinstance(payload, Mapping):
        raise ManifestInvalid("manifest must carry a signed payload object")
    if "signature" in payload:
        # Keeping the signature out of the signed bytes avoids a circular definition.
        raise ManifestInvalid("signature must live outside the signed payload")

    key_id = document.get("key_id")
    if not isinstance(key_id, str) or not _KEY_ID_PATTERN.match(key_id):
        raise ManifestInvalid("manifest must carry a well-formed key_id")

    encoded = document.get("signature")
    if not isinstance(encoded, str) or not encoded:
        raise ManifestInvalid("manifest must carry a signature")
    try:
        signature = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestInvalid(f"signature is not valid base64: {exc}") from exc
    if len(signature) != _SIGNATURE_LENGTH:
        raise ManifestInvalid("signature is not an Ed25519 signature")

    return dict(payload), key_id, signature


def load_trusted_keys() -> dict[str, str]:
    """Production trust root, or a hard failure while none exists.

    Fail-closed on purpose: an updater with no trust root must refuse to update, never fall
    back to accepting unsigned metadata.
    """
    if not TRUSTED_PUBLIC_KEYS:
        raise UpdateTrustNotConfigured(
            "no trusted release public key is configured in this build"
        )
    return dict(TRUSTED_PUBLIC_KEYS)


def verify_manifest(
    document: Any, *, trusted_keys: Mapping[str, str], app_id: str = APP_ID
) -> UpdateManifest:
    """Full fail-closed verification chain; returns the manifest only if every gate passes.

    Order matters: envelope, trust root, signature, then and only then the payload's meaning.
    Nothing downstream may treat any manifest field as trustworthy before this returns.
    """
    payload, key_id, signature = parse_manifest(document)

    if not trusted_keys:
        raise UpdateTrustNotConfigured("no trusted release public key was provided")
    encoded_key = trusted_keys.get(key_id)
    if encoded_key is None:
        raise SignatureInvalid(f"manifest signed by unknown key_id: {key_id}")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded_key, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise UpdateTrustNotConfigured(f"trusted key {key_id} is unusable: {exc}") from exc

    try:
        public_key.verify(signature, canonical_payload_bytes(payload))
    except InvalidSignature as exc:
        raise SignatureInvalid("manifest signature does not verify") from exc

    schema_version = _require(payload, "schema_version", int)
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise UnsupportedManifest(f"unsupported manifest schema_version: {schema_version}")

    manifest_app_id = _require(payload, "app_id", str)
    if manifest_app_id != app_id:
        raise WrongApplication(f"manifest is for {manifest_app_id!r}, not {app_id!r}")

    version = parse_version(payload.get("version"))
    minimum_version = parse_version(payload.get("minimum_version"))
    if minimum_version > version:
        raise ManifestInvalid("minimum_version cannot be newer than version")

    published_at_raw = _require(payload, "published_at", str)
    try:
        published_at = datetime.fromisoformat(published_at_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestInvalid(f"invalid published_at: {published_at_raw!r}") from exc

    manifest = UpdateManifest(
        schema_version=schema_version,
        app_id=manifest_app_id,
        version=version,
        minimum_version=minimum_version,
        published_at=published_at,
        package=_parse_package(payload.get("package")),
        key_id=key_id,
    )
    log.info(
        "update_manifest_verified",
        extra={"app_id": manifest.app_id, "version": manifest.version_text, "key_id": key_id},
    )
    return manifest


# --- version policy ----------------------------------------------------------------------

@dataclass(frozen=True)
class UpdateDecision:
    state: str  # up_to_date | update_available | mandatory_update | downgrade_rejected
    reason: str
    version: str | None

    @property
    def should_install(self) -> bool:
        return self.state in {"update_available", "mandatory_update"}

    @property
    def mandatory(self) -> bool:
        return self.state == "mandatory_update"


def decide_update(current_version: str, manifest: UpdateManifest) -> UpdateDecision:
    """Compare the installed version against a *already verified* manifest.

    A remote manifest can never move the installation backwards: that is what a rollback is
    for, and a rollback runs against a locally installed release that was already verified,
    never against a re-downloaded older one.
    """
    current = parse_version(current_version)
    offered = manifest.version

    if offered < current:
        return UpdateDecision("downgrade_rejected", "remote_version_older", manifest.version_text)
    if offered == current:
        return UpdateDecision("up_to_date", "already_current", None)
    if current < manifest.minimum_version:
        log.info("update_available", extra={"version": manifest.version_text, "mandatory": True})
        return UpdateDecision("mandatory_update", "below_minimum_version", manifest.version_text)
    log.info("update_available", extra={"version": manifest.version_text, "mandatory": False})
    return UpdateDecision("update_available", "newer_version", manifest.version_text)


# --- package integrity -------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_package(path: Path, *, sha256: str, size: int) -> None:
    """Prove the local file is byte-for-byte the artifact the signed manifest describes.

    Size is checked first because it is free and catches the common truncated-download case
    before hashing; the file is then hashed exactly once.
    """
    path = Path(path)
    try:
        actual_size = path.stat().st_size
    except OSError as exc:
        raise PackageHashMismatch(f"package is not readable: {exc}") from exc
    if actual_size != size:
        raise PackageSizeMismatch(f"package size {actual_size} != declared {size}")
    actual = sha256_file(path)
    if actual != sha256:
        raise PackageHashMismatch("package sha256 does not match the signed manifest")
    log.info("update_package_verified", extra={"sha256": sha256, "size": size})


# --- safe extraction ---------------------------------------------------------------------

def _reject_unsafe_entry(info: zipfile.ZipInfo) -> str:
    """Return the safe relative path for an archive entry, or raise ``UnsafeArchive``."""
    name = info.filename
    if stat.S_ISLNK(info.external_attr >> 16):
        # A link entry could point anywhere, including outside the install root.
        raise UnsafeArchive(f"archive entry is a link: {name!r}")
    normalised = name.replace("\\", "/")
    if normalised.startswith("/") or ntpath.splitdrive(name)[0] or ntpath.isabs(name):
        raise UnsafeArchive(f"archive entry has an absolute path: {name!r}")
    parts = [part for part in normalised.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise UnsafeArchive(f"archive entry escapes the destination: {name!r}")
    if not parts:
        raise UnsafeArchive(f"archive entry has no usable name: {name!r}")
    return "/".join(parts)


def extract_package(package_path: Path, destination: Path) -> Path:
    """Unpack a verified ZIP package into an isolated directory, refusing any escape.

    Every entry is validated *before* anything is written, so a malicious archive cannot drop
    half its payload and then fail; and each resolved target is re-checked against the
    destination as defence in depth against normalisation surprises.
    """
    destination = Path(destination)
    resolved_root = destination.resolve()
    with zipfile.ZipFile(package_path) as archive:
        entries = [(info, _reject_unsafe_entry(info)) for info in archive.infolist()]
        destination.mkdir(parents=True, exist_ok=True)
        for info, relative in entries:
            target = (destination / relative)
            if not str(target.resolve()).startswith(str(resolved_root)):
                raise UnsafeArchive(f"archive entry escapes the destination: {info.filename!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as handle:
                handle.write(source.read())
    return destination
