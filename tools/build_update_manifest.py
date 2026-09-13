"""Build a local unsigned update manifest without uploading artifacts."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("artifact", type=Path); p.add_argument("--version", required=True)
    p.add_argument("--channel", default="beta"); p.add_argument("--release-notes", default="")
    p.add_argument("--mandatory", action="store_true"); p.add_argument("--min-supported-version", default="0")
    p.add_argument("--output", type=Path, required=True); a = p.parse_args()
    data = a.artifact.read_bytes()
    manifest = {"version": a.version, "channel": a.channel, "release_notes": a.release_notes,
                "download_url": "", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                "mandatory": a.mandatory, "min_supported_version": a.min_supported_version,
                "published_at": None, "signature": None}
    a.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

if __name__ == "__main__": main()
