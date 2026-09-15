"""Release-side signer for update manifests. NOT part of the distributed application.

This tool runs in a trusted release process and is the only place a **private** Ed25519 key is
ever loaded. The key is supplied externally at release time — a path on the release machine or
``TRADUTOR_IA_RELEASE_KEY_FILE`` — and is never generated, stored or printed here. The
repository contains no private key material and must never contain any; the client ships only
the public counterpart in ``update_manifest.TRUSTED_PUBLIC_KEYS``.

Generate a release key **outside this repository**, once, on the release machine::

    openssl genpkey -algorithm ed25519 -out <secure-path>/tradutor-ia-release.pem

Then publish its public half into the client's trust store::

    python scripts/sign_release.py pubkey --key <secure-path>/tradutor-ia-release.pem

And sign a release::

    python scripts/sign_release.py sign --key <secure-path>/tradutor-ia-release.pem \
        --key-id beta-2026 --version 1.1.0 --minimum-version 1.0.0 \
        --package dist/tradutor-ia-1.1.0.zip \
        --url https://<host>/tradutor-ia-1.1.0.zip --out dist/update.json

``sha256`` and ``size`` are computed from the real artifact rather than accepted as arguments,
so the signed metadata cannot describe a package that was never built.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from update_manifest import (  # noqa: E402  (path bootstrap above)
    APP_ID,
    CLIENT_UPDATE_CHANNEL,
    SUPPORTED_SCHEMA_VERSION,
    canonical_payload_bytes,
    parse_version,
    sha256_file,
)


def public_key_material(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    """Base64 of the raw public key — the exact form the client's trust store holds."""
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    raw = public.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode("ascii")


def sign_manifest(payload: dict, private_key: Ed25519PrivateKey, *, key_id: str) -> dict:
    """Wrap the canonical payload bytes and their signature in the signed envelope.

    The signature deliberately sits *outside* ``payload`` so the signed bytes are well defined
    and the format is not circular.
    """
    signature = private_key.sign(canonical_payload_bytes(payload))
    return {
        "payload": payload,
        "key_id": key_id,
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def build_payload(
    *, version: str, minimum_version: str, package_path: Path, url: str, app_id: str = APP_ID,
    minimum_bootstrap_version: str | None = None,
    channel: str = CLIENT_UPDATE_CHANNEL, artifact_type: str = "zip", release_notes: str = "",
) -> dict:
    parse_version(version)
    parse_version(minimum_version)
    package_path = Path(package_path)
    extra = {}
    if minimum_bootstrap_version:
        parse_version(minimum_bootstrap_version)
        # Only emitted when a release genuinely needs a newer launcher, so ordinary manifests
        # stay byte-identical to the shape #57 signed.
        extra["minimum_bootstrap_version"] = minimum_bootstrap_version
    if artifact_type not in {"zip", "windows-installer"}:
        raise ValueError("unsupported artifact type")
    return {
        **extra,
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "app_id": app_id,
        "channel": channel,
        "artifact_type": artifact_type,
        "release_notes": release_notes,
        "version": version,
        "minimum_version": minimum_version,
        "published_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "package": {
            "filename": package_path.name,
            "url": url,
            "sha256": sha256_file(package_path),
            "size": package_path.stat().st_size,
        },
    }


def load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("release key must be an Ed25519 private key")
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--key", required=True, type=Path, help="path to the external Ed25519 private key (PEM)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("pubkey", help="print the public verification key for the client trust store")

    signer = sub.add_parser("sign", help="produce a signed update manifest")
    signer.add_argument("--key-id", required=True)
    signer.add_argument("--version", required=True)
    signer.add_argument("--minimum-version", required=True)
    signer.add_argument(
        "--minimum-bootstrap-version",
        help="oldest launcher that may host this payload (omit unless a release needs one)",
    )
    signer.add_argument("--package", required=True, type=Path)
    signer.add_argument("--url", required=True)
    signer.add_argument("--app-id", default=APP_ID)
    signer.add_argument("--channel", default=CLIENT_UPDATE_CHANNEL)
    signer.add_argument("--artifact-type", default="zip", choices=("zip", "windows-installer"))
    signer.add_argument("--release-notes", default="")
    signer.add_argument("--out", required=True, type=Path)

    args = parser.parse_args(argv)
    private_key = load_private_key(args.key)

    if args.command == "pubkey":
        print(public_key_material(private_key))
        return 0

    if not args.url.startswith("https://"):
        raise SystemExit("package url must be https")
    payload = build_payload(
        version=args.version,
        minimum_version=args.minimum_version,
        package_path=args.package,
        url=args.url,
        app_id=args.app_id,
        minimum_bootstrap_version=args.minimum_bootstrap_version,
        channel=args.channel,
        artifact_type=args.artifact_type,
        release_notes=args.release_notes,
    )
    args.out.write_text(json.dumps(sign_manifest(payload, private_key, key_id=args.key_id), indent=2))
    print(f"signed manifest written: {args.out} ({args.app_id} {args.version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
