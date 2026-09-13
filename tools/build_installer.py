"""Invoke Inno Setup with the canonical build version."""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from app_version import BUILD_VERSION


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iscc", default="ISCC.exe")
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--script", type=Path, default=Path("packaging/TradutorIA.iss"))
    args = parser.parse_args()
    command = [args.iscc, f"/DProductVersion={BUILD_VERSION}", f"/DBundleRoot={args.bundle_root}", f"/DOutputDir={args.output_dir}", str(args.script)]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
