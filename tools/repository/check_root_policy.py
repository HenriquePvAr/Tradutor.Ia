"""Enforce a small, intentional repository root using a checked-in allowlist."""

from __future__ import annotations

import fnmatch
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "config" / "repository_root_allowlist.json"


def check_root(root: Path = ROOT) -> list[str]:
    policy = json.loads((root / "config" / "repository_root_allowlist.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    allowed_python = policy.get("python", {})
    suspicious = policy.get("suspicious_patterns", [])
    ignored_generated = policy.get("ignored_generated_directories", {})
    for path in root.iterdir():
        if path.is_dir():
            if path.name in ignored_generated:
                continue
            if any(fnmatch.fnmatch(path.name, pattern) for pattern in suspicious):
                errors.append(f"unexpected root directory: {path.name}")
            continue
        name = path.name
        if any(fnmatch.fnmatch(name, pattern) for pattern in suspicious):
            errors.append(f"unexpected root artifact: {name}")
        if path.suffix.casefold() == ".py" and name not in allowed_python:
            errors.append(f"unallowlisted root Python module: {name}")
        if path.suffix.casefold() == ".json" and name.endswith("_report.json"):
            errors.append(f"root report must live under reports/: {name}")
    return errors


def main() -> int:
    errors = check_root()
    if errors:
        print("ROOT_POLICY: FAIL")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("ROOT_POLICY: PASS")
    print(f"ALLOWLISTED_ROOT_PYTHON_FILES: {len(json.loads(POLICY_PATH.read_text(encoding='utf-8'))['python'])}")
    detected = sorted(name for name in json.loads(POLICY_PATH.read_text(encoding="utf-8")).get(
        "ignored_generated_directories", {}) if (ROOT / name).is_dir())
    print(f"IGNORED_GENERATED_ROOT_CACHES: {', '.join(detected) if detected else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
