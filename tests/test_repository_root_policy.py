from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY_TOOL = ROOT / "tools" / "repository" / "check_root_policy.py"
SPEC = importlib.util.spec_from_file_location("check_root_policy", POLICY_TOOL)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_repository_root_matches_the_declarative_allowlist():
    assert module.check_root(ROOT) == []


def test_generated_root_caches_are_explicitly_identified():
    policy = module.json.loads((ROOT / "config/repository_root_allowlist.json").read_text(encoding="utf-8"))
    assert set(policy["ignored_generated_directories"]) == {"__pycache__", ".pytest_cache"}


def test_root_policy_rejects_a_stray_runtime_python_file(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "repository_root_allowlist.json").write_text(
        (ROOT / "config" / "repository_root_allowlist.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "unclassified.py").write_text("pass\n", encoding="utf-8")
    assert "unallowlisted root Python module: unclassified.py" in module.check_root(tmp_path)


def test_root_policy_rejects_generated_artifacts(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "repository_root_allowlist.json").write_text(
        (ROOT / "config" / "repository_root_allowlist.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "probe_model.pt").write_bytes(b"fixture")
    assert "unexpected root artifact: probe_model.pt" in module.check_root(tmp_path)
