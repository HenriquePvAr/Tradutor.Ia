"""Build a local unsigned update manifest without uploading artifacts."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
from app_version import PRODUCT_VERSION

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("artifact", type=Path); p.add_argument("--version", default=PRODUCT_VERSION)
    p.add_argument("--channel", default="beta"); p.add_argument("--release-notes", default="")
    p.add_argument("--mandatory", action="store_true"); p.add_argument("--min-supported-version", default="0")
    p.add_argument("--output", type=Path, required=True); a = p.parse_args()
    data = a.artifact.read_bytes()
    # This helper prepares the unsigned payload consumed by the release signer.  It intentionally
    # emits the same channel-bearing schema the client verifies; unsigned output is never trusted.
    manifest = {"schema_version": 1, "app_id": "tradutor-ia", "channel": a.channel,
                "version": a.version, "minimum_version": a.min_supported_version,
                "published_at": None,
                "package": {"filename": a.artifact.name, "url": "",
                             "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}
    a.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

if __name__ == "__main__": main()
